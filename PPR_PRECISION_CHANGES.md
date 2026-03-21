# PPR Recall Precision Changes

Changes from recall precision tuning (2026-03-20). Apply to any 3am fork
sharing the same DBSCAN + PPR architecture.

Apply in this order — each change is independent but they work best together.

---

## 1. PPR damping: 0.85 → 0.65

**File:** `memory.py`
**Function:** `_ppr_expand`
**Change:** Lower the default damping value.

```python
# Before
def _ppr_expand(self, seed_ids, ..., damping: float = 0.85, ...):

# After
def _ppr_expand(self, seed_ids, ..., damping: float = 0.65, ...):
```

**Why:** More restarts toward the seed set, less graph diffusion.
Raises max PPR scores (~0.053 → ~0.083) and improves separation between
strong and weak matches. Tested across 0.85 → 0.75 → 0.65; diminishing
returns below 0.65.

---

## 2. min_score parameter

**Files:** `memory.py`, `mcp_server.py`

### memory.py — `query_memory` signature
```python
# Before
async def query_memory(self, query, project_id, limit=10, max_tokens=2000):

# After
async def query_memory(self, query, project_id, limit=10, max_tokens=2000,
                       min_score: float = 0.0):
```

### memory.py — result-building loop
Find the loop that builds the results list and add the guard:
```python
for memory_id, score in ranked:
    if score < min_score:   # <-- add this
        continue
    # ... rest of loop unchanged
```

### mcp_server.py — `query_memory` tool
```python
# Before
async def query_memory(query, project_id, limit=10, max_tokens=2000, ...) -> list:

# After
async def query_memory(query, project_id, limit=10, max_tokens=2000,
                       min_score: float = 0.0, ...) -> list:
```

Add to docstring:
```
  min_score:  minimum PPR score to include — results below this are dropped even
              if under the limit. Use ~0.01-0.03 to cut low-signal padding.
```

Pass through to `_memory.query_memory()`:
```python
return await _memory.query_memory(..., min_score=min_score)
```

**Why:** Lets callers trim low-signal padding from results. Useful when
injecting memories into a context budget. Effective range ~0.01–0.03.

---

## 3. Cosine seed gate (most impactful)

**File:** `memory.py` only — gate is internal, not exposed in MCP tool

Root problem: PPR always diffuses to *something* in the graph. Off-topic
queries ("grocery list milk eggs coffee") return 8 results scoring 0.047–0.070,
indistinguishable from real query scores. There is no "found nothing" signal
in PPR scores alone.

Fix: gate before PPR using the raw cosine distance from the vec seed lookup.
If no seed clears the cosine threshold AND FTS found nothing, return [] before
PPR runs.

### memory.py — add `distance` to vec SELECT

```python
# Before
vec_rows = conn.execute("""
    SELECT memory_id FROM vec_memories
    WHERE embedding MATCH ?
    ORDER BY distance
    LIMIT 20
""", (emb_bytes,)).fetchall()

# After
vec_rows = conn.execute("""
    SELECT memory_id, distance FROM vec_memories
    WHERE embedding MATCH ?
    ORDER BY distance
    LIMIT 20
""", (emb_bytes,)).fetchall()
```

### memory.py — add gate after vec_seeds filtering, before PPR

Insert this block between the `vec_seeds = [...][:8]` line and the
`seed_ids = _rrf_merge(...)` line:

```python
# Cosine seed gate: if the best cosine similarity is below min_cosine,
# PPR would only amplify noise — bail out before graph expansion.
if min_cosine > 0.0:
    _best_distance = next(
        (r["distance"] for r in vec_rows
         if r["memory_id"] in self.memories
         and self._is_visible(r["memory_id"], project_id)),
        1.0,
    )
    if (1.0 - _best_distance) < min_cosine and not fts_seeds:
        return []
```

### memory.py — add `min_cosine` to `query_memory` signature

```python
async def query_memory(self, query, project_id, limit=10, max_tokens=2000,
                       min_score: float = 0.0, min_cosine: float = 0.5):
```

**Do NOT expose `min_cosine` in the MCP tool signature.** The threshold is
calibrated and should be internal. Keeping it in `memory.py` only allows
programmatic override (e.g. tests) without letting MCP callers accidentally
disable the gate by passing 0.0.

**Threshold note on cosine distance:** sqlite-vec returns cosine distance as
`1 - cosine_similarity`. So `distance=0` is identical, `distance=1` is
orthogonal. The gate computes `cosine_similarity = 1.0 - distance`.

---

## Calibration results (2026-03-20, finalized)

Tested with: ada-002 embeddings, ~100 project memories, damping=0.65.

### Calibration table — `min_cosine` threshold sweep

| threshold | grocery (off-topic) | DBSCAN | Ingestion | Phase | CLI | Config |
|---|---|---|---|---|---|---|
| 0.00 | 8/8 ❌ | 8/8 ✅ | 8/8 ✅ | 7/8 ✅ | 8/8 ✅ | 5/8 ⚠️ |
| 0.25 | 8/8 ❌ | 8/8 ✅ | 8/8 ✅ | 7/8 ✅ | 8/8 ✅ | 5/8 ⚠️ |
| 0.30 | 8/8 ❌ | — | — | — | — | — |
| 0.35 | 8/8 ❌ | — | — | — | — | — |
| **0.50** | **[] ✅** | **8/8 ✅** | **8/8 ✅** | **7/8 ✅** | **8/8 ✅** | **5/8 ⚠️** |

**Confirmed threshold: 0.50.** Real query results are identical at 0.25 and 0.50
— the gate does not clip on-topic queries.

### Config

```json
"min_cosine": 0.5
```

Add to `config.default.json`.

### Summary table

| Change | Status |
|---|---|
| damping=0.65 | ✅ Validated — ~100 memories, 24 clusters |
| min_score parameter | ✅ Implemented, effective at 0.01–0.03 |
| cosine gate, min_cosine=0.50 | ✅ Calibrated and shipped |

### Remaining precision issue (structural, not fixable by tuning)

Config query returns 5/8 on-topic results. The 3 bleed items are:
- `min_samples` from DBSCAN cluster (config.default.json explicitly lists `eps`/`min_samples`)
- Phase 2 completed (mentions "DBSCAN clustering" in context of config)
- `cli.py cluster` command

This is semantic overlap in the content — the config file genuinely references
clustering parameters. Not a PPR tuning problem.
