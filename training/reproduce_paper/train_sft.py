"""SFT training for recipe generation, reproducing arXiv 2502.02028.

Driven by a YAML config (see configs/). Supports full fine-tuning (paper's
small-scale setup, e.g. SmolLM-135M) and QLoRA rank 8 (paper's large-scale
setup, e.g. SmolLM-360M / 1.7B).

Usage (from repo root, training venv active):
    python -m training.reproduce_paper.train_sft --config training/reproduce_paper/configs/smollm-135m-full.yaml
    python -m training.reproduce_paper.train_sft --config ... --smoke   # 20-step sanity run
"""

import argparse
import dataclasses
import inspect
import json
from pathlib import Path

import torch
import yaml
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

try:
    from .formatting import build_full_text
except ImportError:
    from formatting import build_full_text

REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def stage(msg: str) -> None:
    """Print a clearly delimited pipeline-stage banner."""
    print(f"\n{'=' * 64}\n  {msg}\n{'=' * 64}", flush=True)


def load_splits(data_dir: Path, tokenizer, smoke: bool, dataset_format: str = "text"):
    files = {split: str(data_dir / f"{split}.jsonl") for split in ("train", "validation")}
    ds = load_dataset("json", data_files=files)
    if smoke:
        ds["train"] = ds["train"].select(range(min(512, len(ds["train"]))))
        ds["validation"] = ds["validation"].select(range(min(64, len(ds["validation"]))))

    if dataset_format == "conversational":
        # App-targeted dataset (data_app): conversational prompt-completion, where
        # `prompt` is a user turn and `completion` an assistant turn. TRL applies
        # the model's chat template and masks the prompt (incl. the assistant
        # header), so loss falls only on the completion — we never train the model
        # to echo the big deals_json blob, and the boundary matches how the app's
        # ChatOllama feeds the model at inference. Drop task/meta so the trainer
        # sees only the two columns it consumes.
        keep = {"prompt", "completion"}
        drop = [c for c in ds["train"].column_names if c not in keep]
        return ds.remove_columns(drop)

    def to_text(example):
        return {"text": build_full_text(example["name"], example["ingredients"],
                                        example["steps"], tokenizer.eos_token)}

    return ds.map(to_text, remove_columns=["name", "ingredients", "steps"])


