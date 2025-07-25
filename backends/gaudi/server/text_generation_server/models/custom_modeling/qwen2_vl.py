# coding=utf-8
# Copyright 2024 the HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch Qwen2 VL model."""

from typing import Optional, Tuple, List

import torch
import torch.utils.checkpoint
from torch import nn


from habana_frameworks.torch.hpex.kernels import FusedSDPA
from vllm_hpu_extension.utils import ModuleFusedSDPA


import numpy as np

from transformers.activations import ACT2FN
import torch.nn.functional as F

from text_generation_server.layers.layernorm import FastLayerNorm, FastRMSNorm
from text_generation_server.layers import (
    TensorParallelColumnLinear,
    TensorParallelRowLinear,
    TensorParallelEmbedding,
    SpeculativeHead,
)
from text_generation_server.layers.attention import (
    Seqlen,
    HPUPagedAttentionMetadata,
)
from text_generation_server.models.custom_modeling.flash_qwen2_modeling import (
    Qwen2Model,
)
from habana_frameworks.torch.hpex.kernels import (
    RotaryPosEmbeddingMode,
    apply_rotary_pos_emb,
)
import habana_frameworks.torch as htorch


class Qwen2VLAttention(nn.Module):
    def __init__(self, *, prefix, config, weights):
        super().__init__()
        self.embed_dim = config.embed_dim // weights.process_group.size()
        self.head_dim = config.hidden_size // config.num_heads
        self.num_heads = config.num_heads // weights.process_group.size()

        self.qkv = TensorParallelColumnLinear.load_qkv(
            config,
            prefix=f"{prefix}.qkv",
            weights=weights,
            bias=False,
            num_heads=self.num_heads,
            num_key_value_heads=self.num_heads,
        )
        self.qkv.linear.bias = weights.get_sharded(f"{prefix}.qkv.bias", dim=0)
        self.proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.proj",
            weights=weights,
            bias=True,
        )
        self.softmax_scale = 1.0 / np.sqrt(self.embed_dim // self.num_heads)

    def forward(
        self,
        hidden_state: torch.Tensor,
        cu_seqlens: torch.Tensor,
        cos: torch.Tensor,
        sin: torch.Tensor,
        max_seqlen: int,
    ) -> torch.Tensor:
        # apply the qkv linear layer to the hidden state
        qkv = self.qkv(hidden_state)
        query, key, value = qkv.split(
            [self.embed_dim, self.embed_dim, self.embed_dim], dim=1
        )

        # reshape the query, key, and value tensors
        _shape = (
            hidden_state.shape[0],
            self.num_heads,
            self.embed_dim // self.num_heads,
        )
        query = query.view(*_shape)
        key = key.view(*_shape)
        value = value.view(*_shape)

        # apply rotary positional embeddings
        rope_mode = RotaryPosEmbeddingMode.BLOCKWISE
        rotary_dim = cos.shape[-1]
        query_rot = query[..., :rotary_dim]
        query_pass = query[..., rotary_dim:]
        query_rot = apply_rotary_pos_emb(query_rot, cos, sin, None, 0, rope_mode)
        query.copy_(torch.cat((query_rot, query_pass), dim=-1).reshape(query.shape))

        key_rot = key[..., :rotary_dim]
        key_pass = key[..., rotary_dim:]
        key_rot = apply_rotary_pos_emb(key_rot, cos, sin, None, 0, rope_mode)
        key.copy_(torch.cat((key_rot, key_pass), dim=-1).reshape(key.shape))

        # execute sdpa
        causal = False
        query = query.transpose(0, 1)
        key = key.transpose(0, 1)
        value = value.transpose(0, 1)
        fsdpa_op = ModuleFusedSDPA(FusedSDPA)
        attention_mask = torch.zeros(
            [1, max_seqlen, max_seqlen], device=query.device, dtype=torch.bool
        )
        for i in range(1, len(cu_seqlens)):
            attention_mask[
                :, cu_seqlens[i - 1] : cu_seqlens[i], cu_seqlens[i - 1] : cu_seqlens[i]
            ] = True
        attn_output = fsdpa_op(
            query,
            key,
            value,
            attn_mask=attention_mask,
            dropout_p=0.0,
            is_causal=causal,
            scale=None,
            softmax_mode="None",
            recompute_mode=None,
            valid_sequence_lengths=None,
        )
        attn_output = attn_output.transpose(0, 1)
        # reshape output to original dimensions
        attn_output = attn_output.reshape(hidden_state.shape[0], -1)
        attn_output = self.proj(attn_output)
        return attn_output


class Qwen2VLVisionMLP(nn.Module):
    def __init__(self, *, prefix, config, weights):
        super().__init__()
        self.activation_fn = ACT2FN[config.hidden_act]
        self.fc1 = TensorParallelColumnLinear.load(
            prefix=f"{prefix}.fc1", weights=weights, config=config, bias=True
        )
        self.fc2 = TensorParallelRowLinear.load(
            prefix=f"{prefix}.fc2", weights=weights, config=config, bias=True
        )

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.fc1(hidden_states)
        hidden_states = self.activation_fn(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states


class Qwen2VLVisionBlock(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        self.attn = Qwen2VLAttention(
            prefix=f"{prefix}.attn",
            config=config,
            weights=weights,
        )
        self.norm1 = FastLayerNorm.load(
            prefix=f"{prefix}.norm1",
            weights=weights,
            eps=1e-6,
        )
        self.norm2 = FastLayerNorm.load(
            prefix=f"{prefix}.norm2",
            weights=weights,
            eps=1e-6,
        )
        self.mlp = Qwen2VLVisionMLP(
            prefix=f"{prefix}.mlp",
            config=config,
            weights=weights,
        )

    def forward(self, hidden_states, cu_seqlens, cos, sin, max_seqlen) -> torch.Tensor:
        norm1_out, residual = self.norm1(hidden_states)
        attn_out = self.attn(norm1_out, cu_seqlens, cos, sin, max_seqlen)
        hidden_states = attn_out + residual
        norm2_out, residual = self.norm2(hidden_states)
        hidden_states = hidden_states + self.mlp(norm2_out)
        return hidden_states


class Qwen2VLPatchMerger(nn.Module):
    def __init__(self, *, prefix, config, weights):
        super().__init__()
        self.hidden_size = config.embed_dim * (config.spatial_merge_size**2)
        self.patch_merger_ln_q = FastLayerNorm.load(
            prefix=f"{prefix}.ln_q",
            weights=weights,
            eps=1e-6,
        )
        self.fc1 = TensorParallelColumnLinear.load(
            prefix=f"{prefix}.mlp.0", weights=weights, config=config, bias=True
        )
        self.fc2 = TensorParallelRowLinear.load(
            prefix=f"{prefix}.mlp.2", weights=weights, config=config, bias=True
        )

    def forward(self, hidden_states) -> torch.Tensor:
        hidden_states, _ = self.patch_merger_ln_q(hidden_states)
        hidden_states = hidden_states.view(-1, self.hidden_size)
        hidden_states = self.fc1(hidden_states)
        hidden_states = F.gelu(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states


class Qwen2VisionModel(nn.Module):
    def __init__(self, *, prefix, config, weights):
        super().__init__()
        self.spatial_merge_size = config.spatial_merge_size
        kernel_size = [config.temporal_patch_size, config.patch_size, config.patch_size]
        self.patch_embedding = nn.Conv3d(
            in_channels=config.in_chans,
            out_channels=config.embed_dim,
            kernel_size=kernel_size,
            stride=kernel_size,
            bias=False,
        )
        self.patch_embedding.weight = nn.Parameter(
            weights.get_tensor(f"{prefix}.patch_embed.proj.weight"), requires_grad=False
        )
        head_dim = config.embed_dim // config.num_heads
        # TODO: replace with static positional embeddings once implemented
        theta = 10000.0
        dim = head_dim // 2
        inv_freq = 1.0 / (theta ** (torch.arange(0, dim, 2, dtype=torch.float) / dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

        self.blocks = nn.ModuleList(
            [
                Qwen2VLVisionBlock(
                    prefix=f"{prefix}.blocks.{i}",
                    config=config,
                    weights=weights,
                )
                for i in range(config.depth)
            ]
        )
        self.merger = Qwen2VLPatchMerger(
            prefix=f"{prefix}.merger",
            config=config,
            weights=weights,
        )

        self.temporal_patch_size = config.temporal_patch_size
        self.spatial_patch_size = config.spatial_patch_size
        self.in_channels = config.in_channels
        self.embed_dim = config.embed_dim

    def apply_class_embedding(self, hidden_state: torch.Tensor) -> torch.Tensor:
        batch_size, _, hidden_size = hidden_state.shape
        class_embedding = self.class_embedding.expand(batch_size, 1, hidden_size)
        hidden_state = torch.cat([class_embedding, hidden_state], dim=1)
        return hidden_state

    def forward(
        self,
        pixel_values: torch.Tensor,
        grid_thw: Optional[torch.LongTensor] = None,
    ) -> torch.Tensor:
        # reshape the input tensor for processing
        shape = (
            -1,
            self.in_channels,
            self.temporal_patch_size,
            self.spatial_patch_size,
            self.spatial_patch_size,
        )
        pixel_values = pixel_values.view(shape).to(self.patch_embedding.weight.dtype)
        hidden_states = self.patch_embedding(pixel_values).view(-1, self.embed_dim)
        # TODO: revisit to see if we can avoid some of these reshapes

        # find the position ids for the input tensor based on the grid_thw
        pos_ids = []
        for t, h, w in grid_thw:
            hpos_ids = torch.arange(h).unsqueeze(1).expand(-1, w)
            hpos_ids = hpos_ids.reshape(
                h // self.spatial_merge_size,
                self.spatial_merge_size,
                w // self.spatial_merge_size,
                self.spatial_merge_size,
            )
            hpos_ids = hpos_ids.permute(0, 2, 1, 3)
            hpos_ids = hpos_ids.flatten()

            wpos_ids = torch.arange(w).unsqueeze(0).expand(h, -1)
            wpos_ids = wpos_ids.reshape(
                h // self.spatial_merge_size,
                self.spatial_merge_size,
                w // self.spatial_merge_size,
                self.spatial_merge_size,
            )
            wpos_ids = wpos_ids.permute(0, 2, 1, 3)
            wpos_ids = wpos_ids.flatten()
            pos_ids.append(torch.stack([hpos_ids, wpos_ids], dim=-1).repeat(t, 1))

        pos_ids = torch.cat(pos_ids, dim=0)
        max_grid_size = grid_thw[:, 1:].max()

        # apply the positional embeddings to the position ids
        seq = torch.arange(
            max_grid_size, device=self.inv_freq.device, dtype=self.inv_freq.dtype
        )
        rotary_pos_emb_full = torch.outer(seq, self.inv_freq)
        rotary_pos_emb = rotary_pos_emb_full[pos_ids].flatten(1)
        rotary_pos_emb = rotary_pos_emb.to(hidden_states.device, hidden_states.dtype)

        cos = rotary_pos_emb.cos()
        sin = rotary_pos_emb.sin()
        cos = torch.cat((cos, cos), dim=-1).unsqueeze(1)
        sin = torch.cat((sin, sin), dim=-1).unsqueeze(1)

        # create a cu_seqlens tensor to be used in the attention mask
        cu_seqlens = torch.repeat_interleave(
            grid_thw[:, 1] * grid_thw[:, 2], grid_thw[:, 0]
        ).cumsum(dim=0, dtype=torch.int32)
        cu_seqlens = F.pad(cu_seqlens, (1, 0), value=0)
        max_seqlen = torch.max(cu_seqlens[1:] - cu_seqlens[:-1])
        # iterately apply the blocks to the hidden states
        lazy_mode = htorch.utils.internal.is_lazy()
        if lazy_mode:
            htorch.core.mark_step()
        for block in self.blocks:
            hidden_states = block(hidden_states, cu_seqlens, cos, sin, max_seqlen)
            if lazy_mode:
                htorch.core.mark_step()

        # apply the final patch merger to the hidden states
        hidden_states = self.merger(hidden_states)
        return hidden_states


class Qwen2VLForConditionalGeneration(nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()
        self.config = config
        config.vision_config.quantize = None
        config.vision_config.speculator = config.speculator
        # set rope_scaling.type == "mrope" since AutoConfig.from_pretrained incorrectly
        # returns rope_scaling.type == "default" for Qwen2-VL model at the moment
        if (
            hasattr(config, "rope_scaling")
            and config.rope_scaling is not None
            and config.rope_scaling.get("type", None) == "default"
        ):
            config.rope_scaling.update({"rope_type": "mrope"})
        self.hidden_size = config.hidden_size
        self.vision_start_token_id = config.vision_start_token_id
        self.vision_end_token_id = config.vision_end_token_id
        self.image_token_id = config.image_token_id
        self.video_token_id = config.video_token_id
        self.spatial_merge_size = config.vision_config.spatial_merge_size
        self.embed_tokens = TensorParallelEmbedding(
            prefix="model.embed_tokens", weights=weights
        )
        self.visual = Qwen2VisionModel(
            prefix="visual", config=config.vision_config, weights=weights
        )
        self.text_model = Qwen2Model(prefix=None, config=config, weights=weights)
        if config.tie_word_embeddings:
            suffix = "model.embed_tokens"
        else:
            suffix = "lm_head"

        self.lm_head = SpeculativeHead.load(
            config,
            prefix=suffix if not prefix else f"{prefix}.{suffix}",
            weights=weights,
        )
        self.norm = FastRMSNorm.load(
            prefix="model.norm",
            weights=weights,
            eps=config.rms_norm_eps,
        )
        self.device = weights.device

    # based on https://github.com/huggingface/transformers/blob/e284c7e954abe12c34b50461c17f8115a0afe115/src/transformers/models/qwen2_vl/modeling_qwen2_vl.py#L1391
    # modified to first find segments then initialize position ids for each segment
    # Steps:
    #  locate all vision and text segments
    #  calculate `vision_segment_lengths` for each vision segment to be use as offset
    #  calculate `text_segment_lengths` for each text segment to be used as offset
    #  create position ids for each vision segment based on the image grid
    #  create position ids for each text segment
    #  combine all the position ids
    #  the final segment is the difference between the last vision segment and the end of the input
    #  combine all the position ids and reshape to (3, input_ids_len) then swap dimensions to (input_ids_len, 3)
    def get_position_ids(
        self,
        input_ids: torch.Tensor,
        image_grid_thw: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        if image_grid_thw is None:
            return (
                torch.arange(input_ids.shape[0], device=input_ids.device)
                .unsqueeze(1)
                .repeat(1, 3)
            )

        spatial_merge_size = self.spatial_merge_size
        vision_start_token_id = self.vision_start_token_id
        vision_end_token_id = self.vision_end_token_id
        device = input_ids.device
        dtype = input_ids.dtype
        input_ids_len = input_ids.shape[0]

        vision_starts = torch.where(input_ids == vision_start_token_id)[0]
        vision_ends = torch.where(input_ids == vision_end_token_id)[0]
        vision_segments = torch.stack((vision_starts, vision_ends), dim=1)
        prev_vision_end = torch.cat(
            [torch.zeros(1, device=vision_ends.device, dtype=dtype), vision_ends[:-1]]
        )
        text_lengths_between_vision = vision_segments[:, 0] - prev_vision_end + 1
        vision_widths_max = torch.cat(
            [
                torch.zeros(1, device=image_grid_thw.device, dtype=dtype),
                image_grid_thw[:-1, 2] // spatial_merge_size,
            ]
        )
        vision_segment_lengths = vision_widths_max + text_lengths_between_vision
        vision_segment_lengths = vision_segment_lengths.cumsum(dim=0)
        text_segment_lengths = vision_segment_lengths - text_lengths_between_vision

        # create position ids for each vision segment based on the image grid
        llm_pos_ids_list = []
        for i, _ in enumerate(vision_segments):
            t, h, w = (
                image_grid_thw[i][0],
                image_grid_thw[i][1] // spatial_merge_size,
                image_grid_thw[i][2] // spatial_merge_size,
            )
            t_indices = torch.arange(t, device=device).repeat_interleave(h * w)
            h_indices = torch.arange(h, device=device).repeat_interleave(w).repeat(t)
            w_indices = torch.arange(w, device=device).repeat(t * h)
            image_position_ids = torch.stack([t_indices, h_indices, w_indices], dim=0)

            # offset by the position of the last vision segment
            im = image_position_ids + vision_segment_lengths[i]
            llm_pos_ids_list.append(im)

        # create position ids for each text segment
        text_ranges = [
            torch.arange(seq_len, device=device).view(1, -1).expand(3, -1)
            + text_segment_lengths[i]
            for i, seq_len in enumerate(text_lengths_between_vision)
        ]

        full_llm_pos_ids_list = [
            item for sublist in zip(text_ranges, llm_pos_ids_list) for item in sublist
        ]
        max_s = full_llm_pos_ids_list[-1].max() + 1
        final_text_len = input_ids_len - vision_ends[-1]
        if final_text_len > 0:
            m = torch.arange(final_text_len, device=device).view(1, -1).expand(3, -1)
            full_llm_pos_ids_list.append(m + max_s)

        position_ids = (
            torch.cat(full_llm_pos_ids_list, dim=1).reshape(3, -1).transpose(0, 1)
        )
        return position_ids

    def get_vision_embeds(
        self,
        pixel_values: torch.FloatTensor,
        pixel_attention_mask: Optional[torch.FloatTensor] = None,
        image_sizes: Optional[torch.Tensor] = None,
        image_grid_thw: Optional[torch.LongTensor] = None,
    ):
        image_embeds = self.visual(pixel_values, grid_thw=image_grid_thw).squeeze(0)
        return image_embeds

    def get_inputs_embeds(
        self,
        input_ids: torch.Tensor,
        vision_embeds: torch.Tensor = None,
    ):
        inputs_embeds = self.embed_tokens(input_ids)

        # apply the visual model to the pixel values if they are provided
        if vision_embeds is not None:
            mask = torch.where(input_ids == self.image_token_id)
            inputs_embeds[mask] = vision_embeds

        return inputs_embeds

    def forward(
        self,
        inputs_embeds: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
        lm_head_indices: Optional[torch.Tensor],
        attention_mask: Optional[torch.BoolTensor] = None,
        adapter_data: Optional[torch.Tensor] = None,
        image_indices=None,
    ):
        hidden_states = self.text_model(
            inputs_embeds=inputs_embeds,
            position_ids=position_ids,
            cu_seqlen_prefill=cu_seqlen_prefill,
            kv_cache=kv_cache,
            slots=slots,
            seqlen=seqlen,
            hpu_attention_meta=hpu_attention_meta,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits, speculative_logits = self.lm_head(hidden_states)
        return logits, speculative_logits
