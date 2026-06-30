# Benchmarks

## LoCoMo retrieval benchmark

Measures whether `query_memory` surfaces the **gold evidence turns** for each
LoCoMo question — a retrieval metric (recall@k), broken down by question
category. Category **temporal** is the one a temporal/contradiction layer would
improve, so its score drives the "should we build temporal?" decision.

### Why recall@k (not QA accuracy)

3am-claude is a retrieval engine, not an answer generator. Published LoCoMo
numbers (Zep ~85%, Letta ~83%, Mem0 ~58–66%) are **LLM-judged end-to-end
answers** — a different scale. recall@k isolates the part 3am-claude controls
and is the right yardstick for comparing 3am-claude **against itself** across
versions. Do not compare these percentages directly to the published QA numbers.

### Run

```bash
# one-time: fetch the dataset (gitignored, ~2.8 MB)
mkdir -p benchmarks/data
curl -sfL -o benchmarks/data/locomo10.json \
  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json

# full run (~6–7 min, CPU embedding)
.venv/bin/python benchmarks/locomo_bench.py

# quick iteration on one conversation
.venv/bin/python benchmarks/locomo_bench.py --max-samples 1
```

The harness uses a throwaway temp DB (never the live `memory.db`), one
`project_id` per conversation, and disables dedup + auto-promotion so the corpus
is faithful. Results are written to `benchmarks/results/locomo_<timestamp>.json`.

### Baseline — v1 (2026-06-30)

Stock retrieval: hybrid FTS5 + vec + PPR, `nomic-embed-text-v1.5`, no temporal
layer. 10 conversations, 1982 answerable questions.

| category    |    n | R@5   | R@10  | R@20  |
|-------------|-----:|------:|------:|------:|
| multi-hop   |  282 | 52.5% | 67.4% | 69.5% |
| **temporal**|  321 | 47.7% | 51.4% | 52.0% |
| open-domain |   92 | 34.8% | 47.8% | 50.0% |
| single-hop  |  841 | 42.9% | 63.6% | 66.3% |
| adversarial |  446 | 23.1% | 37.7% | 41.7% |
| **OVERALL** | 1982 | 40.2% | 55.6% | 58.2% |

**Diagnosis:** temporal is the weakest *answerable* category (51% R@10 vs
64–67% for single/multi-hop) **and its recall is flat across k** (47.7→52.0 from
k=5→20) — the supporting turns aren't being retrieved at any depth, not merely
ranked low. That flat curve is the signature of a *representational* gap (no time
modeling), not a ranking problem — i.e. evidence that a temporal layer is the
right next investment. Adversarial scoring low is partly by design (misleading
questions).

### v2 — temporal retrieval (2026-06-30)

Added: temporal-query beam widening, **soft per-universe caps** (unused result
slots backfill instead of being wasted — a temporal "when" query was being
classified procedural and throttled to the declarative cap), stopword-filtered
FTS, and superseded-memory filtering. Same dataset, same harness.

| category    |    n | R@5   | R@10  | R@20  |
|-------------|-----:|------:|------:|------:|
| multi-hop   |  282 | 51.1% | 72.7% | 81.9% |
| **temporal**|  321 | 39.6% | 65.7% | 85.0% |
| open-domain |   92 | 34.8% | 52.2% | 60.9% |
| single-hop  |  841 | 42.3% | 71.2% | 85.4% |
| adversarial |  446 | 22.7% | 50.7% | 76.2% |
| **OVERALL** | 1982 | 38.4% | 65.0% | 81.6% |

**Δ vs v1 (R@20):** temporal **+33.0** (52.0→85.0), single-hop +19.1, overall
**+23.4** (58.2→81.6). The flat temporal curve is gone (39.6→65.7→85.0 now rises
healthily with k). The root cause was the universe-cap throttle, not (only) the
embedding — a useful finding. Trade-off: R@5 dipped slightly (temporal 47.7→39.6)
as the beam widened; net strongly positive at k=10/20. The contradiction/
supersession layer (supersede_memory, conflict detection) addresses a *different*
temporal axis (facts that change) and isn't what these numbers measure.
