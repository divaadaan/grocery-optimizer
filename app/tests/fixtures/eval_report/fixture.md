# Recipe evaluation report

- Generated at: 2026-10-30T12:00:00Z
- Git sha(s): 3cacf6672c (dirty)
- Runs: 1
- Verdicts: PASS 1 / FAIL 0 / N/A 0

## Compliance

Compliance is decided only from curated product attributes; violations are cited by deal/product id.

### FAIL (0)

None.

### N/A (0)

None.

## Runs

| scenario | snapshot | config | verdict | approved | compliance | citation validity | instruction refs | deal share | jaccard mean/max | budget LB per-recipe-sum | budget LB per-unique-deal | budget status | savings | llm s | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27-vegetarian | seed27 (2026-07-15) | chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | PASS | 3/3 | PASS | 100% | 6 steps, 0 invalid, 0 unreferenced | 67% | 0.00/0.00 | $7.14 | $7.14 | within_lower_bound (avail $80.00) | $5.80 | 0.002 | 6637 (known for 6/9 calls) |

Budget columns are **lower bounds** (one deal unit per cited ingredient; ignores quantity, household size, multi-buy pricing; pantry staples cost 0). Both cost semantics are shown: *per-recipe-sum* charges a deal reused across recipes once per recipe; *per-unique-deal* charges each deal once (the shopping list, and the plan total since Track C). `savings` is shown only where regular prices are known, otherwise n/a. Token totals sum only calls that reported usage.

## Aggregates

### By model configuration

| model configuration | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | budget LB per-recipe-sum (mean) | budget LB per-unique-deal (mean) | not within LB | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chef=scripted/chef-script; sous_chef=scripted/sous-script; nutritionist=scripted/nutri-script | 1 | 1/0/0 | 100% | 100% | 67% | 0.00 | 0.00 | $7.14 | $7.14 | 0 | $5.80 (known for 1/1 runs) | 0.002 | 0.002 | 6637 (known for 6/9 calls) |

### By snapshot

| snapshot | runs | PASS/FAIL/N/A | pass rate | citation validity (mean) | deal share (mean) | jaccard mean (mean) | jaccard max (max) | budget LB per-recipe-sum (mean) | budget LB per-unique-deal (mean) | not within LB | savings (sum, known runs) | llm s (sum) | llm s (mean/run) | tokens (sum) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| seed27 (2026-07-15) | 1 | 1/0/0 | 100% | 100% | 67% | 0.00 | 0.00 | $7.14 | $7.14 | 0 | $5.80 (known for 1/1 runs) | 0.002 | 0.002 | 6637 (known for 6/9 calls) |
