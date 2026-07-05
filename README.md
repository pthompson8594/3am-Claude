# 3am-claude

Persistent, self-organizing memory for Claude Code via MCP.

Claude Code sessions are stateless — every session starts fresh. 3am-claude fixes that. It runs as a local HTTP daemon that Claude connects to over MCP, storing and retrieving memories across sessions. Memory is project-scoped, encrypted at rest, and self-organizes into semantic clusters over time.

---

## How it works

- **Claude does the extraction.** Claude classifies and summarizes before writing — the server is a pure storage/retrieval engine, no local LLM required.
- **One DB, project-scoped.** All projects share `~/.local/share/3am-claude/memory.db`. Each memory carries a `project_id` (sha256[:16] of the git root). General memories (cross-project patterns, preferences) use `project_id=NULL` and are visible everywhere.
- **Semantic retrieval.** Queries use hybrid FTS5 + vector search, expanded with Personalized PageRank over semantic lanes. Project A memories cannot walk to Project B — only through the general pool.
- **Self-organizing clusters.** Every write triggers a debounced clustering pass (default 10s after the last write), keeping memories organized as they accumulate.
- **Encrypted at rest.** Fernet encryption keyed from the system keyring (KWallet, gnome-keyring). The key is never written to disk.
- **Auto-promotion.** When the same knowledge independently shows up in two different projects, it's general by definition. 3am-claude detects this at store time and lifts it into the shared pool automatically (high-confidence) or queues it for one-click review (borderline).

---

## Auto-promotion

Cross-project isolation means a project-scoped memory is *only* ever recalled by its own project — so "recalled in N projects" can never be the promotion signal. The real signal is **store-time cross-project similarity**: when you store a memory and a near-identical one already exists in a *different* project, that knowledge is general.

It's a hybrid policy, tuned by `config.json`:

| Best cross-project cosine | Spans ≥ `promote_min_projects` | Action |
|---|---|---|
| ≥ `promote_auto_cosine` (0.90) | yes | **Auto-promote** to general — logged + reversible |
| ≥ `promote_candidate_cosine` (0.82) | yes | **Queue** as a candidate for confirmation |
| below | — | left project-scoped |

Every auto-promotion is written to an audit log (`list_promotions`) and can be undone (`revert_promotion`) — it just restores the original `project_id`. Candidates are reviewed with `approve_promotion` / `dismiss_promotion`, or visually in the web UI. Set `"auto_promote": false` to make *everything* go through the review queue instead.

---

## Temporal memory

Facts change. 3am-claude handles time along two axes:

**Contradiction / supersession** (ported from the 3am-AI engine). When a fact is replaced — "we migrated from Flask to FastAPI", "moved from Saskatoon to Regina" — use `supersede_memory` (or `apply_correction` for an explicit user correction). The old memory isn't deleted: it's flagged `superseded_by`, **decays 10× faster**, and is **excluded from retrieval**, but stays in the DB for audit.

**Store-time conflict surfacing.** Cosine similarity can't tell "same fact, new value" from "two distinct facts on the same topic" — so the engine never auto-resolves contradictions (automatic conflict-detection is off by default; it wrongly retired distinct memories). Instead, when a newly stored memory lands semantically close to existing same-scope memories, `store_memory` returns them as `possible_conflicts` and *Claude* judges: same fact with an outdated value → `mark_superseded(old_id, new_id)`; distinct fact → leave it. Near-identical stores (above the dedup threshold) return the existing content with a `dedup_note` instead, so a value change is never silently swallowed by dedup.

**Provenance.** Every memory can carry a `source`: `user-stated` (authoritative), `inferred` (could be wrong), `observed-in-code` (drifts as code changes), or `ingested`. It's surfaced in every recall — `[declarative, user-stated, 3d ago]` — so a future session knows how much to trust what it's reading.

**Distrust flags.** When a recalled memory contradicts what Claude currently sees but the right value isn't known yet, `flag_memory(id, reason)` marks it suspect: demoted in query ranking, excluded from unprompted action-recall injection, queued in `list_flagged` for review. Resolve later with `unflag_memory`, `correct_memory`, `supersede_memory`, or `delete_memory`.

**Time-grounded retrieval.** Memories carry an optional `event_time` (when the fact is *about*, vs. the store-time `timestamp`), and temporal questions ("when did X", "before/after") automatically widen the retrieval beam. Combined with soft per-universe caps (unused slots backfill instead of being wasted), this substantially lifts recall on time-oriented queries — see [benchmarks/README.md](benchmarks/README.md) for the before/after on LoCoMo.

