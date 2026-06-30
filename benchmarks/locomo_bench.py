#!/usr/bin/env python3
"""
LoCoMo retrieval benchmark for 3am-claude.

What it measures
----------------
3am-claude is a *retrieval* system, not an answer generator. So instead of
end-to-end QA accuracy (which mixes in an LLM judge), we measure what 3am-claude
actually controls: **does query_memory surface the gold evidence turn(s)?**

LoCoMo gives every question an `evidence` list of dialog-turn ids (e.g. D1:3).
We ingest every conversation turn as one memory (tagged with its turn id, dated
with its session timestamp), then for each question check whether the retrieved
memories include the gold evidence turns. Reported as recall@k, broken down by
category — category 2 is *temporal*, the thing a temporal layer would fix.

Notes
-----
- Throwaway temp DB; never touches the live memory.db.
- One project_id per conversation (exercises project isolation realistically).
- Dedup + auto-promotion are disabled so no turn is silently merged or moved.
- recall@k is NOT directly comparable to published end-to-end LoCoMo accuracy
  (Zep ~85% etc.) — those are LLM-judged answers. This is a retrieval yardstick
  for measuring 3am-claude against *itself* across versions.

Usage:
  .venv/bin/python benchmarks/locomo_bench.py [--max-samples N] [--k 5,10,20]
"""
import argparse
import ast
import asyncio
import json
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory as memmod
from memory import MemorySystem

CAT_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain",
             4: "single-hop", 5: "adversarial"}
TURN_RE = re.compile(r"D\d+:\d+")


def parse_evidence(ev) -> list:
    """LoCoMo evidence is a stringified list like "['D1:3', 'D2:5']"."""
    if ev is None:
        return []
    if isinstance(ev, list):
        items = ev
    else:
        try:
            items = ast.literal_eval(str(ev))
            if not isinstance(items, list):
                items = [items]
        except Exception:
            items = [ev]
    out = []
    for it in items:
        out.extend(TURN_RE.findall(str(it)))
    return out


def load_turns(conv: dict) -> list:
    """Flatten all sessions into [(dia_id, dated_text)] in order."""
    turns = []
    i = 1
    while f"session_{i}" in conv:
        date = conv.get(f"session_{i}_date_time", "")
        for t in conv[f"session_{i}"]:
            text = t.get("text", "")
            if not text:
                continue
            speaker = t.get("speaker", "")
            dia_id = t.get("dia_id", f"D{i}:?")
            dated = f"[{date}] {speaker}: {text}"
            turns.append((dia_id, dated))
        i += 1
    return turns


