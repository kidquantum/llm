# Usage: deepspeed train_lora.py --deepspeed <$PATH_TO_DEEPSPEED_CONFIG>

# Adopted from tatsu-lab@stanford_alpaca. Below is the original copyright:
#    Copyright 2023 Rohan Taori, Ishaan Gulrajani, Tianyi Zhang, Yann Dubois, Xuechen Li
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from dataclasses import dataclass, field
import logging
import pathlib
import typing
import os

from deepspeed import zero
from deepspeed.runtime.zero.partition_parameters import ZeroParamStatus
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
import transformers
from transformers import Trainer, BitsAndBytesConfig, deepspeed
import torch

from fastchat.train.train import (
    DataArguments,
    ModelArguments,
    TrainingArguments,
    make_supervised_data_module,
)

# from fastchat.train.llama_flash_attn_monkey_patch import (
#     replace_llama_attn_with_flash_attn,
# )


@dataclass
class LoraArguments:
    lora_r: int = 8
    lora_alpha: int = 16
    lora_dropout: float = 0.05
    lora_target_modules: typing.List[str] = field(
        default_factory=lambda: ["q_proj", "v_proj"]
    )
    lora_weight_path: str = ""
    lora_bias: str = "none"
    q_lora: bool = False


@dataclass
class FlashAttentionArguments:
    flash_attn: bool = False

    @property
    def enabled(self):
        return self.flash_attn


@dataclass
class TuningArgs:
    model: ModelArguments
    data: DataArguments
    training: TrainingArguments
    lora: LoraArguments
    flash_attn: FlashAttentionArguments


def maybe_zero_3(param):
    if hasattr(param, "ds_id"):
        assert param.ds_status == ZeroParamStatus.NOT_AVAILABLE
        with zero.GatheredParameters([param]):
            param = param.data.detach().cpu().clone()
    else:
        param = param.detach().cpu().clone()
    return param


# Borrowed from peft.utils.get_peft_model_state_dict
def get_peft_state_maybe_zero_3(named_params, bias):
    if bias == "none":
        to_return = {k: t for k, t in named_params if "lora_" in k}
    elif bias == "all":
        to_return = {k: t for k, t in named_params if "lora_" in k or "bias" in k}
    elif bias == "lora_only":
        to_return = {}
        maybe_lora_bias = {}
        lora_bias_names = set()
        for k, t in named_params:
            if "lora_" in k:
                to_return[k] = t
                bias_name = k.split("lora_")[0] + "bias"
                lora_bias_names.add(bias_name)
            elif "bias" in k:
                maybe_lora_bias[k] = t
        for k, t in maybe_lora_bias:
            if bias_name in lora_bias_names:
                to_return[bias_name] = t
    else:
        raise NotImplementedError
    to_return = {k: maybe_zero_3(v) for k, v in to_return.items()}
    return to_return


def train(args: TuningArgs):
    # if args.flash_attn.enabled:
    #     replace_llama_attn_with_flash_attn()

    device_map = None
    if args.lora.q_lora:
        world_size = int(os.environ.get("WORLD_SIZE", 1))
        device_map = (
            {"": int(os.environ.get("LOCAL_RANK") or 0)} if world_size != 1 else None
        )
        if len(args.training.fsdp) > 0 or deepspeed.is_deepspeed_zero3_enabled():
            logging.warn("FSDP and ZeRO3 are both currently incompatible with QLoRA.")

    compute_dtype = (
        torch.float16
        if args.training.fp16
        else (torch.bfloat16 if args.training.bf16 else torch.float32)
    )

    model = transformers.AutoModelForCausalLM.from_pretrained(
        args.model.model_name_or_path,
        cache_dir=args.training.cache_dir,
        device_map=device_map,
        # load_in_8bit=True,
        # torch_dtype=compute_dtype,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=compute_dtype,
        )
        if args.lora.q_lora
        else None,
    )
    lora_config = LoraConfig(
        r=args.lora.lora_r,
        lora_alpha=args.lora.lora_alpha,
        target_modules=args.lora.lora_target_modules,
        lora_dropout=args.lora.lora_dropout,
        bias=args.lora.lora_bias,
        task_type="CAUSAL_LM",
    )

    if args.lora.q_lora:
        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=args.training.gradient_checkpointing
        )
        if torch.cuda.device_count() > 1:
            # keeps Trainer from trying its own DataParallelism when more than 1 gpu is available
            model.is_parallelizable = True
            model.model_parallel = True

    model = get_peft_model(model, lora_config)
    if args.training.deepspeed is not None and args.training.local_rank == 0:
        model.print_trainable_parameters()

    if args.training.gradient_checkpointing:
        model.enable_input_require_grads()

    tokenizer = transformers.AutoTokenizer.from_pretrained(
        args.model.model_name_or_path,
        cache_dir=args.training.cache_dir,
        model_max_length=args.training.model_max_length,
        padding_side="right",
        use_fast=False,
    )
    tokenizer.pad_token = tokenizer.unk_token

    data_module = make_supervised_data_module(tokenizer=tokenizer, data_args=args.data)
    trainer = Trainer(
        model=model, tokenizer=tokenizer, args=args.training, **data_module
    )

    model.config.use_cache = False

    if list(pathlib.Path(args.training.output_dir).glob("checkpoint-*")):
        trainer.train(resume_from_checkpoint=True)
    else:
        trainer.train()
    trainer.save_state()

    # check if zero3 mode enabled
    if trainer.hf_deepspeed_config_orig.is_zero3():
        # use deepspeed engine internal function to gather state dict
        # state_dict_zero3 contains whole parameters of base and lora adapters
        # we will not extract lora parameters since peft save_pretrained will do that
        # https://github.com/huggingface/peft/blob/3714aa2fff158fdfa637b2b65952580801d890b2/src/peft/peft_model.py#L125
        # https://github.com/huggingface/peft/blob/3714aa2fff158fdfa637b2b65952580801d890b2/src/peft/utils/save_and_load.py#L19
        state_dict_zero3 = trainer.model_wrapped._zero3_consolidated_16bit_state_dict()
        if args.training.local_rank == 0:
            state_dict = state_dict_zero3
    else:
        # in other mode we use original code from fastchat team, to make sure our change is minimum
        state_dict = get_peft_state_maybe_zero_3(
            model.named_parameters(), args.lora.lora_bias
        )

    if args.training.local_rank == 0:
        model.save_pretrained(args.training.output_dir, state_dict=state_dict)


if __name__ == "__main__":
    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments, LoraArguments, FlashAttentionArguments)
    )
    (
        model_args,
        data_args,
        training_args,
        lora_args,
        flash_attn_args,
    ) = parser.parse_args_into_dataclasses()
    args = TuningArgs(model_args, data_args, training_args, lora_args, flash_attn_args)
    train(args)
