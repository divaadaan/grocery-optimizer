"""Merge a QLoRA adapter into its base model for export.

Needed before converting to GGUF for Ollama serving (llama.cpp's
convert_hf_to_gguf.py wants merged HF weights, not an adapter).

Usage (from repo root, training venv active):
    python -m training.reproduce_paper.merge_lora \\
        --adapter training/runs/smollm-360m-qlora --output training/runs/smollm-360m-merged
"""

import argparse
from pathlib import Path

import torch
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer

REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapter", required=True, help="Run dir containing adapter_config.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    adapter_dir, output_dir = resolve(args.adapter), resolve(args.output)
    model = AutoPeftModelForCausalLM.from_pretrained(str(adapter_dir), dtype=torch.float16)
    merged = model.merge_and_unload()
    merged.save_pretrained(str(output_dir))
    AutoTokenizer.from_pretrained(str(adapter_dir)).save_pretrained(str(output_dir))
    print(f"Merged model saved to {output_dir}")


if __name__ == "__main__":
    main()
