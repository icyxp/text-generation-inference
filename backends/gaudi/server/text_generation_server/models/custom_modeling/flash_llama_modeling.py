# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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

from contextlib import contextmanager
from typing import List, Optional, Tuple, Type

import torch
import torch.distributed

from torch import nn
from transformers.activations import ACT2FN
import habana_frameworks.torch as htorch
from text_generation_server.layers.attention import (
    KVCache,
    get_kv_scales,
)
from text_generation_server.layers.moe import DenseMoELayer, MoELayer, SparseMoELayer
from text_generation_server.layers.attention import (
    paged_attention,
    attention,
    set_block_mapping,
    Seqlen,
    HPUPagedAttentionMetadata,
)
from text_generation_server.layers import (
    TensorParallelRowLinear,
    TensorParallelColumnLinear,
    TensorParallelEmbedding,
    SpeculativeHead,
    TensorParallelMultiAdapterLinear,
    TensorParallelAdapterRowLinear,
)
from text_generation_server.layers.rotary import PositionRotaryEmbedding
from text_generation_server.layers.layernorm import (
    FastRMSNorm,
    FastLayerNorm,
)
from text_generation_server.layers import (
    FastLinear,
)
from text_generation_server.utils.weights import (
    Weights,
)
from text_generation_server.layers.fp8 import HybridFP8UnquantLoader


def load_attention(config, prefix: str, weights, layer_id):
    # Only defined in granite.
    bias = getattr(config, "attention_bias", False)
    head_size = config.hidden_size // config.num_attention_heads
    sizes = None
    prefixes = None

    if config.model_type == "phi3":
        base_layer = TensorParallelColumnLinear.load_qkv(
            config,
            prefix=f"{prefix}.qkv_proj",
            weights=weights,
            bias=bias,
            num_heads=config.num_attention_heads,
            num_key_value_heads=config.num_key_value_heads,
        )
        prefixes = ["qkv_proj"]
    elif config.model_type == "baichuan":
        prefix = f"{prefix}.W_pack"
        base_layer = TensorParallelColumnLinear.load_qkv(
            config,
            prefix=prefix,
            weights=weights,
            bias=bias,
            num_heads=config.num_attention_heads,
            num_key_value_heads=config.num_key_value_heads,
        )
        prefixes = [prefix]
    else:
        prefixes = ["q_proj", "k_proj", "v_proj"]
        sizes = [
            head_size * config.num_attention_heads,
            head_size * config.num_key_value_heads,
            head_size * config.num_key_value_heads,
        ]
        base_layer = TensorParallelColumnLinear.load_multi(
            config,
            prefixes=[f"{prefix}.q_proj", f"{prefix}.k_proj", f"{prefix}.v_proj"],
            dim=0,
            weights=weights,
            bias=bias,
        )

    return TensorParallelMultiAdapterLinear.load(
        base_layer=base_layer,
        layer_id=layer_id,
        layer_names=prefixes,
        sizes=sizes,
        process_group=weights.process_group,
    )


@contextmanager
def no_fp8(weights: Weights):
    """De-activate fp8 auto conversion for the duration of this context manager"""
    weights_loader = weights.weights_loader
    if isinstance(weights_loader, HybridFP8UnquantLoader) and weights_loader.to_fp8:
        weights_loader = HybridFP8UnquantLoader(
            weights_loader.activation_scale_ub, to_fp8=False
        )

    with weights.use_loader(weights_loader):
        yield


