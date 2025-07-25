# ruff: noqa: F821
# the above line disables the `undefined-name` rule for the model type variables

from compressed_tensors.compressors.model_compressors.model_compressor import (
    QuantizationConfig,
)
from compressed_tensors.quantization import QuantizationType
from pydantic import ValidationError
import enum
import os
from typing import Optional, List, Dict
from pathlib import Path
from loguru import logger

import torch
import transformers
from transformers.configuration_utils import PretrainedConfig
from transformers.models.auto import modeling_auto
from transformers.dynamic_module_utils import get_class_from_dynamic_module
from huggingface_hub import hf_hub_download, HfApi

from text_generation_server.utils.speculate import get_speculate, set_speculate
from text_generation_server.models.model import Model
from text_generation_server.models.causal_lm import CausalLM, CausalLMBatchKeysLast

from text_generation_server.models.custom_modeling.opt_modeling import OPTForCausalLM
from text_generation_server.models.custom_modeling.mpt_modeling import (
    MPTForCausalLM,
)
from text_generation_server.models.bloom import BloomCausalLMBatch
from text_generation_server.models.custom_modeling.bloom_modeling import (
    BloomForCausalLM,
)
from text_generation_server.models.globals import ATTENTION
from text_generation_server.models.seq2seq_lm import Seq2SeqLM
from text_generation_server.models.galactica import GalacticaCausalLMBatch
from text_generation_server.models.custom_modeling.neox_modeling import (
    GPTNeoxForCausalLM,
)
from text_generation_server.models.custom_modeling.phi_modeling import (
    PhiConfig,
    PhiForCausalLM,
)
from text_generation_server.models.custom_modeling.flash_phi_moe_modeling import (
    PhiMoEConfig,
)
from text_generation_server.models.custom_modeling.t5_modeling import (
    T5ForConditionalGeneration,
)


from text_generation_server.utils.adapter import (
    AdapterParameters,
    build_layer_weight_lookup,
    load_and_merge_adapters,
    AdapterInfo,
)
from text_generation_server.adapters.lora import LoraWeights


from text_generation_server.utils.import_utils import SYSTEM
from text_generation_server.utils.log import log_master

# The flag below controls whether to allow TF32 on matmul. This flag defaults to False
# in PyTorch 1.12 and later.
torch.backends.cuda.matmul.allow_tf32 = True

# The flag below controls whether to allow TF32 on cuDNN. This flag defaults to True.
torch.backends.cudnn.allow_tf32 = True

# Disable gradients
torch.set_grad_enabled(False)

__all__ = [
    "Model",
    "CausalLM",
    "Seq2SeqLM",
    "get_model_with_lora_adapters",
]

FLASH_ATT_ERROR_MESSAGE = "{} requires Flash Attention enabled models."

FLASH_ATTENTION = True

