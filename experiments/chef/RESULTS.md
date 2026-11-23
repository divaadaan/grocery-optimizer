# Chef prototypes — results (2026-11-23)

How should the Chef see this week's deals? Three approaches, four models, three seeds each,
over one frozen, dietary-gated snapshot (916 deals, postal code L4G3E8, no restrictions).

**Verdict: approach C (code-built trays).** It removes position bias on every model, lets the local
7B plan sensible meals, and is the fastest. B (browse by category) is a credible fallback that needs
no labelling step. A (everything in one prompt) is ruled out.

## Why this was run

The 2026-11-23 MLflow trace of the production Chef showed:
- its prompt was cut off: Ollama kept only the last ~4,100 of ~11,100 tokens;
- all 12 of its picks came from deals 66–97 of the 100 it was sent;
- sorting by discount % kept every major chain out of those 100, because their flyers have no
  regular price.

The owner's own planning method is the design target:
1. find the loss leaders (the anchors);
2. find discounted staples to stock up on;
3. pair sides with the anchors.

The owner also reframed the product as budget-agnostic: "here's what you can eat from grocery sales
near you".

## Approaches

| | How the Chef gets deals | Code |
|---|---|---|
| **A: one long prompt** | All 916 gated deals as one-line rows (~30k tokens), shuffled per seed, nothing truncated | `arms/a_full_list.py` |
| **B: browse by category** | Tools `list_deal_types()` and `list_deals_by_type(category, page)`; 40 rows per page sorted by name; at most 15 calls | `arms/b_browse.py` |
| **C: code-built trays** | Each deal labelled once with an `{item, role}` (`label.py`, gpt-oss-120b, batches of 40). Code groups deals by item, normalises prices to $/lb and compares each deal with the median across stores. The Chef gets 3 trays, about 55 rows: ANCHORS (15 proteins), SIDES (25 vegetables, starches, fruit) and STOCK-UP (15 staples) | `arms/c_trays.py` |

Every approach returns the same dish-first plan: dishes, each with anchor and side deal ids, pantry
items from a fixed list, plus stock-up deal ids (`common.py`).

## Results

`python -m experiments.chef.compare`

| arm | model | runs | valid_plans | invalid_ids | unseen_ids | spread | chains | beverages | tools | seen | sec |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A_full_list | Qwen3.6-35B-A3B | 3 | 2/3 | 0 | 0 | 0.68 | 11 | 0 | 0 | 916 | 54 |
| A_full_list | Llama-3.3-70B-Instruct | 3 | 3/3 | 0 | 0 | 0.22 | 12 | 0 | 0 | 916 | 26 |
| A_full_list | gpt-oss-120b | 3 | 3/3 | 0 | 0 | 0.54 | 11.7 | 0 | 0 | 916 | 16 |
| A_full_list | ollama:qwen2.5:7b | 3 | 3/3 | 0 | 0 | 0.29 | 9.3 | 0 | 0 | 916 | 41 |
| B_browse | Qwen3.6-35B-A3B | 3 | 3/3 | 0 | 0 | 0.58 | 10.7 | 0 | 10 | 349 | 193 |
| B_browse | Llama-3.3-70B-Instruct | 3 | 2/3 | 0 | 0 | 0.34 | 8.5 | 0 | 8 | 271 | 10 |
| B_browse | gpt-oss-120b | 3 | 3/3 | 0 | 0 | 0.76 | 12.3 | 0 | 5.7 | 170 | 19 |
| B_browse | ollama:qwen2.5:7b | 3 | 3/3 | 0 | 0 | 0.80 | 9 | 0 | 8 | 252 | 46 |
| **C_trays** | Qwen3.6-35B-A3B | 3 | 3/3 | 0 | 0 | 0.84 | 14 | 1 | 0 | 55 | 79 |
| **C_trays** | Llama-3.3-70B-Instruct | 3 | 2/3 | 0 | 0 | 0.80 | 13 | 0 | 0 | 55 | 20 |
| **C_trays** | gpt-oss-120b | 3 | 2/3 | 0 | 0 | 0.82 | 12 | 0 | 0 | 55 | 10 |
| **C_trays** | ollama:qwen2.5:7b | 3 | 3/3 | 0 | 0 | 0.79 | 12.3 | 0 | 0 | 55 | 11 |

Column meanings:
- **valid_plans:** exactly 5 dishes and no invalid ids.
- **spread:** the share of cited deals that came from outside the top fifth of what the model was
  shown. 0.8 means no position bias.
- **unseen_ids:** valid ids the model cited without ever being shown them.
- **seen:** how many deals the model actually read.

No dietary judgement is involved anywhere: the snapshot is gated before any approach runs. Dish
quality was judged by reading `runs/*/review.md`.

## Findings

- **A: putting everything in one prompt fails even when nothing is truncated.**
  - Picks cluster at the top of whatever order the model sees. A larger model does not fix it:
    Llama-70B's spread (0.22) was worse than the 7B's.
  - On the 7B, dishes didn't match their anchors, e.g. milk anchoring "shrimp pasta" and sliced
    bread anchoring "beef pot pie".
- **B: browsing removes the bias on strong models** (gpt-oss-120b 0.76).
  - Plans were real: whole chicken, thighs, pork chops and ground beef across Farm Boy, Loblaws,
    No Frills and Sobeys.
  - Weaker models only read page 1 of each category. Pages are sorted by name, so that is a new
    position bias.
  - The 7B used ready meals (M&M entrées) and "Cups" as ingredients.
- **C: trays work on every model.**
  - The 7B produced sensible plans in 3 of 3 seeds in about 11 s: thighs with potatoes, shrimp
    stir-fry, tofu stir-fry, beef soup, pork chops with apples.
  - Ready meals, snacks and beverages never reach the Chef. The one beverage pick came from a
    mislabelled stock-up item.
- **C's weak points:**
  - Label quality. tea, coffee pods and Reese's were labelled `pantry_staple`.
  - Package-price comparisons. 53% of deals have no unit, so "rice $4.99 vs median $13.99" may just
    be a smaller bag. $/lb comparisons, which matter most for anchors, are reliable.
- **No model made up a deal id in any run.** Failures were malformed JSON only:
  - Llama dropped a quote mark;
  - gpt-oss went into a repeating loop when the JSON example had `//` comments, and still
    occasionally returns junk;
  - Qwen3.6 can spend its whole output budget thinking, even with `enable_thinking=false`.

  Production code needs one validate-and-retry step.

## Cost

Hugging Face Inference Providers, for the whole study including superseded runs: about 810k input
and 125k output tokens, under about $0.30 at listed prices. Labelling 916 deals takes 23 calls and
costs a few cents per weekly snapshot.

## Reproduce

```
.venv/bin/python -m experiments.chef.snapshot --postal L4G3E8          # read-only; gated by deal_gate
.venv/bin/python -m experiments.chef.label --snapshot experiments/chef/snapshots/2026-11-23_L4G3E8_open.json
.venv/bin/python -m experiments.chef.arms.c_trays experiments/chef/snapshots/2026-11-23_L4G3E8_open.json   # print trays
.venv/bin/python -m experiments.chef.run --arm C --model ollama:qwen2.5:7b \
    --snapshot experiments/chef/snapshots/2026-11-23_L4G3E8_open.json --repeats 3
.venv/bin/python -m experiments.chef.compare
```

- Model specs are `ollama:<name>` or `hf:<org/model>`.
- HF calls read `HUGGINGFACE_API_KEY` (or `HF_TOKEN`) from `.env`.
- `runs/_superseded/` (not committed) holds runs from before the plan format was fixed.
