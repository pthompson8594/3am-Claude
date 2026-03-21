#!/usr/bin/env python3
"""
3am-claude CLI — manage and inspect the memory database.

Usage:
  python cli.py export [-o memories.json] [--project-id <id>]
  python cli.py import -i memories.json
  python cli.py stats
  python cli.py decay [--dry-run]

Export writes decrypted memory content to JSON (no embeddings).
Import re-embeds each memory — the server does NOT need to be running.
Stats shows DB health: memory counts, cluster status, token estimate.
Decay runs TTL eviction and retention score cleanup (same as server startup).
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _build_memory_system(db_path: str):
    from memory import MemorySystem

    db = Path(db_path).expanduser()

    # Try keyring encryption (same logic as mcp_server.py)
    encryptor = None
    try:
        import keyring
        from data_security import DataEncryptor

        stored = keyring.get_password("3am-claude", "enc-key")
        if stored:
            encryptor = DataEncryptor(stored.encode())
    except Exception:
        pass

    ms = MemorySystem(db_path=db, encryptor=encryptor)
    ms.initialize()
    return ms


def _build_memory_system_no_cleanup(db_path: str):
    """Load MemorySystem without running _cleanup_expired (used by decay cmd)."""
    from memory import MemorySystem

    db = Path(db_path).expanduser()
    encryptor = None
    try:
        import keyring
        from data_security import DataEncryptor

        stored = keyring.get_password("3am-claude", "enc-key")
        if stored:
            encryptor = DataEncryptor(stored.encode())
    except Exception:
        pass

    ms = MemorySystem(db_path=db, encryptor=encryptor)
    conn = ms._get_conn()
    ms._init_db(conn)
    ms._load_from_db(conn)
    return ms


def cmd_stats(args):
    ms = _build_memory_system(args.db)
    memories = list(ms.memories.values())
    total = len(memories)

    if total == 0:
        print("No memories stored.")
        return

    by_universe: dict = {}
    by_project: dict = {}
    for e in memories:
        by_universe[e.universe] = by_universe.get(e.universe, 0) + 1
        key = e.project_id or "(general)"
        by_project[key] = by_project.get(key, 0) + 1

    n_clusters = len(ms.clusters)
    n_unclustered = ms.unclustered_count()
    oldest_ts = min(e.timestamp for e in memories)
    oldest_str = datetime.fromtimestamp(oldest_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    approx_tokens = sum(len(e.content) for e in memories) // 4

    last_cleanup = ms.stats.get("last_cleanup", 0)
    last_cleanup_str = (
        datetime.fromtimestamp(last_cleanup, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if last_cleanup else "never"
    )

    print(f"DB: {ms.db_file}")
    print(f"Memories: {total}  (~{approx_tokens} tokens)")
    print(f"Clusters: {n_clusters} ({n_unclustered} unclustered)")
    print()
    print("By universe:")
    for u in ("declarative", "procedural", "episodic"):
        cnt = by_universe.get(u, 0)
        if cnt:
            print(f"  {u:<12} {cnt}")
    print()
    print("By scope:")
    for scope, cnt in sorted(by_project.items(), key=lambda x: -x[1]):
        print(f"  {scope:<36} {cnt}")
    print()
    print(f"Oldest memory:  {oldest_str}")
    print(f"Last cleanup:   {last_cleanup_str}")


def cmd_decay(args):
    from memory import MESSAGE_RETENTION_THRESHOLD

    ms = _build_memory_system_no_cleanup(args.db)
    before_m = len(ms.memories)
    before_c = len(ms.clusters)
    current_time = time.time()

    if args.dry_run:
        to_delete = []
        for mid, entry in ms.memories.items():
            if entry.ttl_expires and current_time > entry.ttl_expires:
                to_delete.append((mid, "ttl"))
            elif ms._calculate_retention(entry, current_time) <= MESSAGE_RETENTION_THRESHOLD:
                to_delete.append((mid, "decayed"))
        remaining_ids = set(ms.memories) - {mid for mid, _ in to_delete}
        empty_clusters = sum(
            1 for c in ms.clusters.values()
            if not any(r in remaining_ids for r in c.memory_refs)
        )
        print(f"Dry run — would remove {len(to_delete)} memories, {empty_clusters} clusters")
        print(f"({before_m - len(to_delete)} memories would remain)")
    else:
        conn = ms._get_conn()
        ms._cleanup_expired(conn)
        removed_m = before_m - len(ms.memories)
        removed_c = before_c - len(ms.clusters)
        print(f"Removed {removed_m} memories, {removed_c} clusters")
        print(f"{len(ms.memories)} memories remaining")


async def cmd_export(args):
    ms = _build_memory_system(args.db)
    data = await ms.export_memories(project_id=args.project_id or None)
    out = Path(args.output)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Exported {data['count']} memories → {out}")


async def cmd_import(args):
    inp = Path(args.input)
    if not inp.exists():
        print(f"Error: {inp} not found", file=sys.stderr)
        sys.exit(1)
    with open(inp, encoding="utf-8") as f:
        data = json.load(f)
    version = data.get("version", 0)
    if version != 1:
        print(f"Warning: unexpected export version {version!r}", file=sys.stderr)
    ms = _build_memory_system(args.db)
    result = await ms.import_memories(data)
    print(
        f"Imported {result['imported']} memories"
        f" (skipped {result['skipped']} duplicates/expired)"
    )


def main():
    parser = argparse.ArgumentParser(
        description="3am-claude memory CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db",
        default="~/.local/share/3am-claude/memory.db",
        help="Path to memory DB (default: ~/.local/share/3am-claude/memory.db)",
    )

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("stats", help="Show DB health summary")

    dp = sub.add_parser("decay", help="Run TTL eviction and retention score cleanup")
    dp.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be removed without deleting",
    )

    ep = sub.add_parser("export", help="Export memories to JSON")
    ep.add_argument("-o", "--output", default="memories_export.json", metavar="FILE")
    ep.add_argument(
        "--project-id", default=None, metavar="ID",
        help="Export only memories for this project (default: export all)",
    )

    ip = sub.add_parser("import", help="Import memories from JSON (re-embeds)")
    ip.add_argument("-i", "--input", required=True, metavar="FILE")

    args = parser.parse_args()

    if args.command == "stats":
        cmd_stats(args)
    elif args.command == "decay":
        cmd_decay(args)
    elif args.command == "export":
        asyncio.run(cmd_export(args))
    elif args.command == "import":
        asyncio.run(cmd_import(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