**Category-aware decay.** A memory's `category` (`identity`, `preferences`, `projects`, `activities`, …) scales its decay rate: who you are barely fades; a one-off activity fades normally.

---

## Web UI — memory map

A dependency-free visualizer is served by the daemon at **http://127.0.0.1:8765/ui**:

- A force-directed **memory map** — nodes are memories (hue = cluster, size = priority/access), general-pool memories ringed in gold, semantic lanes drawn between them, cluster themes floating as labels. Drag to pan, scroll to zoom, click a node for full content.
- A **Candidates** tab — the promotion review queue, with cross-project match previews and Promote / Dismiss buttons.
- A **Promotions** tab — the audit log, with one-click Revert.

It reads the local DB through the daemon (`/api/ui/*`); nothing leaves your machine. Styling mirrors the 3am web interface.

---

## Installation

```bash
git clone https://github.com/pthompson8594/3am-Claude.git
cd 3am-claude
./install.sh
```

`install.sh` will:
1. Create a virtualenv and install dependencies (including `threeam-core` from a sibling `3am-AI` directory)
2. Create `~/.local/share/3am-claude/`
3. Install a systemd user service (if systemd is available)
4. Write `~/.claude/hooks/3am-session-start.sh` (CLAUDE.md bootstrap + session orientation)
5. Write `~/.claude/hooks/3am-prompt-context.sh` (per-prompt memory injection)
6. Write `~/.claude/hooks/3am-stop.sh` (importance-aware memory extraction)
   and install bundled skills (e.g. `3am-consolidate`) into `~/.claude/skills/`
7. Write `~/.claude/hooks/3am-precompact.sh` (pre-compaction memory flush)
8. Write `~/.claude/hooks/3am-session-stop.sh` (recluster + episodic cleanup)
9. Print registration instructions for `~/.claude/settings.json`

### Register in Claude Code

**1. Register the MCP server.** MCP servers are *not* read from `settings.json` — they live in `~/.claude.json` (user scope) or a project `.mcp.json`. Use the CLI (writes to `~/.claude.json` for all projects):

```bash
claude mcp add --transport http --scope user 3am http://127.0.0.1:8765/mcp
```

Or, if the `claude` CLI isn't on your PATH (e.g. VS Code extension only), add it to `~/.claude.json` by hand under the top-level `mcpServers` key:

```json
{
  "mcpServers": {
    "3am": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

**2. Register the hooks** in `~/.claude/settings.json` (hooks *are* read from here):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-start.sh", "timeout": 10}]
    }],
    "UserPromptSubmit": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-prompt-context.sh", "timeout": 8}]
    }],
    "Stop": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-stop.sh", "timeout": 10}]
    }],
    "PreCompact": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-precompact.sh", "timeout": 10}]
    }],
    "SessionEnd": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-stop.sh", "timeout": 15}]
    }]
  }
}
```

### Start the server

```bash
# If systemd was installed:
systemctl --user start 3am-claude
systemctl --user status 3am-claude

# Or manually:
.venv/bin/uvicorn mcp_server:app --host 127.0.0.1 --port 8765
```

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `store_memory` | Store a fact. Claude classifies universe, priority, and provenance (`source`); returns `possible_conflicts` for judgment when neighbors are suspiciously close. |
| `query_memory` | Retrieve relevant memories for a query. Hybrid FTS5 + vector + PPR. |
| `get_session_summary` | Cluster themes for the current project + general pool. Call at session start. |
| `ingest_document` | Batch-store a list of propositions (Claude extracts them from docs). |
| `correct_memory` | Replace memory content in-place, re-embeds, rebuilds lanes (also clears any distrust flag). |
| `supersede_memory` | Record that a fact *changed* — stores the new value, fades the old one (kept for audit). |
| `mark_superseded` | Mark an existing memory as replaced by another already-stored one — the resolution path for `possible_conflicts`. |
| `apply_correction` | Apply a user correction: find the memory matching the wrong claim and supersede it. |
| `flag_memory` | Mark a memory as suspect (no correct value needed yet): demoted in ranking, excluded from action recall, queued for review. |
| `unflag_memory` | Clear a distrust flag — the memory turned out to be fine. |
| `list_flagged` | The distrust review queue, newest first. |
| `resolve_conflicts` | Run the contradiction pass now (same-category, keep newer). Also runs during clustering. |
| `list_compression_candidates` | List verbose memories worth rewriting tighter (context economy). |
| `delete_memory` | Hard-delete a specific memory by ID. |
| `promote_to_general` | Promote a project-specific memory to the general pool. |
| `list_promotion_candidates` | List borderline cross-project matches queued for promotion review. |
| `approve_promotion` | Approve a queued candidate — move it to the general pool (reversible). |
| `dismiss_promotion` | Dismiss a queued candidate without promoting. |
| `list_promotions` | The promotion audit log (auto + approved). |
| `revert_promotion` | Undo a promotion — return the memory to its original project. |
| `trigger_clustering` | Manually trigger a full clustering pass without waiting for the background debounce. |
| `wipe_episodic` | Wipe episodic memories for the current project (call on session end). |