async def run(dataset: Path, max_samples: int, k_list: list) -> dict:
    data = json.loads(dataset.read_text())
    if max_samples:
        data = data[:max_samples]
    max_k = max(k_list)

    # Disable dedup + auto-promotion so the benchmark corpus is faithful.
    memmod.DEDUP_DISTANCE = -1.0

    db = Path(tempfile.mkdtemp(prefix="locomo_")) / "bench.db"
    mem = MemorySystem(db_path=db, encryptor=None,
                       clustering_config={"auto_promote": False})
    mem.initialize()

    # per-category accumulators: cat -> {k -> hits}, count, all_evidence hits
    any_hits = defaultdict(lambda: defaultdict(int))   # cat -> k -> count
    all_hits = defaultdict(lambda: defaultdict(int))
    counts = defaultdict(int)
    adversarial = 0
    skipped_no_ev = 0
    t0 = time.time()

    for si, sample in enumerate(data):
        proj = f"locomo_{sample.get('sample_id', si)}"
        turns = load_turns(sample["conversation"])
        # ingest
        for dia_id, text in turns:
            await mem.store_memory(text, project_id=proj, universe="declarative",
                                   priority=3, tags=[f"turn:{dia_id}"])
        ningest = len(turns)

        qa = sample.get("qa", [])
        for q in qa:
            cat = int(q.get("category", 0))
            gold = parse_evidence(q.get("evidence"))
            if cat == 5 and not gold:
                adversarial += 1
                continue
            if not gold:
                skipped_no_ev += 1
                continue
            counts[cat] += 1
            results = await mem.query_memory(
                query=q["question"], project_id=proj,
                limit=max_k, max_tokens=10_000_000,
                min_score=0.0, min_cosine=0.0,
            )
            ranked_turns = []
            for r in results:
                for tg in (r.get("tags") or []):
                    if isinstance(tg, str) and tg.startswith("turn:"):
                        ranked_turns.append(tg[5:])
            goldset = set(gold)
            for k in k_list:
                topk = set(ranked_turns[:k])
                if goldset & topk:
                    any_hits[cat][k] += 1
                if goldset <= topk and goldset:
                    all_hits[cat][k] += 1
        print(f"  [{si+1}/{len(data)}] {proj}: ingested {ningest} turns, "
              f"{len(qa)} questions  ({time.time()-t0:.0f}s elapsed)")

    # aggregate
    report = {"k_list": k_list, "per_category": {}, "overall": {},
              "adversarial_questions": adversarial,
              "skipped_no_evidence": skipped_no_ev,
              "elapsed_sec": round(time.time() - t0, 1)}
    tot = defaultdict(int)
    tot_all = defaultdict(int)
    ntot = 0
    for cat in sorted(counts):
        n = counts[cat]
        ntot += n
        entry = {"n": n, "name": CAT_NAMES.get(cat, str(cat))}
        for k in k_list:
            entry[f"recall@{k}_any"] = round(any_hits[cat][k] / n, 4) if n else 0.0
            entry[f"recall@{k}_all"] = round(all_hits[cat][k] / n, 4) if n else 0.0
            tot[k] += any_hits[cat][k]
            tot_all[k] += all_hits[cat][k]
        report["per_category"][CAT_NAMES.get(cat, str(cat))] = entry
    for k in k_list:
        report["overall"][f"recall@{k}_any"] = round(tot[k] / ntot, 4) if ntot else 0.0
        report["overall"][f"recall@{k}_all"] = round(tot_all[k] / ntot, 4) if ntot else 0.0
    report["overall"]["n"] = ntot
    return report


def print_report(rep: dict):
    ks = rep["k_list"]
    print("\n" + "=" * 72)
    print("LoCoMo retrieval benchmark — 3am-claude")
    print("=" * 72)
    header = f"{'category':<14}{'n':>5}  " + "  ".join(f"R@{k}(any)".rjust(10) for k in ks)
    print(header)
    print("-" * len(header))
    for name, e in rep["per_category"].items():
        row = f"{name:<14}{e['n']:>5}  " + "  ".join(
            f"{e[f'recall@{k}_any']*100:9.1f}%" for k in ks)
        print(row)
    print("-" * len(header))
    o = rep["overall"]
    print(f"{'OVERALL':<14}{o['n']:>5}  " + "  ".join(
        f"{o[f'recall@{k}_any']*100:9.1f}%" for k in ks))
    print(f"\nmulti-hop 'all-evidence' recall@{ks[-1]}: "
          f"{rep['per_category'].get('multi-hop',{}).get(f'recall@{ks[-1]}_all',0)*100:.1f}%")
    print(f"adversarial questions (excluded): {rep['adversarial_questions']}")
    print(f"elapsed: {rep['elapsed_sec']}s")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(Path(__file__).parent / "data" / "locomo10.json"))
    ap.add_argument("--max-samples", type=int, default=0, help="0 = all")
    ap.add_argument("--k", default="5,10,20")
    args = ap.parse_args()
    k_list = sorted(int(x) for x in args.k.split(","))

    rep = asyncio.run(run(Path(args.dataset), args.max_samples, k_list))
    print_report(rep)

    outdir = Path(__file__).parent / "results"
    outdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outfile = outdir / f"locomo_{stamp}.json"
    outfile.write_text(json.dumps(rep, indent=2))
    print(f"\nresults saved → {outfile}")


if __name__ == "__main__":
    main()
