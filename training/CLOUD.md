# Running a training job on a cloud GPU (vast.ai)

Offload a run when the local card is too small/slow. Recommended pick:
**RTX 4090 (24 GB), ~$0.30–0.45/hr** — finishes the 135M full-FT run in ~3–4 h
(~$1–1.5 total) and leaves VRAM headroom for bigger jobs.

> Code ships via git. The data splits and checkpoints are gitignored, so the
> `foodcom/` splits go up by `scp` and the trained model comes back by `scp`.

## 1. One-time setup (local)

```bash
pip install vastai
vastai set api-key YOUR_KEY                     # after adding billing on vast.ai
vastai create ssh-key ~/.ssh/id_ed25519.pub     # register your public key
```

## 2. Find and rent a GPU

```bash
vastai search offers 'gpu_name=RTX_4090 num_gpus=1 verified=true rentable=true inet_down>=200 disk_space>=60 cuda_vers>=12.1' -o 'dph+'

vastai create instance OFFER_ID --image pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime --disk 60 --ssh --direct
vastai show instance INSTANCE_ID                 # wait for status = running
vastai ssh-url INSTANCE_ID                        # -> ssh root@HOST -p PORT
```

## 3. Get code + data onto the instance

```bash
# on the instance:
git clone YOUR_REMOTE grocery-optimizer

# from your local machine (74 MB of splits):
scp -P PORT -r training/data/foodcom root@HOST:/workspace/grocery-optimizer/training/data/
```

## 4. Run it

The bootstrap installs deps and launches training **inside a detached tmux
session named `sft`**, so the run keeps going even if your SSH drops — you don't
have to start tmux yourself for the run to survive.

```bash
# on the instance, from the repo root:
cd /workspace/grocery-optimizer
bash training/cloud_bootstrap.sh
```

Once it reports the run has launched, **transfer into the tmux session** to watch
the live progress bar / loss:

```bash
tmux attach -t sft        # detach again (leaves it running) with: Ctrl-b then d
```

> The `pip install` step runs in the foreground *before* the tmux session
> exists. If your connection is flaky and you want that protected too, start a
> session first — `tmux new -s setup` — run the bootstrap inside it, then
> `Ctrl-b d` to detach and `tmux attach -t sft` to watch training.

Pass a different config as an argument, e.g.
`bash training/cloud_bootstrap.sh training/reproduce_paper/configs/smollm-360m-qlora.yaml`.

TensorBoard: add `-L 6006:localhost:6006` to your `ssh` command, run
`tensorboard --logdir training/runs` on the instance, open `localhost:6006`.

## 5. Pull the model back, then stop billing

```bash
# from your local machine:
scp -P PORT -r root@HOST:/workspace/grocery-optimizer/training/runs/smollm-135m-full training/runs/

# ⚠️ billing runs until you destroy the instance (stop still charges for storage):
vastai destroy instance INSTANCE_ID
```

## tmux quick reference

The run lives in a tmux session, so detaching/disconnecting won't kill it.

```bash
tmux ls                     # list running sessions
tmux attach -t sft          # reattach to the training session
tmux new -s NAME            # start a new named session
tmux kill-session -t NAME   # kill a session (stops whatever's in it)
```

Inside a session (prefix is **Ctrl-b**, released, then the key):

```
Ctrl-b  d        detach — leaves the session (and your run) alive
Ctrl-b  [        scroll mode — PgUp/arrows to scroll, q or Esc to exit
Ctrl-b  c        new window      Ctrl-b  n / p   next / previous window
exit (Ctrl-d)    close the current session/window (ends the process in it)
```