### Memory universes

| Universe | Use for | Decay |
|----------|---------|-------|
| `declarative` | Architecture decisions, module facts, API shapes, bugs | Slow |
| `procedural` | Codebase patterns: how tests work, error handling style | Slow |
| `episodic` | Current session context, in-progress work, what was tried | Fast (1 day TTL) |

### Priority scale

`5` = permanent · `4` = months · `3` = weeks · `2` = days · `1` = skip

---

## Hooks

**SessionStart** (`~/.claude/hooks/3am-session-start.sh`)
Fires when Claude Code opens. Bootstraps CLAUDE.md on first project visit and injects cluster-level orientation context.

**UserPromptSubmit** (`~/.claude/hooks/3am-prompt-context.sh`)
Fires before every user prompt. Queries the daemon with the prompt text and injects the top 4–5 relevant memories as `additionalContext`. Replaces static session summaries with per-prompt targeted recall — every turn gets the memories most relevant to what you just asked.

**Stop** (`~/.claude/hooks/3am-stop.sh`)
Fires after every Claude response and is **importance-aware** — it reads the transcript and decides whether to block for extraction based on what just happened, not a blind turn counter:
- **Correction** in the last user turn (the highest-value moment to learn) → always block, asking Claude to store the *lesson* as an actionable rule.
- **Work turn** (the response used tools/edits) → block every 2nd such turn (`THREEAM_STOP_EVERY_WORK`, default 2).
- **Idle / chitchat** → block rarely (`THREEAM_STOP_EVERY_IDLE`, default 5).

The follow-up stop (`stop_hook_active: true`) passes through. PreCompact remains the safety net before any context is lost, so this hook can stay light.

**PreCompact** (`~/.claude/hooks/3am-precompact.sh`)
Fires right before context is compacted — the one moment session detail is actually destroyed. PreCompact supports `decision: block` (but not `additionalContext`), so on **auto**-compaction it blocks once with a prompt to checkpoint in-flight state, then allows it on the retry. A flag file at `/tmp/3am-precompact-{session_id}` guarantees compaction is only blocked once per event, so auto-compaction can never be deadlocked. **Manual** `/compact` is never blocked — it doesn't auto-retry, so blocking it would force the user to run `/compact` twice; the hook checks the payload's `trigger` field and passes manual compaction straight through.

**SessionEnd** (`~/.claude/hooks/3am-session-stop.sh`)
Fires when the session ends. Triggers a full recluster (incorporating memories stored this session) and wipes episodic memories for the session.

**PreToolUse + PostToolUse** (`~/.claude/hooks/3am-recall.sh` → `hooks/recall_hook.py`)
**Action-triggered recall.** Standard recall is *input*-triggered — memories are matched against your prompt. But mid-task Claude's reasoning drifts to subtopics the prompt never mentioned, and the relevant memory is missed. This hook makes recall follow what Claude is *doing*: before an Edit/Write and after a Read/Edit/Grep, it derives a signal from the activity (the code being changed, the file, the search pattern) and surfaces relevant memories — so a stored lesson appears exactly when Claude reaches for the thing it's about (e.g. editing the stop hook surfaces the stop-hook lesson). To stay quiet, it uses a **precision** retrieval path (`recall_precise`: pure vector cosine gate, no FTS/PPR — the opposite tuning from prompt recall), the shared seen-set (below), and it skips orientation memories already in the session context. `recall_min_cosine` (default `0.60`) is the relevance gate; raise it for fewer, surer hits.

