# Recipe evaluation report

- Generated at: 2026-10-30T12:00:00Z
- Git sha(s): d5c89255f9 (dirty)
- Runs: 4
- Verdicts: PASS 2 / FAIL 1 / N/A 1

## Compliance

Compliance is decided only from curated product attributes; violations are cited by deal/product id.

### FAIL (1)

- **seed27-vegetarian-leak [seed27] chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script**
  - compliance: 1/3 approved recipe(s) violate the restrictions
  - recipe 99b95ff9-7ad0-5847-9a7b-3dd3d3c2fa7e: deal 1 -> contains_meat
  - run's recorded verdict disagrees on 1 recipe(s)

### N/A (1)

- **seed27-halal [seed27] chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script**: unsupported restriction label(s): halal (not scored; never a pass)

## Runs

| scenario | snapshot | config | verdict | approved | compliance | citation validity | instruction refs | deal share | jaccard mean/max | budget LB per-recipe-sum | budget LB per-unique-deal | budget status | savings | llm s | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27-halal | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | NA | 3/3 | NA | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.00/0.00 | $7.14 | $7.14 | within_lower_bound (avail $80.00) | $5.80 | 0.002 | 6572 (known for 6/9 calls) |
| seed27-sparse-data | seed27 (2026-07-15) | chef=ollama/qwen-x; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | PASS | 3/3 | PASS | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.00/0.00 | $7.14 | $7.14 | within_lower_bound (avail $80.00) | n/a | 0.002 | n/a (known for 0/9 calls) |
| seed27-vegetarian | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | PASS | 3/3 | PASS | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.00/0.00 | $7.14 | $7.14 | within_lower_bound (avail $80.00) | $5.80 | 0.002 | 6572 (known for 6/9 calls) |
| seed27-vegetarian-leak | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | FAIL | 3/3 | FAIL | 90% | 6 steps, 0 invalid, 1 unreferenced | 70% | 0.00/0.00 | $16.13 | $16.13 | within_lower_bound (avail $80.00) | $5.80 | 0.002 | 6572 (known for 6/9 calls) |

Budget columns are **lower bounds** (one deal unit per cited ingredient; ignores quantity, household size, multi-buy pricing; pantry staples cost 0). Both cost semantics are shown: *per-recipe-sum* charges a deal reused across recipes once per recipe (the plan total); *per-unique-deal* charges each deal once (the shopping list). `savings` is shown only where regular prices are known, otherwise n/a. Token totals sum only calls that reported usage.

## Aggregates

### By model configuration

| model configuration | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | budget LB per-recipe-sum (mean) | budget LB per-unique-deal (mean) | not within LB | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chef=ollama/qwen-x; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | 1 | 1/0/0 | 100% | 100% | 67% | 0.00 | 0.00 | $7.14 | $7.14 | 0 | n/a (known for 0/1 runs) | 0.002 | 0.002 | n/a (known for 0/9 calls) |
| chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | 3 | 1/1/1 | 33% | 97% | 68% | 0.00 | 0.00 | $10.14 | $10.14 | 0 | $17.40 (known for 3/3 runs) | 0.006 | 0.002 | 19716 (known for 18/27 calls) |

### By snapshot

| snapshot | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | budget LB per-recipe-sum (mean) | budget LB per-unique-deal (mean) | not within LB | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27 (2026-07-15) | 4 | 2/1/1 | 50% | 98% | 68% | 0.00 | 0.00 | $9.39 | $9.39 | 0 | $17.40 (known for 3/4 runs) | 0.007 | 0.002 | 19716 (known for 18/36 calls) |