def build_sft_config(cfg: dict, output_dir: Path, smoke: bool) -> SFTConfig:
    precision = cfg.get("precision", "fp16")
    dataset_format = cfg.get("dataset_format", "text")
    kwargs = {
        "output_dir": str(output_dir),
        "per_device_train_batch_size": cfg["per_device_train_batch_size"],
        "gradient_accumulation_steps": cfg["gradient_accumulation_steps"],
        "learning_rate": float(cfg["learning_rate"]),
        "num_train_epochs": cfg["num_train_epochs"],
        "weight_decay": cfg.get("weight_decay", 0.01),
        "warmup_steps": cfg.get("warmup_steps", 100),
        "optim": cfg.get("optim", "adamw_torch"),
        "max_length": cfg.get("max_length", 512),
        "fp16": precision == "fp16",
        "bf16": precision == "bf16",
        "gradient_checkpointing": cfg.get("gradient_checkpointing", False),
        "logging_steps": 25,
        "eval_strategy": "steps",
        "eval_steps": cfg.get("eval_steps", 500),
        "per_device_eval_batch_size": cfg["per_device_train_batch_size"],
        "save_strategy": "epoch",
        "report_to": cfg.get("report_to", "none"),
        "seed": cfg.get("seed", 42),
    }
    if dataset_format == "conversational":
        # Conversational prompt/completion: no single text field; mask the prompt
        # and train only on the assistant completion (TRL resolves None->True
        # here, but be explicit). Filtered out below if the installed TRL lacks
        # the field.
        kwargs["completion_only_loss"] = True
    else:
        kwargs["dataset_text_field"] = "text"
    if smoke:
        kwargs.update(max_steps=20, eval_strategy="no", save_strategy="no", logging_steps=5)

    # TRL/transformers renamed these fields across versions; remap to whatever
    # the installed version exposes.
    valid = {f.name for f in dataclasses.fields(SFTConfig)}
    renames = {"max_length": "max_seq_length", "eval_strategy": "evaluation_strategy"}
    for new, old in renames.items():
        if new not in valid and old in valid and new in kwargs:
            kwargs[old] = kwargs.pop(new)
    return SFTConfig(**{k: v for k, v in kwargs.items() if k in valid})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--smoke", action="store_true",
                        help="20-step run on a 512-example subset to verify the pipeline")
    args = parser.parse_args()

    with open(resolve(args.config), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    output_dir = resolve(cfg["output_dir"])
    if args.smoke:
        output_dir = output_dir.with_name(output_dir.name + "-smoke")

    stage(f"Stage 1/5 · Loading tokenizer ({cfg['model_name']})")
    tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    stage("Stage 2/5 · Preparing dataset")
    dataset_format = cfg.get("dataset_format", "text")
    data_dir = resolve(cfg.get("data_dir", "training/data/foodcom"))
    ds = load_splits(data_dir, tokenizer, args.smoke, dataset_format)
    print(f"  source: {data_dir}  format: {dataset_format}", flush=True)
    print(f"  train={len(ds['train'])}  validation={len(ds['validation'])}"
          f"{'  (smoke subset)' if args.smoke else ''}", flush=True)

    stage("Stage 3/5 · Building model")
    qlora = cfg.get("qlora", {})
    peft_config = None
    model_kwargs = {}
    if qlora.get("enabled", False):
        from peft import LoraConfig

        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.float16,
        )
        peft_config = LoraConfig(
            r=qlora.get("r", 8),
            lora_alpha=qlora.get("alpha", 16),
            lora_dropout=qlora.get("dropout", 0.05),
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=qlora.get(
                "target_modules",
                ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            ),
        )

    model = AutoModelForCausalLM.from_pretrained(cfg["model_name"], **model_kwargs)
    model.config.use_cache = False  # incompatible with gradient checkpointing / training
    mode = f"QLoRA 4-bit (r={qlora.get('r', 8)})" if qlora.get("enabled", False) else "full fine-tune"
    print(f"  mode: {mode}", flush=True)

    sft_config = build_sft_config(cfg, output_dir, args.smoke)
    trainer_kwargs = {
        "model": model,
        "args": sft_config,
        "train_dataset": ds["train"],
        "eval_dataset": ds["validation"],
        "peft_config": peft_config,
    }
    # tokenizer= was renamed to processing_class= in recent TRL
    if "processing_class" in inspect.signature(SFTTrainer.__init__).parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer

    trainer = SFTTrainer(**trainer_kwargs)

    if dataset_format == "conversational":
        # Eyeball completion-only masking on a tokenized training sample. TRL's
        # prompt-completion path stores a `completion_mask` (1 on completion
        # tokens, 0 on the masked prompt); the collator turns the 0s into -100
        # labels. A `trained` count near len(completion) confirms the deals_json
        # prompt region is masked and max_length didn't truncate the completion.
        sample = trainer.train_dataset[0]
        mask = sample.get("completion_mask")
        if mask is not None:
            trained = sum(mask)
            masked = len(mask) - trained
            print(f"  completion-only loss: {masked}/{len(mask)} tokens masked "
                  f"(prompt), {trained} trained (completion)", flush=True)
            if trained == 0:
                raise SystemExit("No completion tokens left to train on — max_length "
                                 "is shorter than the prompt; raise it in the config.")

    stage("Stage 4/5 · Running SFT")
    eff_batch = cfg["per_device_train_batch_size"] * cfg["gradient_accumulation_steps"]
    print(f"  epochs={cfg['num_train_epochs']}  effective_batch={eff_batch}  "
          f"lr={cfg['learning_rate']}", flush=True)
    print(f"  output: {output_dir}", flush=True)
    result = trainer.train()

    stage("Stage 5/5 · Saving model & artifacts")
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))
    with open(output_dir / "train_result.json", "w", encoding="utf-8") as f:
        json.dump({"config": cfg, "smoke": args.smoke, "metrics": result.metrics}, f, indent=2)
    print(f"Saved to {output_dir}")
    print(json.dumps(result.metrics, indent=2))


if __name__ == "__main__":
    main()