try:
    from text_generation_server.models.flash_causal_lm import FlashCausalLM
    from text_generation_server.models.vlm_causal_lm import VlmCausalLM
    from text_generation_server.models.mllama_causal_lm import MllamaCausalLM
    from text_generation_server.models.custom_modeling.flash_deepseek_v2_modeling import (
        FlashDeepseekV2ForCausalLM,
        DeepseekV2Config,
    )
    from text_generation_server.models.custom_modeling.flash_deepseek_v3_modeling import (
        FlashDeepseekV3ForCausalLM,
        DeepseekV3Config,
    )
    from text_generation_server.models.custom_modeling.flash_llama_modeling import (
        FlashLlamaForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_cohere_modeling import (
        FlashCohereForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_gemma_modeling import (
        FlashGemmaForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_gemma2_modeling import (
        FlashGemma2ForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_gemma3_modeling import (
        FlashGemma3ForCausalLM,
        Gemma3ForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.gemma3.processing_gemma3 import (
        Gemma3Processor,
    )
    from text_generation_server.models.custom_modeling.gemma3.configuration_gemma3 import (
        Gemma3Config,
        Gemma3TextConfig,
    )
    from text_generation_server.models.custom_modeling.flash_dbrx_modeling import (
        FlashDbrxForCausalLM,
        DbrxConfig,
    )
    from text_generation_server.models.custom_modeling.flash_rw_modeling import (
        RWConfig,
        FlashRWForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_neox_modeling import (
        FlashGPTNeoXForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_pali_gemma_modeling import (
        PaliGemmaForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.flash_phi_modeling import (
        FlashPhiForCausalLM,
    )
    from text_generation_server.models.idefics_causal_lm import IdeficsCausalLM
    from text_generation_server.models.mllama_causal_lm import MllamaCausalLMBatch
    from text_generation_server.models.custom_modeling.mllama import (
        MllamaForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.llava_next import (
        LlavaNextForConditionalGeneration,
    )

    from text_generation_server.models.custom_modeling.flash_santacoder_modeling import (
        FlashSantacoderForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_starcoder2_modeling import (
        FlashStarcoder2ForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_qwen2_modeling import (
        Qwen2ForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_qwen3_modeling import (
        Qwen3ForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_mistral_modeling import (
        FlashMistralForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_mixtral_modeling import (
        FlashMixtralForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_gpt2_modeling import (
        FlashGPT2ForCausalLM,
    )
    from text_generation_server.models.custom_modeling.flash_gptj_modeling import (
        FlashGPTJForCausalLM,
    )
    from text_generation_server.models.custom_modeling.idefics2 import (
        Idefics2ForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.idefics3 import (
        Idefics3ForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.qwen2_vl import (
        Qwen2VLForConditionalGeneration,
    )
    from text_generation_server.models.custom_modeling.qwen2_5_vl import (
        Qwen2_5VLForConditionalGeneration,
        Qwen2_5_VLConfig,
        Qwen2_5_VLProcessor,
    )
    from text_generation_server.layers.attention import SUPPORTS_WINDOWING
except ImportError as e:
    log_master(logger.warning, f"Could not import Flash Attention enabled models: {e}")
    SUPPORTS_WINDOWING = False
    FLASH_ATTENTION = False

if FLASH_ATTENTION:
    __all__.append(FlashCausalLM)
    __all__.append(IdeficsCausalLM)

MAMBA_AVAILABLE = True
try:
    from text_generation_server.models.mamba import Mamba
except ImportError as e:
    log_master(logger.warning, f"Could not import Mamba: {e}")
    MAMBA_AVAILABLE = False

if MAMBA_AVAILABLE:
    __all__.append(Mamba)

FLASH_TRANSFORMERS_BACKEND = torch.cuda.is_available() or SYSTEM == "ipex"

try:
    from text_generation_server.models.transformers_flash_causal_lm import (
        TransformersFlashCausalLM,
    )
    from text_generation_server.models.transformers_flash_vlm import (
        TransformersFlashVlmCausalLM,
        TransformersGemma3VlmCausalLM,
        TransformersLlama4VlmCausalLM,
    )
except ImportError as e:
    log_master(logger.warning, f"Could not import Flash Transformers Backend: {e}")
    FLASH_TRANSFORMERS_BACKEND = False


class ModelType(enum.Enum):
    DEEPSEEK_V2 = {
        "type": "deepseek_v2",
        "name": "Deepseek V2",
        "url": "https://huggingface.co/deepseek-ai/DeepSeek-V2",
    }
    DEEPSEEK_V3 = {
        "type": "deepseek_v3",
        "name": "Deepseek V3",
        "url": "https://huggingface.co/deepseek-ai/DeepSeek-V3",
    }
    IDEFICS2 = {
        "type": "idefics2",
        "name": "Idefics 2",
        "url": "https://huggingface.co/HuggingFaceM4/idefics2-8b",
        "multimodal": True,
    }
    IDEFICS3 = {
        "type": "idefics3",
        "name": "Idefics 3",
        "url": "https://huggingface.co/HuggingFaceM4/Idefics3-8B-Llama3",
        "multimodal": True,
    }
    LLAVA_NEXT = {
        "type": "llava_next",
        "name": "Llava Next (1.6)",
        "url": "https://huggingface.co/llava-hf/llava-v1.6-vicuna-13b-hf",
        "multimodal": True,
    }
    LLAMA = {
        "type": "llama",
        "name": "Llama",
        "url": "https://huggingface.co/collections/meta-llama/llama-31-669fc079a0c406a149a5738f",
    }
    LLAMA4 = {
        "type": "llama4",
        "name": "Llama4",
        "url": "https://huggingface.co/collections/meta-llama/llama-31-669fc079a0c406a149a5738f",
    }
    PHI3 = {
        "type": "phi3",
        "name": "Phi 3",
        "url": "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct",
    }
    GRANITE = {
        "type": "granite",
        "name": "Granite",
        "url": "https://huggingface.co/ibm-granite/granite-3.0-8b-instruct",
    }
    GEMMA = {
        "type": "gemma",
        "name": "Gemma",
        "url": "https://huggingface.co/google/gemma-7b",
    }
    PALIGEMMA = {
        "type": "paligemma",
        "name": "PaliGemma",
        "url": "https://huggingface.co/google/paligemma-3b-pt-224",
    }
    GEMMA2 = {
        "type": "gemma2",
        "name": "Gemma2",
        "url": "https://huggingface.co/collections/google/gemma-2-release-667d6600fd5220e7b967f315",
    }
    GEMMA3 = {
        "type": "gemma3",
        "name": "Gemma3",
        "url": "https://huggingface.co/collections/google/gemma-3-release-67c6c6f89c4f76621268bb6d",
    }
    GEMMA3_TEXT = {
        "type": "gemma3_text",
        "name": "Gemma3 Text",
        "url": "https://huggingface.co/collections/google/gemma-3-release-67c6c6f89c4f76621268bb6d",
    }
    COHERE = {
        "type": "cohere",
        "name": "Cohere",
        "url": "https://huggingface.co/CohereForAI/c4ai-command-r-plus",
    }
    DBRX = {
        "type": "dbrx",
        "name": "Dbrx",
        "url": "https://huggingface.co/databricks/dbrx-instruct",
    }
    MAMBA = {
        "type": "mamba",
        "name": "Mamba",
        "url": "https://huggingface.co/state-spaces/mamba-2.8b-slimpj",
    }
    MISTRAL = {
        "type": "mistral",
        "name": "Mistral",
        "url": "https://huggingface.co/mistralai/Mistral-Nemo-Instruct-2407",
    }
    MIXTRAL = {
        "type": "mixtral",
        "name": "Mixtral",
        "url": "https://huggingface.co/mistralai/Mixtral-8x22B-Instruct-v0.1",
    }
    GPT_BIGCODE = {
        "type": "gpt_bigcode",
        "name": "Gpt Bigcode",
        "url": "https://huggingface.co/bigcode/gpt_bigcode-santacoder",
    }
    PHI = {
        "type": "phi",
        "name": "Phi",
        "url": "https://huggingface.co/microsoft/phi-1_5",
    }
    PHI_MOE = {
        "type": "phimoe",
        "name": "PhiMoe",
        "url": "https://huggingface.co/microsoft/Phi-3.5-MoE-instruct",
    }
    BAICHUAN = {
        "type": "baichuan",
        "name": "Baichuan",
        "url": "https://huggingface.co/baichuan-inc/Baichuan2-7B-Chat",
    }
    FALCON = {
        "type": "falcon",
        "name": "Falcon",
        "url": "https://huggingface.co/tiiuae/falcon-7b-instruct",
    }
    STARCODER2 = {
        "type": "starcoder2",
        "name": "StarCoder 2",
        "url": "https://huggingface.co/bigcode/starcoder2-15b-instruct-v0.1",
    }
    QWEN2 = {
        "type": "qwen2",
        "name": "Qwen 2",
        "url": "https://huggingface.co/collections/Qwen/qwen2-6659360b33528ced941e557f",
    }
    QWEN3 = {
        "type": "qwen3",
        "name": "Qwen 3",
        "url": "https://huggingface.co/collections/Qwen/qwen3-67c6c6f89c4f76621268bb6d",
    }
    QWEN2_VL = {
        "type": "qwen2_vl",
        "name": "Qwen 2 VL",
        "url": "https://huggingface.co/collections/Qwen/qwen2-vl-66cee7455501d7126940800d",
    }
    QWEN2_5_VL = {
        "type": "qwen2_5_vl",
        "name": "Qwen 2.5 VL",
        "url": "https://huggingface.co/collections/Qwen/qwen25-66e81a666513e518adb90d9e",
    }
    OPT = {
        "type": "opt",
        "name": "Opt",
        "url": "https://huggingface.co/facebook/opt-6.7b",
    }
    T5 = {
        "type": "t5",
        "name": "T5",
        "url": "https://huggingface.co/google/flan-t5-xxl",
    }
    GALACTICA = {
        "type": "galactica",
        "name": "Galactica",
        "url": "https://huggingface.co/facebook/galactica-120b",
    }
    SANTACODER = {
        "type": "santacoder",
        "name": "SantaCoder",
        "url": "https://huggingface.co/bigcode/santacoder",
    }
    BLOOM = {
        "type": "bloom",
        "name": "Bloom",
        "url": "https://huggingface.co/bigscience/bloom-560m",
    }
    MPT = {
        "type": "mpt",
        "name": "Mpt",
        "url": "https://huggingface.co/mosaicml/mpt-7b-instruct",
    }
    GPT2 = {
        "type": "gpt2",
        "name": "Gpt2",
        "url": "https://huggingface.co/openai-community/gpt2",
    }
    GPT_NEOX = {
        "type": "gpt_neox",
        "name": "Gpt Neox",
        "url": "https://huggingface.co/EleutherAI/gpt-neox-20b",
    }
    GPTJ = {
        "type": "gptj",
        "name": "Gptj",
        "url": "https://huggingface.co/EleutherAI/gpt-j-6b",
    }
    IDEFICS = {
        "type": "idefics",
        "name": "Idefics",
        "url": "https://huggingface.co/HuggingFaceM4/idefics-9b",
        "multimodal": True,
    }
    MLLAMA = {
        "type": "mllama",
        "name": "Mllama",
        "url": "https://huggingface.co/meta-llama/Llama-3.2-11B-Vision-Instruct",
        "multimodal": True,
    }


__GLOBALS = locals()
for data in ModelType:
    __GLOBALS[data.name] = data.value["type"]


def get_model(
    model_id: str,
    lora_adapter_ids: Optional[List[str]],
    revision: Optional[str],
    sharded: bool,
    quantize: Optional[str],
    speculate: Optional[int],
    dtype: Optional[str],
    kv_cache_dtype: Optional[str],
    trust_remote_code: bool,
    max_input_tokens: int,
) -> Model:
    global FLASH_ATTENTION

    config_dict, _ = PretrainedConfig.get_config_dict(
        model_id, revision=revision, trust_remote_code=trust_remote_code
    )
    model_type = config_dict.get("model_type", None)

    quantization_config = config_dict.get("quantization_config", None)
    if quantization_config is None:
        quantization_config = config_dict.get("compression_config", None)
    if quantization_config is not None and quantize is None:
        method = quantization_config.get("quant_method", None)
        if method in {"gptq", "awq", "exl2"}:
            log_master(logger.info, f"Auto selecting quantization method {method}")
            quantize = method
        elif method == "fbgemm_fp8" or method == "fp8":
            log_master(logger.info, "Auto selecting quantization method fp8")
            quantize = "fp8"
        if method == "compressed-tensors":
            log_master(
                logger.info, "Auto selecting quantization method compressed-tensors"
            )
            quantize = "compressed-tensors"

        else:
            log_master(logger.warning, f"Unknown quantization method {method}")

    if dtype is None:
        if quantize in ["awq", "exl2", "gptq", "marlin"]:
            if SYSTEM == "ipex" and not (
                hasattr(torch, "xpu") and torch.xpu.is_available()
            ):
                dtype = torch.bfloat16
            else:
                # These quantizers only work with float16 params.
                dtype = torch.float16
        else:
            # Keep it as default for now and let
            # every model resolve their own default dtype.
            dtype = None
    elif dtype == "float16":
        dtype = torch.float16
    elif dtype == "bfloat16":
        dtype = torch.bfloat16
    else:
        raise RuntimeError(f"Unknown dtype {dtype}")

    compressed_tensors_config = None
    if quantize == "compressed-tensors":
        try:
            compressed_tensors_config = QuantizationConfig.model_validate(
                quantization_config
            )
        except ValidationError as e:
            raise ValueError("Cannot parse compressed-tensors configuration") from e

    if kv_cache_dtype is None:
        kv_cache_scheme = (
            compressed_tensors_config.kv_cache_scheme
            if isinstance(compressed_tensors_config, QuantizationConfig)
            else None
        )
        if (
            kv_cache_scheme is not None
            and kv_cache_scheme.type == QuantizationType.FLOAT
            and kv_cache_scheme.num_bits == 8
            and SYSTEM == "cuda"
            and ATTENTION == "flashinfer"
        ):
            kv_cache_dtype = torch.float8_e4m3fn
        else:
            kv_cache_dtype = dtype
    elif kv_cache_dtype == "fp8_e4m3fn":
        kv_cache_dtype = torch.float8_e4m3fn
    elif kv_cache_dtype == "fp8_e5m2":
        kv_cache_dtype = torch.float8_e5m2
    else:
        raise RuntimeError(f"Unknown kv_cache_dtype: {kv_cache_dtype}")

    if speculate is not None:
        set_speculate(speculate)
    else:
        set_speculate(0)

    speculator = None
    if "medusa_num_heads" in config_dict:
        medusa_model_id = model_id
        medusa_revision = revision
        model_id = config_dict["base_model_name_or_path"]
        revision = "main"
        speculate_medusa = config_dict["medusa_num_heads"]
        if speculate is not None:
            if speculate > speculate_medusa:
                raise RuntimeError(
                    f"Speculate is set to `{speculate}` but this medusa models only has `{speculate_medusa}` heads, please make them match"
                )
            else:
                set_speculate(speculate)
        else:
            set_speculate(speculate_medusa)

        config_dict, _ = PretrainedConfig.get_config_dict(
            model_id, revision=revision, trust_remote_code=trust_remote_code
        )
        # Reload model type from parent.
        model_type = config_dict.get("model_type", None)
        is_local = Path(medusa_model_id).exists()
        if not is_local:
            medusa_config = hf_hub_download(
                medusa_model_id, revision=medusa_revision, filename="config.json"
            )
            hf_hub_download(
                medusa_model_id,
                revision=medusa_revision,
                filename="medusa_lm_head.safetensors",
            )
            speculator = {
                "path": Path(medusa_config).parent,
                "model_paths": ["medusa_lm_head.safetensors"],
            }
        else:
            speculator = {
                "path": Path(medusa_model_id),
                "model_paths": ["medusa_lm_head.safetensors"],
            }

        method = "medusa"
    elif model_type == "mlp_speculator":
        mlp_model_id = model_id
        mlp_revision = revision
        model_id = config_dict["base_model_name_or_path"]
        revision = "main"
        speculate_mlp = config_dict["n_predict"]
        if speculate is not None:
            if speculate > speculate_mlp:
                raise RuntimeError(
                    f"Speculate is set to `{speculate}` but this mlp_speculator models only has `{speculate_mlp}` heads, please make them match"
                )
            else:
                set_speculate(speculate)
        else:
            set_speculate(speculate_mlp)

        config_dict, _ = PretrainedConfig.get_config_dict(
            model_id, revision=revision, trust_remote_code=trust_remote_code
        )
        # Reload model type from parent.
        model_type = config_dict.get("model_type", None)
        is_local = Path(mlp_model_id).exists()
        extension = ".safetensors"
        if not is_local:
            mlp_speculator_config = hf_hub_download(
                mlp_model_id, revision=mlp_revision, filename="config.json"
            )
            api = HfApi()
            info = api.model_info(mlp_model_id, revision=mlp_revision)
            filenames = [
                s.rfilename
                for s in info.siblings
                if s.rfilename.endswith(extension)
                and len(s.rfilename.split("/")) == 1
                and "arguments" not in s.rfilename
                and "args" not in s.rfilename
                and "training" not in s.rfilename
            ]
            for filename in filenames:
                hf_hub_download(
                    mlp_model_id,
                    revision=mlp_revision,
                    filename=filename,
                )
            speculator_dir_path = Path(mlp_speculator_config).parent
            # if these are downloaded, they get converted to safetensors
            filenames.extend(
                [p for p in os.listdir(speculator_dir_path) if p.endswith(extension)]
            )
            speculator = {
                "path": Path(mlp_speculator_config).parent,
                "model_paths": filenames,
            }
        else:
            speculator = Path(mlp_model_id)
            filenames = [p for p in os.listdir(speculator) if p.endswith(extension)]
            speculator = {"path": speculator, "model_paths": filenames}
        method = "mlp_speculator"
    else:
        method = "n-gram"

    speculate = get_speculate()
    if speculate > 0:
        log_master(
            logger.info, f"Using speculation {method} with {speculate} input ids."
        )

    if model_type is None:
        # TODO: fix how we determine model type for Mamba
        if "ssm_cfg" in config_dict:
            # *only happens in Mamba case
            model_type = "mamba"
        else:
            raise RuntimeError(
                f"Could not determine model type for {model_id} revision {revision}"
            )

    if quantize == "exl2" and sharded:
        raise RuntimeError(
            "Sharding is currently not supported with `exl2` quantization"
        )

    sliding_window = (
        config_dict.get("sliding_window")
        if config_dict.get("sliding_window") is not None
        else -1
    )

    use_sliding_window = sliding_window is not None and sliding_window != -1
    needs_sliding_window = (
        max_input_tokens is not None and max_input_tokens > sliding_window
    )
    if use_sliding_window and needs_sliding_window and not SUPPORTS_WINDOWING:
        raise ValueError(
            f"The backend {SYSTEM} does not support sliding window attention that is used by the model type {model_type}. To use this model nonetheless with the {SYSTEM} backend, please launch TGI with the argument `--max-input-tokens` smaller than sliding_window={sliding_window} (got here max_input_tokens={max_input_tokens})."
        )
    if model_type == DEEPSEEK_V2:
        if FLASH_ATTENTION:
            head_size = max(
                config_dict.get("qk_nope_dim", 128)
                + config_dict.get("qk_rope_dim", 64),
                config_dict.get("v_head_dim", 128),
            )
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashDeepseekV2ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                default_dtype=torch.bfloat16,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                config_class=DeepseekV2Config,
                head_size=head_size,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format("Sharded Deepseek V2")
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == DEEPSEEK_V3:
        if FLASH_ATTENTION:
            head_size = max(
                config_dict.get("qk_nope_dim", 128)
                + config_dict.get("qk_rope_dim", 64),
                config_dict.get("v_head_dim", 128),
            )
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashDeepseekV3ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                default_dtype=torch.bfloat16,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                config_class=DeepseekV3Config,
                head_size=head_size,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format("Sharded Deepseek V3")
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == MAMBA:
        return Mamba(
            model_id,
            revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
        )
    elif model_type == "ssm":
        raise RuntimeError(
            "`ssm` models have been deprecated in favor of `mamba` models, which follow standard HF formats. Check out a list here: https://huggingface.co/models?search=mamba%20-hf"
        )

    if model_id.startswith("facebook/galactica"):
        return CausalLM(
            model_id=model_id,
            # Yes galactica is just an OPT model.
            model_class=OPTForCausalLM,
            revision=revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
            batch_class=GalacticaCausalLMBatch,
        )

    if (
        model_type == GPT_BIGCODE
        or model_type == GPT2
        and model_id.startswith("bigcode/")
    ):
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashSantacoderForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                aliases={"transformer.wte.weight": ["lm_head.weight"]},
                num_kv_heads=1,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format("Sharded Santacoder")
            )
        else:
            return CausalLM.fallback(
                model_id=model_id,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == BLOOM:
        return CausalLM(
            model_id=model_id,
            model_class=BloomForCausalLM,
            revision=revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
            batch_class=BloomCausalLMBatch,
        )
    elif model_type == MPT:
        return CausalLM(
            model_id=model_id,
            model_class=MPTForCausalLM,
            revision=revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
            batch_class=CausalLMBatchKeysLast,
        )
    elif model_type == GPT2:
        if FLASH_ATTENTION:
            try:
                return FlashCausalLM(
                    model_id=model_id,
                    model_class=FlashGPT2ForCausalLM,
                    revision=revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    kv_cache_dtype=kv_cache_dtype,
                    trust_remote_code=trust_remote_code,
                    lora_adapter_ids=lora_adapter_ids,
                )
            except RuntimeError as e:
                # Lots of legacy models with various weight names.
                log_master(logger.warning, f"Couldn't load flash gpt2 variant: {e}")
                return CausalLM.fallback(
                    model_id,
                    revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    trust_remote_code=trust_remote_code,
                )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded GPT-2"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == GPTJ:
        if FLASH_ATTENTION:
            try:
                return FlashCausalLM(
                    model_id=model_id,
                    model_class=FlashGPTJForCausalLM,
                    revision=revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    kv_cache_dtype=kv_cache_dtype,
                    trust_remote_code=trust_remote_code,
                    lora_adapter_ids=lora_adapter_ids,
                )
            except RuntimeError as e:
                # Lots of legacy models with various weight names.
                log_master(logger.warning, f"Couldn't load flash gptj variant: {e}")
                return CausalLM.fallback(
                    model_id,
                    revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    trust_remote_code=trust_remote_code,
                )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded GPT-J"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == GPT_NEOX:
        if FLASH_ATTENTION:
            from text_generation_server.models.custom_modeling.flash_neox_modeling import (
                GPTNeoXConfig,
            )

            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashGPTNeoXForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                config_class=GPTNeoXConfig,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            return CausalLM(
                model_id=model_id,
                model_class=GPTNeoxForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    elif model_type == PHI:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashPhiForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        else:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    elif model_type == PHI_MOE:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashLlamaForCausalLM,
                config_class=PhiMoEConfig,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    elif model_type == "phi-msft":
        if FLASH_ATTENTION:
            raise NotImplementedError(
                "Legacy phi-msft is not supported with Flash Attention"
            )
        else:
            return CausalLM(
                model_id=model_id,
                model_class=PhiForCausalLM,
                config_class=PhiConfig,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    elif model_type == LLAMA or model_type == PHI3 or model_type == GRANITE:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashLlamaForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format(f"Sharded {model_type}")
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == LLAMA4:
        if FLASH_TRANSFORMERS_BACKEND:
            from transformers import Llama4ForConditionalGeneration as Llama4Model

            return TransformersLlama4VlmCausalLM.fallback(
                model_id,
                Llama4Model,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                processor_kwargs={
                    "use_fast": True,
                },
            )
    elif model_type == BAICHUAN:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashLlamaForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format(f"Sharded {model_type}")
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == GEMMA:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashGemmaForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # Works better for these models
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Gemma"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == GEMMA2:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashGemma2ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # Works better for these models
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Gemma2"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == GEMMA3_TEXT:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashGemma3ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # TODO: once implemented in transformers, use the config class
                # and processor class from there.
                config_class=Gemma3TextConfig,
                # Works better for these models
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Gemma3"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
    elif model_type == GEMMA3:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=Gemma3ForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # TODO: once implemented in transformers, use the config class
                # and processor class from there.
                config_class=Gemma3Config,
                processor_class=Gemma3Processor,
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                support_chunking=False,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            from transformers import Gemma3ForConditionalGeneration as Gemma3Model

            return TransformersGemma3VlmCausalLM.fallback(
                model_id,
                Gemma3Model,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                support_chunking=False,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Gemma3"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == COHERE:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashCohereForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Cohere"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == DBRX:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashDbrxForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # Dbrx works better in bfloat16.
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                config_class=DbrxConfig,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded DBRX"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type in ["RefinedWeb", "RefinedWebModel", FALCON]:
        if sharded:
            if FLASH_ATTENTION:
                if config_dict.get("alibi", False):
                    raise NotImplementedError("sharded is not supported for this model")
                return FlashCausalLM(
                    model_id=model_id,
                    model_class=FlashRWForCausalLM,
                    revision=revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    kv_cache_dtype=kv_cache_dtype,
                    aliases={
                        "lm_head.weight": ["transformer.word_embeddings.weight"],
                        "transformer.word_embeddings.weight": ["lm_head.weight"],
                    },
                    trust_remote_code=trust_remote_code,
                    lora_adapter_ids=lora_adapter_ids,
                    config_class=RWConfig,
                )
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Falcon"))
        else:
            if FLASH_ATTENTION and not config_dict.get("alibi", False):
                return FlashCausalLM(
                    model_id=model_id,
                    model_class=FlashRWForCausalLM,
                    revision=revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    kv_cache_dtype=kv_cache_dtype,
                    aliases={
                        "lm_head.weight": ["transformer.word_embeddings.weight"],
                        "transformer.word_embeddings.weight": ["lm_head.weight"],
                    },
                    trust_remote_code=trust_remote_code,
                    lora_adapter_ids=lora_adapter_ids,
                    config_class=RWConfig,
                )
            else:
                return CausalLM.fallback(
                    model_id,
                    revision,
                    quantize=quantize,
                    speculator=speculator,
                    dtype=dtype,
                    trust_remote_code=trust_remote_code,
                )

    if model_type == MISTRAL:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashMistralForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Mistral"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == MIXTRAL:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashMixtralForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Mixtral"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == STARCODER2:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=FlashStarcoder2ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(
                FLASH_ATT_ERROR_MESSAGE.format("Sharded Starcoder2")
            )
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == QWEN2:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=Qwen2ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Qwen2"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == QWEN3:
        if FLASH_ATTENTION:
            return FlashCausalLM(
                model_id=model_id,
                model_class=Qwen3ForCausalLM,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Sharded Qwen3"))
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    if model_type == OPT:
        return CausalLM(
            model_id=model_id,
            model_class=OPTForCausalLM,
            revision=revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
        )

    if model_type == T5:
        return Seq2SeqLM(
            model_id=model_id,
            model_class=T5ForConditionalGeneration,
            revision=revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
            aliases={
                "shared.weight": [
                    "encoder.embed_tokens.weight",
                    "decoder.embed_tokens.weight",
                ]
            },
        )
    if model_type == IDEFICS:
        if FLASH_ATTENTION:
            return IdeficsCausalLM(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Idefics"))
    if model_type == QWEN2_VL:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=Qwen2VLForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                default_dtype=torch.bfloat16,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                # TODO: Fix bug in rust image_text_replacement implementation
                support_chunking=False,
            )
        # TODO: Uncomment when transformers is refactored
        # elif FLASH_TRANSFORMERS_BACKEND:
        #     from transformers import Qwen2VLForConditionalGeneration as Qwen2VLModel

        #     return TransformersQwen2VlmCausalLM.fallback(
        #         model_id,
        #         Qwen2VLModel,
        #         revision,
        #         quantize=quantize,
        #         speculator=speculator,
        #         dtype=torch.bfloat16,
        #         trust_remote_code=trust_remote_code,
        #     )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Qwen2_VL"))
    if model_type == QWEN2_5_VL:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=Qwen2_5VLForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                default_dtype=torch.bfloat16,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                config_class=Qwen2_5_VLConfig,
                processor_class=Qwen2_5_VLProcessor,
                # TODO: Fix bug in rust image_text_replacement implementation
                support_chunking=False,
            )
        # TODO: Uncomment when transformers is refactored
        # elif FLASH_TRANSFORMERS_BACKEND:
        #     return TransformersQwen2VlmCausalLM.fallback(
        #         model_id,
        #         Qwen2VLModel,
        #         revision,
        #         quantize=quantize,
        #         speculator=speculator,
        #         dtype=torch.bfloat16,
        #         trust_remote_code=trust_remote_code,
        #         config_class=Qwen2_5_VLConfig,
        #         processor_class=Qwen2_5_VLProcessor,
        #     )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Qwen2_5_VL"))
    if model_type == MLLAMA:
        if FLASH_ATTENTION:
            return MllamaCausalLM(
                model_id=model_id,
                model_class=MllamaForConditionalGeneration,
                batch_class=MllamaCausalLMBatch,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                support_chunking=False,
            )
        # TODO: Uncomment when transformers is refactored and cross attn is added
        # elif FLASH_TRANSFORMERS_BACKEND:
        #     from transformers import MllamaForConditionalGeneration as MllamaModel

        #     return TransformersFlashVlmCausalLM.fallback(
        #         model_id,
        #         MllamaModel,
        #         revision,
        #         quantize=quantize,
        #         speculator=speculator,
        #         dtype=torch.bfloat16,
        #         trust_remote_code=trust_remote_code,
        #         batch_class=MllamaCausalLMBatch,
        #     )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Mllama"))
    if model_type == IDEFICS2:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=Idefics2ForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                # XXX: Extremely important to cap resolution in order to limit
                # VRAM usage.
                processor_kwargs={"size": {"longest_edge": 448, "shortest_edge": 378}},
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            from transformers import Idefics2ForConditionalGeneration as Idefics2Model

            return TransformersFlashVlmCausalLM.fallback(
                model_id,
                Idefics2Model,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
                processor_kwargs={"size": {"longest_edge": 448, "shortest_edge": 378}},
            )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Idefics"))
    if model_type == IDEFICS3:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=Idefics3ForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
                # XXX: Extremely important to cap resolution in order to limit
                # VRAM usage.
                processor_kwargs={"size": {"longest_edge": 1456}},
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            from transformers import Idefics3ForConditionalGeneration as Idefics3Model

            return TransformersFlashVlmCausalLM.fallback(
                model_id,
                Idefics3Model,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                processor_kwargs={"size": {"longest_edge": 1456}},
            )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("Idefics"))
    if model_type == PALIGEMMA:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_id=model_id,
                model_class=PaliGemmaForConditionalGeneration,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                # Works better for these models
                default_dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
                lora_adapter_ids=lora_adapter_ids,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            from transformers import PaliGemmaForConditionalGeneration as PaliGemmaModel

            return TransformersFlashVlmCausalLM.fallback(
                model_id,
                PaliGemmaModel,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=torch.bfloat16,
                trust_remote_code=trust_remote_code,
            )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("PaliGemma"))
    if model_type == LLAVA_NEXT:
        if FLASH_ATTENTION:
            return VlmCausalLM(
                model_class=LlavaNextForConditionalGeneration,
                model_id=model_id,
                revision=revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                kv_cache_dtype=kv_cache_dtype,
                trust_remote_code=trust_remote_code,
            )
        elif FLASH_TRANSFORMERS_BACKEND:
            from transformers import LlavaNextForConditionalGeneration as LlavaNextModel

            return TransformersFlashVlmCausalLM.fallback(
                model_id,
                LlavaNextModel,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        else:
            raise NotImplementedError(FLASH_ATT_ERROR_MESSAGE.format("LlavaNext"))

    if quantize == "gptq":
        raise NotImplementedError(
            "gptq quantization is not supported for AutoModel, you can try to quantize it with `text-generation-server quantize ORIGINAL_MODEL_ID NEW_MODEL_ID`"
        )
    if quantize == "awq":
        raise NotImplementedError("awq quantization is not supported for AutoModel")
    elif (quantize == "bitsandbytes-fp4") or (quantize == "bitsandbytes-nf4"):
        raise NotImplementedError("4bit quantization is not supported for AutoModel")
    elif quantize == "eetq":
        raise NotImplementedError("Eetq quantization is not supported for AutoModel")
    elif quantize == "exl2":
        raise NotImplementedError("exl2 quantization is not supported for AutoModel")

    auto_map = config_dict.get("auto_map", None)
    model_class = None

    # If the model is already in the library
    if model_type in modeling_auto.MODEL_FOR_CAUSAL_LM_MAPPING_NAMES:
        model_class = getattr(
            transformers, modeling_auto.MODEL_FOR_CAUSAL_LM_MAPPING_NAMES[model_type]
        )
    elif (
        trust_remote_code
        and auto_map is not None
        and "AutoModelForCausalLM" in auto_map.keys()
    ):
        model_class = get_class_from_dynamic_module(
            config_dict["auto_map"]["AutoModelForCausalLM"], model_id
        )

    # This means the model is ForCausalLM
    if model_class is not None:
        if FLASH_TRANSFORMERS_BACKEND and model_class.is_backend_compatible():
            return TransformersFlashCausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
        elif sharded:
            raise NotImplementedError("sharded is not supported for AutoModel")
        else:
            return CausalLM.fallback(
                model_id,
                revision,
                quantize=quantize,
                speculator=speculator,
                dtype=dtype,
                trust_remote_code=trust_remote_code,
            )

    # Not supported at this point
    if sharded:
        raise NotImplementedError("sharded is not supported for AutoModel")

    # This means it is a ForSeq2SeqLM model
    if model_type in modeling_auto.MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING_NAMES or (
        trust_remote_code
        and auto_map is not None
        and "AutoModelForSeq2SeqLM" in auto_map.keys()
    ):
        return Seq2SeqLM.fallback(
            model_id,
            revision,
            quantize=quantize,
            speculator=speculator,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
        )

    raise ValueError(f"Unsupported model type {model_type}")


# get_model_with_lora_adapters wraps the internal get_model function and adds support for loading adapters
# this provides a post model loading hook to load adapters into the model after the model has been loaded
def get_model_with_lora_adapters(
    model_id: str,
    lora_adapters: Optional[List[AdapterInfo]],
    revision: Optional[str],
    sharded: bool,
    quantize: Optional[str],
    speculate: Optional[int],
    dtype: Optional[str],
    kv_cache_dtype: Optional[str],
    trust_remote_code: bool,
    max_input_tokens: int,
    adapter_to_index: Dict[str, int],
):
    lora_adapter_ids = [adapter.id for adapter in lora_adapters]
    model = get_model(
        model_id,
        lora_adapter_ids,
        revision,
        sharded,
        quantize,
        speculate,
        dtype,
        kv_cache_dtype,
        trust_remote_code,
        max_input_tokens,
    )

    if len(lora_adapters) > 0:
        target_to_layer = build_layer_weight_lookup(model.model)

        for index, adapter in enumerate(lora_adapters):
            # The AdapterParameters object allows for merging multiple adapters into a single adapter.
            # At the moment, we only support loading a single adapter into the model, but we keep the
            # AdapterParameters object for easier extension in the future.
            adapter_parameters = AdapterParameters(
                adapter_info=[adapter],
                # when merging multiple adapters we can weight them differently
                # if this is not set, all adapters will be weighted equally
                # see: text_generation_server.utils.merges.strategies for impl
                weights=None,
                merge_strategy=0,
                density=1.0,
                majority_sign_method=0,
            )

            adapter_index = index + 1
            adapter_to_index[adapter.id] = adapter_index

            logger.info(
                f"Loading adapter weights into model: {','.join([adapter.id for adapter in adapter_parameters.adapter_info])}"
            )
            weight_names = tuple([v[0] for v in target_to_layer.values()])
            (
                module_map,
                adapter_config,
                adapter_weight_names,
                adapter_tokenizer,
            ) = load_and_merge_adapters(
                model.model_id,
                adapter_parameters,
                adapter_index,
                weight_names,
                False,
            )

            unused_weight_names = adapter_weight_names.copy()

            adapter_layers = [
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
                "qkv_proj",
                # add c_* layers used in starcoder2
                "c_proj",
                "c_fc",
            ]

            for layer_name in adapter_layers:
                nlayers = (
                    1 if layer_name == "lm_head" else len(model.model.model.layers)
                )
                adapter_weights = LoraWeights.prepare_weights(
                    config=adapter_config,
                    module_map=module_map,
                    layer_type=layer_name,
                    unused_weight_names=unused_weight_names,
                    nlayers=nlayers,
                    dtype=model.dtype,
                    world_size=model.world_size,
                    process_group=model.process_group,
                    target_to_layer=target_to_layer,
                )

                if adapter_weights is None:
                    continue

                model.layer_to_adapter_weights[layer_name].add_adapter(
                    adapter_index, adapter_weights
                )

            if len(unused_weight_names) > 0:
                logger.warning(
                    f"{','.join([a.id for a in lora_adapters])} unused adapter weights: {unused_weight_names}"
                )

            if adapter_tokenizer is not None:
                model.tokenizers.add_tokenizer(adapter_index, adapter_tokenizer)

            model.loaded_adapters.add(adapter_index)

    return model