class FlashLlamaAttention(torch.nn.Module):
    def __init__(
        self,
        index: int,
        prefix: str,
        config,
        weights,
        rotary_emb,
    ):
        super().__init__()
        self.num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.head_size = self.hidden_size // self.num_heads

        self.rotary_emb = rotary_emb

        # `config.attention_multiplier` is used in Granite
        self.softmax_scale = getattr(
            config, "attention_multiplier", self.head_size**-0.5
        )

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        if config.num_key_value_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_key_value_heads` must be divisible by `num_shards` (got `num_key_value_heads`: {config.num_key_value_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()
        self.num_key_value_heads = (
            config.num_key_value_heads // weights.process_group.size()
        )

        self.query_key_value = load_attention(config, prefix, weights, index)
        self.index = index

        self.kv_scales = get_kv_scales(weights, f"{prefix}")

        o_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.o_proj",
            weights=weights,
            bias=getattr(config, "attention_bias", False),
        )

        self.o_proj = TensorParallelAdapterRowLinear.load(
            o_proj,
            index,
            "o_proj",
            process_group=weights.process_group,
        )

        self.num_groups = self.num_heads // self.num_key_value_heads
        self.kv_head_mapping = torch.arange(
            0, self.num_key_value_heads, dtype=torch.int32, device=weights.device
        ).repeat_interleave(self.num_groups)

    def forward(
        self,
        hidden_states,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache: KVCache,
        slots,
        seqlen,
        adapter_data,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ):
        qkv = self.query_key_value(hidden_states, adapter_data)
        query, kv = qkv.split(
            [
                self.head_size * self.num_heads,
                2 * self.head_size * self.num_key_value_heads,
            ],
            dim=1,
        )
        query = query.view(-1, self.num_heads, self.head_size)
        kv = kv.view(-1, 2, self.num_key_value_heads, self.head_size)

        self.rotary_emb(query, torch.select(kv, dim=1, index=0), cos, sin)

        kv_cache.store(
            key=kv[:, 0],
            value=kv[:, 1],
            slots=slots,
            kv_scales=self.kv_scales,
        )

        # Prefill
        if cu_seqlen_prefill is not None:
            # sdpa
            attn_output = attention(
                query=query,
                key=kv[:, 0],
                value=kv[:, 1],
                kv_scales=self.kv_scales,
                kv_cache=kv_cache,
                seqlen=seqlen,
                softmax_scale=self.softmax_scale,
            )
        # Decode
        else:
            attn_output = paged_attention(
                query,
                kv_cache,
                self.kv_head_mapping,
                self.softmax_scale,
                seqlen,
                kv_scales=self.kv_scales,
                hpu_attention_meta=hpu_attention_meta,
            )

        return self.o_proj(
            attn_output.view(-1, self.num_heads * self.head_size), adapter_data
        )


class Phi3MoE(nn.Module):
    def __init__(
        self, prefix: str, config, moe_layer_cls: Type[MoELayer], weights: Weights
    ):
        super().__init__()

        # gating
        self.gate = FastLinear.load(config, f"{prefix}.gate", weights, bias=False)

        self.moe = moe_layer_cls(
            prefix=f"{prefix}.experts",
            n_experts=config.num_local_experts,
            n_expert_group=None,
            renormalize=True,
            topk=config.num_experts_per_tok,
            topk_group=None,
            weights=weights,
            gate_proj_name="w1",
            up_proj_name="w3",
            down_proj_name="w2",
        )

        self.process_group = weights.process_group

    def forward(self, x, adapter_data) -> torch.Tensor:
        # router_logits: (num_tokens, n_experts)
        router_logits = self.gate(x)
        out = self.moe(x, gating_output=router_logits)

        # Reduce sum
        if self.process_group.size() > 1:
            torch.distributed.all_reduce(out, group=self.process_group)

        return out.view(*x.shape)


class LlamaMLP(nn.Module):
    def __init__(self, prefix, config, weights, index):
        super().__init__()
        self.hidden_act = config.hidden_act
        self.act = (
            ACT2FN[self.hidden_act]
            if "gelu" not in self.hidden_act
            else lambda x: torch.nn.functional.gelu(
                x,
                approximate=(
                    "tanh"
                    if self.hidden_act in ["gelu_fast", "gelu_pytorch_tanh"]
                    else "none"
                ),
            )
        )
        prefixes = None
        sizes = None

        # Fuse gate and up proj
        bias = getattr(config, "mlp_bias", False)
        if config.model_type == "phi3":
            gate_up_proj = TensorParallelColumnLinear.load_gate_up(
                config,
                prefix=f"{prefix}.gate_up_proj",
                weights=weights,
                bias=bias,
            )
        else:
            prefixes = ["gate_proj", "up_proj"]
            sizes = [
                config.intermediate_size,
                config.intermediate_size,
            ]
            gate_up_proj = TensorParallelColumnLinear.load_multi(
                config,
                prefixes=[f"{prefix}.gate_proj", f"{prefix}.up_proj"],
                weights=weights,
                dim=0,
                bias=bias,
            )

        self.gate_up_proj = TensorParallelMultiAdapterLinear.load(
            gate_up_proj,
            index,
            layer_names=prefixes,
            sizes=sizes,
            process_group=weights.process_group,
        )

        down_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.down_proj",
            weights=weights,
            bias=bias,
        )

        self.down_proj = TensorParallelAdapterRowLinear.load(
            down_proj,
            index,
            "down_proj",
            process_group=weights.process_group,
        )

        self.intermediate_size = (
            config.intermediate_size // weights.process_group.size()
        )

        # TODO: This is a hotfix to be removed & properly refactored.
        self.quantize = config.quantize

        self.hidden_size = config.hidden_size

    def forward(self, hidden_states, adapter_data):
        gate_up_states = self.gate_up_proj(hidden_states, adapter_data)
        gate_up_states = gate_up_states.view(-1, 2, self.intermediate_size)
        return self.down_proj(
            self.act(gate_up_states[:, 0]) * gate_up_states[:, 1], adapter_data
        )


class FlashLlamaLayer(nn.Module):
    def __init__(self, index, prefix, config, weights, rotary_emb):
        super().__init__()

        with no_fp8(weights):
            self.self_attn = FlashLlamaAttention(
                index=index,
                prefix=f"{prefix}.self_attn",
                config=config,
                weights=weights,
                rotary_emb=rotary_emb,
            )

        if config.model_type == "phimoe":
            moe_layer_cls = (
                SparseMoELayer
                if SparseMoELayer.is_supported(weights)
                else DenseMoELayer
            )
            self.mlp = Phi3MoE(
                f"{prefix}.block_sparse_moe", config, moe_layer_cls, weights
            )
            # with moe the layernorms are are not rmsnorms and they have bias
            self.input_layernorm = FastLayerNorm.load(
                prefix=f"{prefix}.input_layernorm",
                weights=weights,
                eps=config.rms_norm_eps,
            )
            self.post_attention_layernorm = FastLayerNorm.load(
                prefix=f"{prefix}.post_attention_layernorm",
                weights=weights,
                eps=config.rms_norm_eps,
            )
        else:
            self.mlp = LlamaMLP(
                prefix=f"{prefix}.mlp", config=config, weights=weights, index=index
            )
            self.input_layernorm = FastRMSNorm.load(
                prefix=f"{prefix}.input_layernorm",
                weights=weights,
                eps=config.rms_norm_eps,
            )
            self.post_attention_layernorm = FastRMSNorm.load(
                prefix=f"{prefix}.post_attention_layernorm",
                weights=weights,
                eps=config.rms_norm_eps,
            )

        # Used in Granite
        # This could eventually be baked into the weights like we do for the embeddings/lm_head
        # but this would mean modifying the lora code
        self.residual_multiplier = getattr(config, "residual_multiplier", None)

    def forward(
        self,
        hidden_states,
        residual,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        slots,
        seqlen,
        adapter_data,
        cross_attention_states,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
    ):
        normed_hidden_states, res = self.input_layernorm(hidden_states, residual)

        # Self Attention
        attn_output = self.self_attn(
            normed_hidden_states,
            cos,
            sin,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            adapter_data,
            hpu_attention_meta=hpu_attention_meta,
        )
        if self.residual_multiplier is not None:
            attn_output *= self.residual_multiplier

        normed_attn_res_output, attn_res = self.post_attention_layernorm(
            attn_output, res
        )

        mlp_output = self.mlp(normed_attn_res_output, adapter_data)
        if self.residual_multiplier is not None:
            mlp_output *= self.residual_multiplier

        return mlp_output, attn_res


class FlashLlamaModel(torch.nn.Module):
    def __init__(self, prefix, config, weights):
        super().__init__()

        process_group = weights.process_group
        self.tp_rank = process_group.rank()
        self.tp_world_size = process_group.size()

        # Skip fp8 quant for first and last layers
        self.layers = nn.ModuleList()
        self.cross_attention_layers = getattr(config, "cross_attention_layers", [])
        # Setting defaults for baichuan custom config which doesn't apply them.
        config.rope_theta = getattr(config, "rope_theta", 10000)
        config.num_key_value_heads = getattr(
            config, "num_key_value_heads", config.num_attention_heads
        )
        rotary_emb = PositionRotaryEmbedding.static(
            config=config,
            dim=config.hidden_size // config.num_attention_heads,
            base=config.rope_theta,
            device=weights.device,
        )
        with no_fp8(weights):
            self.layers.append(
                FlashLlamaLayer(
                    index=0,
                    prefix=f"{prefix}.layers.0",
                    config=config,
                    weights=weights,
                    rotary_emb=rotary_emb,
                )
            )

        # Skip first and last layers
        for layer_id in range(1, config.num_hidden_layers - 1):
            if layer_id in self.cross_attention_layers:
                from text_generation_server.models.custom_modeling.flash_mllama import (
                    FlashLlamaCrossLayer,
                )

                self.layers.append(
                    FlashLlamaCrossLayer(
                        index=layer_id,
                        prefix=(f"{prefix}.layers.{layer_id}"),
                        config=config,
                        weights=weights,
                    )
                )
            else:
                self.layers.append(
                    FlashLlamaLayer(
                        index=layer_id,
                        prefix=(f"{prefix}.layers.{layer_id}"),
                        config=config,
                        weights=weights,
                        rotary_emb=rotary_emb,
                    )
                )

        with no_fp8(weights):
            last_layer_id = config.num_hidden_layers - 1
            self.layers.append(
                FlashLlamaLayer(
                    index=last_layer_id,
                    prefix=(f"{prefix}.layers.{last_layer_id}"),
                    config=config,
                    weights=weights,
                    rotary_emb=rotary_emb,
                )
            )

        self.norm = FastRMSNorm.load(
            prefix=f"{prefix}.norm",
            weights=weights,
            eps=config.rms_norm_eps,
        )

        self.gradient_checkpointing = False

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads
        self.num_key_value_heads = self.layers[0].self_attn.num_key_value_heads

    def forward(
        self,
        inputs_embeds: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        adapter_data,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
        cross_attention_states=None,
    ) -> torch.Tensor:
        if hpu_attention_meta is not None:
            hpu_attention_meta = set_block_mapping(
                hpu_attention_meta, inputs_embeds.shape[0]
            )

        hidden_states = inputs_embeds

        # Get rotary cos and sin for this forward
        # Avoid to index in each layer
        cos, sin = self.layers[0].self_attn.rotary_emb.get_cos_sin(position_ids)

        residual = None
        lazy_mode = htorch.utils.internal.is_lazy()
        if lazy_mode:
            htorch.core.mark_step()
        for i, layer in enumerate(self.layers):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cos,
                sin,
                cu_seqlen_prefill,
                kv_cache[i],
                slots,
                seqlen,
                adapter_data,
                cross_attention_states,
                hpu_attention_meta=hpu_attention_meta,
            )
            if lazy_mode:
                htorch.core.mark_step()

        hidden_states, _ = self.norm(hidden_states, residual)

        return hidden_states


class FlashLlamaForCausalLM(torch.nn.Module):
    def __init__(self, prefix: str, config, weights, name=None):
        if name is None:
            name = "model"
        super().__init__()
        with no_fp8(weights):
            self.embed_tokens = TensorParallelEmbedding(
                prefix=(
                    f"{name}.embed_tokens"
                    if not prefix
                    else f"{prefix}.{name}.embed_tokens"
                ),
                weights=weights,
            )
        self.model = FlashLlamaModel(
            prefix=name if not prefix else f"{prefix}.{name}",
            config=config,
            weights=weights,
        )
        if config.tie_word_embeddings:
            suffix = "model.embed_tokens"
        else:
            suffix = "lm_head"

        # Used in Granite
        embedding_multiplier = getattr(config, "embedding_multiplier", None)
        if embedding_multiplier is not None:
            self.embed_tokens.weight.data *= embedding_multiplier
        prefix = suffix if not prefix or name != "model" else f"{prefix}.{suffix}"
        with no_fp8(weights):
            self.lm_head = SpeculativeHead.load(
                config,
                prefix,
                weights,
            )

        # Used in Granite
        self.logits_scaling = getattr(config, "logits_scaling", None)
        if self.logits_scaling is not None and self.lm_head.head is not None:
            try:
                # Scale the weights directly
                self.lm_head.head.linear.weight.data /= self.logits_scaling
                self.logits_scaled = True
            except Exception:
                self.logits_scaled = False

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        slots: torch.Tensor,
        seqlen: Seqlen,
        hpu_attention_meta: Optional[HPUPagedAttentionMetadata],
        lm_head_indices: Optional[torch.Tensor] = None,
        adapter_data: Optional[torch.Tensor] = None,
        cross_attention_states=None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        inputs_embeds = self.embed_tokens(input_ids)
        hidden_states = self.model(
            inputs_embeds,
            position_ids,
            cu_seqlen_prefill,
            kv_cache,
            slots,
            seqlen,
            adapter_data=adapter_data,
            cross_attention_states=cross_attention_states,
            hpu_attention_meta=hpu_attention_meta,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits, speculative_logits = self.lm_head(hidden_states)

        # Used in Granite
        if self.logits_scaling is not None and not self.logits_scaled:
            logits /= self.logits_scaling
            if speculative_logits is not None:
                speculative_logits /= self.logits_scaling

        return logits, speculative_logits
