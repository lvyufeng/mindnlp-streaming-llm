import math
from typing import Optional, Tuple

import mindspore
from mindspore import ops

from mindnlp.transformers.models.gpt_neox.modeling_gpt_neox import (
    apply_rotary_pos_emb,
    rotate_half,
    GPTNeoXAttention,
)
import types

__all__ = ["enable_gpt_neox_pos_shift_attention"]


def apply_rotary_pos_emb_single(x, cos, sin, position_ids):
    gather_indices = position_ids[:, None, :, None]  # [bs, 1, seq_len, 1]
    gather_indices = gather_indices.repeat(1, cos.shape[1], 1, cos.shape[3])
    cos = ops.gather_elements(cos.repeat(gather_indices.shape[0], 1, 1, 1), 2, gather_indices)
    sin = ops.gather_elements(sin.repeat(gather_indices.shape[0], 1, 1, 1), 2, gather_indices)
    x_embed = (x * cos) + (rotate_half(x) * sin)
    return x_embed


def gpt_neox_pos_shift_attention_forward(
    self,
    hidden_states: mindspore.Tensor,
    attention_mask: mindspore.Tensor,
    position_ids: mindspore.Tensor,
    head_mask: Optional[mindspore.Tensor] = None,
    layer_past: Optional[Tuple[mindspore.Tensor]] = None,
    use_cache: Optional[bool] = False,
    output_attentions: Optional[bool] = False,
):
    has_layer_past = layer_past is not None

    # Compute QKV
    # Attention heads [batch, seq_len, hidden_size]
    #   --> [batch, seq_len, (np * 3 * head_size)]
    qkv = self.query_key_value(hidden_states)

    # [batch, seq_len, (num_heads * 3 * head_size)]
    #   --> [batch, seq_len, num_heads, 3 * head_size]
    new_qkv_shape = qkv.shape[:-1] + (self.num_attention_heads, 3 * self.head_size)
    qkv = qkv.view(*new_qkv_shape)

    # [batch, seq_len, num_attention_heads, 3 * head_size] --> 3 [batch, num_attention_heads, seq_len, head_size]
    query = qkv[..., : self.head_size].permute(0, 2, 1, 3)
    key = qkv[..., self.head_size : 2 * self.head_size].permute(0, 2, 1, 3)
    value = qkv[..., 2 * self.head_size :].permute(0, 2, 1, 3)

    # Compute rotary embeddings on rotary_ndims
    query_rot = query[..., : self.rotary_ndims]
    query_pass = query[..., self.rotary_ndims :]

    # Compute token offset for rotary embeddings (when decoding)
    seq_len = key.shape[-2]
    if has_layer_past:
        seq_len += layer_past[0].shape[-2]
    cos, sin = self.rotary_emb(value, seq_len=seq_len)
    query = apply_rotary_pos_emb_single(query_rot, cos, sin, position_ids)
    query = ops.cat((query, query_pass), axis=-1)

    # Cache QKV values
    if has_layer_past:
        past_key = layer_past[0]
        past_value = layer_past[1]
        key = ops.cat((past_key, key), axis=-2)
        value = ops.cat((past_value, value), axis=-2)

    present = (key, value) if use_cache else None

    key_rot = key[..., : self.rotary_ndims]
    key_pass = key[..., self.rotary_ndims :]
    key_position_ids = ops.arange(seq_len).unsqueeze(0)
    key = apply_rotary_pos_emb_single(key_rot, cos, sin, key_position_ids)
    key = ops.cat((key, key_pass), axis=-1)

    # Compute attention
    attn_output, attn_weights = self._attn(query, key, value, attention_mask, head_mask)

    # Reshape outputs
    attn_output = self._merge_heads(
        attn_output, self.num_attention_heads, self.head_size
    )
    attn_output = self.dense(attn_output)

    outputs = (attn_output, present)
    if output_attentions:
        outputs += (attn_weights,)

    return outputs


def enable_gpt_neox_pos_shift_attention(model):
    for name, module in reversed(model._cells.items()):
        if len(list(module.cells())) > 0:
            enable_gpt_neox_pos_shift_attention(
                module,
            )

        if isinstance(module, GPTNeoXAttention):
            module.construct = types.MethodType(
                gpt_neox_pos_shift_attention_forward, module
            )