# Recipe evaluation report

- Generated at: 2026-10-30T12:00:00Z
- Git sha(s): fbba753da7 (dirty)
- Runs: 4
- Verdicts: PASS 2 / FAIL 1 / N/A 1

## Compliance

Compliance is decided only from curated product attributes; violations are cited by deal/product id.

### FAIL (1)

- **seed27-vegetarian-leak [seed27] chef=scripted/chef-script; sous_chef=scripted/sous-script**
  - compliance: 1/3 approved recipe(s) violate the restrictions
  - recipe 732e1ca4-ce93-53cd-81b5-e01825549049: deal 1 -> contains_meat
  - run's recorded verdict disagrees on 1 recipe(s)

### N/A (1)

- **seed27-halal [seed27] chef=scripted/chef-script; sous_chef=scripted/sous-script**: unsupported restriction label(s): halal (not scored; never a pass)

## Runs

| scenario | snapshot | config | verdict | approved | compliance | citation validity | instruction refs | deal share | jaccard mean/max | cost LB per-recipe-sum | cost LB per-unique-deal | savings | llm s | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27-halal | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script | NA | 3/3 | NA | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.11/0.33 | $15.64 | $13.15 | $6.80 | 0.000 | 4183 (known for 5/5 calls) |
| seed27-sparse-data | seed27 (2026-07-15) | chef=ollama/qwen-x; sous_chef=scripted/sous-script | PASS | 3/3 | PASS | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.11/0.33 | $15.64 | $13.15 | n/a | 0.000 | n/a (known for 0/5 calls) |
| seed27-vegetarian | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script | PASS | 3/3 | PASS | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.11/0.33 | $15.64 | $13.15 | $6.80 | 0.000 | 4183 (known for 5/5 calls) |
| seed27-vegetarian-leak | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script | FAIL | 3/3 | FAIL | 90% | 6 steps, 0 invalid, 1 unreferenced | 70% | 0.08/0.25 | $24.63 | $22.14 | $6.80 | 0.000 | 4183 (known for 5/5 calls) |

Cost columns are information only (the product is budget-agnostic) and **lower bounds** (one deal unit per cited ingredient; ignores quantity, household size, multi-buy pricing; pantry staples cost 0). Both cost semantics are shown: *per-recipe-sum* charges a deal reused across recipes once per recipe; *per-unique-deal* charges each deal once (the shopping list, and the plan total since Track C). `savings` is shown only where regular prices are known, otherwise n/a. Token totals sum only calls that reported usage.

## Aggregates

### By model configuration

| model configuration | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | cost LB per-recipe-sum (mean) | cost LB per-unique-deal (mean) | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chef=ollama/qwen-x; sous_chef=scripted/sous-script | 1 | 1/0/0 | 100% | 100% | 67% | 0.11 | 0.33 | $15.64 | $13.15 | n/a (known for 0/1 runs) | 0.000 | 0.000 | n/a (known for 0/5 calls) |
| chef=scripted/chef-script; sous_chef=scripted/sous-script | 3 | 1/1/1 | 33% | 97% | 68% | 0.10 | 0.33 | $18.64 | $16.15 | $20.40 (known for 3/3 runs) | 0.001 | 0.000 | 12549 (known for 15/15 calls) |

### By snapshot

| snapshot | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | cost LB per-recipe-sum (mean) | cost LB per-unique-deal (mean) | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27 (2026-07-15) | 4 | 2/1/1 | 50% | 98% | 68% | 0.10 | 0.33 | $17.89 | $15.40 | $20.40 (known for 3/4 runs) | 0.001 | 0.000 | 12549 (known for 15/20 calls) |