**Shared per-session seen-set.** All injection surfaces (per-prompt recall *and* action-triggered recall) dedup against one server-side seen-set keyed by session: a memory is injected **at most once per session**, no matter which path surfaces it first. Without this, the same memory could arrive twice through different surfaces — and noise, not missed recall, is what teaches a model to ignore injections. The set is dropped at SessionEnd (stale sessions expire after 48h).

---

## CLI

```bash
.venv/bin/python cli.py query '<text>'               # query the live server
.venv/bin/python cli.py stats                        # DB health: counts, cluster summary
.venv/bin/python cli.py export -o backup.json        # decrypt all memories → JSON
.venv/bin/python cli.py export --project-id <id> -o backup.json
.venv/bin/python cli.py import -i backup.json        # restore from backup
.venv/bin/python cli.py ingest <file>                # ingest a document
.venv/bin/python cli.py cluster                      # trigger a manual recluster
.venv/bin/python cli.py wipe                         # wipe episodic memories for current project
```

---

## Configuration

Copy `config.default.json` to `~/.local/share/3am-claude/config.json` and edit:

```json
{
  "db_path": "~/.local/share/3am-claude/memory.db",
  "server_port": 8765,
  "episodic_ttl_days": 1,
  "code_snippet_ttl_days": 7,
  "debounce_seconds": 10,
  "min_cosine": 0.5,
  "recall_min_cosine": 0.60,
  "llm_url": "http://localhost:8080"
}
```

`llm_url` is only used for optional async cluster theme generation. The server works without it — themes fall back to top-keyword summaries.

`debounce_seconds` controls how long the server waits after the last write before triggering a clustering pass. Each new write resets the timer, so rapid batches of memories coalesce into a single recluster.

`min_cosine` is the internal cosine similarity gate — queries with no seed above this threshold return `[]` before PPR runs, preventing noise amplification. `0.50` is calibrated for `nomic-embed-text-v1.5`. If you switch embedding models, recalibrate by running off-topic and on-topic queries and finding the threshold where off-topic returns `[]` without clipping real results.

---

## Security

- **Encryption at rest:** All memory content is Fernet-encrypted. The key lives in the system keyring — never on disk. A process with filesystem access cannot read or inject memories without the key.
- **Secret filter:** `store_memory` rejects writes that match patterns for API keys, tokens, and passwords. Store reasoning and conclusions, not secrets.
- **Cross-project isolation:** PPR lane walks block Project A → Project B traversal. Each project only sees its own memories and the general pool.
- **Key loss:** If the keyring is wiped (OS reinstall etc.), the encrypted DB is unrecoverable. Run `cli.py export` periodically as a backup.

---

## Troubleshooting

**Server not connecting**
```bash
curl http://127.0.0.1:8765/health
systemctl --user status 3am-claude
journalctl --user -u 3am-claude -f
```

**Port conflict** — change `server_port` in `config.json` and update the URL in `settings.json` and `3am-session-start.sh`.

**Keyring unavailable** (headless/CI) — set `SECRET_KEY_3AM` as an environment variable; the server uses it before touching the keyring.

**Encrypted content shows as `gAAAAAB…` ciphertext / garbled cluster labels** — the daemon started before the secret service (kwallet/gnome-keyring) was up and ran without the key. It no longer *silently* does this: with encrypted data present it now retries the keyring and, if still unavailable, **fails startup** so systemd restarts it until the secret service is ready (`After=graphical-session.target`, `StartLimitIntervalSec=0`, `Restart=on-failure`). If you see it, just ensure the keyring is unlocked and `systemctl --user restart 3am-claude`; then `trigger_clustering` to refresh any themes computed while it was down. The key is not lost — the data decrypts once the daemon reattaches to it.

**Stale cluster themes** — run `cli.py cluster` or call `trigger_clustering` from inside Claude Code.

**Key loss** — the daemon will **never generate a new key or run unencrypted when encrypted memories already exist** (it fails loudly instead), so a boot-time keyring hiccup can't orphan your data. Genuine key loss only happens if the keyring entry itself is wiped (`service=3am-claude`, `user=enc-key`); run `cli.py export` periodically as a backup, or set a stable `SECRET_KEY_3AM`.

---

## Requirements

- Python 3.11+
- Linux (systemd optional; KWallet or gnome-keyring recommended for keyring)
- ~2 GB disk for the embedding model (`nomic-ai/nomic-embed-text-v1.5`, downloaded on first use)
