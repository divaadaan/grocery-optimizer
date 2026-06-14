#!/usr/bin/env bash
# Bootstrap an SFT run on a fresh cloud GPU instance (e.g. vast.ai pytorch image).
# Run from the repo root AFTER cloning the repo and scp-ing the foodcom splits up.
#
#   bash training/cloud_bootstrap.sh [CONFIG]
#
# CONFIG defaults to the 135M full-FT config. See training/CLOUD.md.
set -euo pipefail

CONFIG="${1:-training/reproduce_paper/configs/smollm-135m-full.yaml}"
SESSION="sft"

# Must run from repo root.
if [ ! -f "$CONFIG" ]; then
  echo "ERROR: config not found: $CONFIG (run this from the repo root)." >&2
  exit 1
fi

# Data has to be uploaded separately (it's gitignored).
if [ ! -f training/data/foodcom/train.jsonl ]; then
  echo "ERROR: training/data/foodcom/train.jsonl not found." >&2
  echo "scp the foodcom splits up before running this script." >&2
  exit 1
fi

# tmux keeps the run alive across SSH drops; the pytorch image may not ship it.
command -v tmux >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq tmux; }

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already exists; attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

# torch/CUDA are already in the pytorch image; this adds transformers/trl/peft/etc.
pip install -r training/requirements.txt

mkdir -p training/runs
tmux new-session -d -s "$SESSION" \
  "python -m training.reproduce_paper.train_sft --config '$CONFIG' 2>&1 | tee training/runs/cloud_run.log"

cat <<EOF

Training launched in tmux session '$SESSION'.
  Watch:        tmux attach -t $SESSION    (detach: Ctrl-b then d)
  Log:          tail -f training/runs/cloud_run.log
  TensorBoard:  tensorboard --logdir training/runs --port 6006
EOF
