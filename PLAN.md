# 3am-claude: MCP Memory Server for Claude Code

## What It Is

A lightweight offshoot of 3am-AI that gives Claude Code persistent, self-organizing
memory via the MCP protocol. Same core stack (embeddings, torque clustering, PPR/lanes),
but the FastAPI chat server is replaced by an MCP server.

Memory persists across Claude Code sessions and grows into a self-organizing knowledge
base about the projects Claude works on.

---

## Key Design Decisions (reviewed by architecture + MCP agents)

### 1. HTTP daemon, not stdio

Stdio is wrong for this use case. Each Claude Code session spawns a fresh stdio process,
killing all background threads and in-process state. Since we need persistent clustering +
embeddings loaded once, the server runs as a **persistent HTTP/SSE daemon** (Starlette app,
same pattern as 3am-AI's FastAPI server). Claude Code connects via HTTP MCP transport.

Run as: `uvicorn mcp_server:app --host 127.0.0.1 --port 8765`
Register in `.claude/settings.json` as an HTTP MCP server.

### 2. Inverted LLM architecture — Claude extracts, server stores

3am-AI calls a local llama.cpp to extract durable facts at write time. For 3am-claude,
**Claude Code is already the LLM** — it does the extraction and classification before
calling `store_memory`. The MCP server is a pure storage/retrieval engine.

Benefits:
- No llama.cpp dependency on the memory server (Phase 1 deployable without it)
- No 120-second timeout risk on write
- Faster writes (no LLM inference blocking the tool call)
- Cleaner separation of concerns

### 3. Single DB, project_id scoping with lane-topology isolation

One DB at `~/.local/share/3am-claude/memory.db`. Every memory row has a `project_id`
field (sha256[:16] of git root, or NULL for general/shared memories).

**Why single DB over per-project DBs**: Knowledge should transfer. Code style, response
preferences, ingested datasheets, and patterns learned in one project are useful in all
projects. A single DB lets general knowledge accumulate and be accessible everywhere —
more like how humans actually learn: skills transfer across contexts.

**Lane topology enforces the boundary:**
```
Project A ──semantic lanes──→ General (project_id=NULL) ←── Project B
                                       ↑
                            code style, datasheets,
                            response preferences,
                            cross-project patterns
```

PPR walk rules (enforced in `_ppr_expand`):
- Same project ↔ Same project: allowed
- Any project → General: allowed
- General → Any project: allowed
- Project A → Project B: **blocked** (no direct cross-project walks)

**Knowledge promotion**: When a memory is stored as project-specific but later proves
relevant across projects (e.g. a pattern generalizes), it can be promoted to
`project_id=NULL` — either explicitly via a `promote_to_general` tool call or
automatically when clustering finds it centroid-adjacent to memories from other projects.

**Claude passes project_id on every tool call** — a short hash of the current git root.
`session.py` provides a helper Claude Code can call once at session start.

### 4. Universe model reframed for coding

The episodic/declarative/procedural split is kept but reframed:

| Universe    | Content for Claude Code                                   | Decay   |
|-------------|-----------------------------------------------------------|---------|
| Declarative | Architecture decisions, module facts, API shapes, bugs    | Slow    |
| Procedural  | Codebase patterns: how tests work, error handling style   | Slow    |
| Episodic    | Current session context, in-progress work, what was tried | Fast    |

No local LLM extraction — Claude passes `universe` and `priority` directly.
In-progress work is KEPT (episodic, fast-decay) — unlike 3am-AI which filters it out.
Code snippets: stored with TTL=7 days, low retention score — decay naturally.

---

## Architecture

```
Claude Code (MCP HTTP client)
     |
     | MCP protocol (HTTP/SSE, localhost:8765)
     v
mcp_server.py              ← new: FastMCP + Starlette HTTP app
     |
     +-- memory.py          ← stripped: single-user, keyring-keyed encryption, asyncio locks
     +-- clustering.py      ← renamed from 3am_clustering.py
     +-- torque_clustering/
     +-- ingest.py          ← phase 2: doc ingestion tool
     +-- data_security.py   ← kept: Fernet encryption at rest
     |
     v
~/.local/share/3am-claude/memory.db  (single DB, encrypted blobs, project_id scoped)
```

Encrypted at rest (Fernet). Single DB. No multi-user. No local LLM at write time.

---

## MCP Tools (Exposed to Claude)

### `store_memory`
```
store_memory(
    content: str,
    project_id: str,                              # sha256[:16] of git root; None = general
    universe: "declarative" | "procedural" | "episodic",
    priority: int,                                # 1-5
    tags: list[str] = [],
    ttl_days: int = None                          # None = permanent; 7 for code snippets
) -> {id, cluster_theme}
```
Claude classifies before calling. Server embeds, deduplicates, stores, links lanes.

**Sensitive data**: Do NOT store raw secrets, API keys, passwords, or PII. Store only
reasoning and conclusions about them ("project uses bearer auth, token in .env") not
the values themselves. Server applies a lightweight pattern filter (regex for common
secret shapes) and rejects the write with a warning if triggered.

### `query_memory`
```
query_memory(
    query: str,
    project_id: str,         # current project — also retrieves general memories
    limit: int = 10,
    max_tokens: int = 2000,  # server truncates results to stay within budget
    min_score: float = 0.0   # drop results below this PPR score (use ~0.01–0.03)
) -> [{content, universe, score, cluster_theme, project_id}]
```
Hybrid FTS5 + vec retrieval, PPR expansion respecting lane topology.
Returns project-scoped + general memories. Never returns other projects' memories.
`max_tokens` prevents flooding the context window on a rich DB — server summarizes
or drops lowest-scored results to fit.
Cosine seed gate (internal, min_cosine=0.50) returns [] for off-topic queries before
PPR runs — prevents graph diffusion from amplifying noise into misleading results.

### `get_session_summary`
```
get_session_summary(project_id: str) -> [{theme, size, sample_memories, scope}]
```
Cluster themes for current project + general clusters. `scope` = "project" | "general".
Call at session start to orient quickly.

### `promote_to_general`
```
promote_to_general(memory_id: str) -> {ok}
```
Promotes a project-specific memory to general (project_id=NULL). Use when a pattern
clearly generalizes beyond one project.

### `wipe_episodic`
```
wipe_episodic(project_id: str) -> {wiped: int}
```
Wipe this session's episodic memories for this project. Called on clean session end.

### `delete_memory`
```
delete_memory(memory_id: str) -> {ok}
```
Hard delete a specific memory. Use when a stored fact is wrong or stale.

### `correct_memory`
```
correct_memory(memory_id: str, new_content: str) -> {id}
```
Replace content of an existing memory in-place, re-embeds, rebuilds lanes.
Preferred over delete+store when the memory_id is known — preserves cluster
membership and lane connections.

### `ingest_document` *(Phase 3)*
```
ingest_document(path: str, propositions: list[str], project_id: str = None) -> {stored: int}
```
Claude extracts propositions first, passes them in. `project_id=None` for datasheets/docs
that should be accessible across all projects.

---

## Session Lifecycle

```
HTTP daemon starts (systemd service or manual)
  → loads DB for project root on first tool call
  → embedder lazy-loaded on first write

Claude Code session starts:
  → calls get_session_summary() to orient
  → stores/queries throughout session

Session ends:
  → Claude calls wipe_episodic() (or server auto-decays them by TTL)
  → Declarative + procedural memories persist

Nightly (background asyncio task in daemon):
  → clustering pass when unclustered count > 20
  → PPR lane rebuild
```

---

## What Changes vs 3am-AI

### Encryption: keep but improve key management

3am-AI derives the key from a login password — requiring re-authentication after every
server restart. 3am-claude uses the **system keyring** instead (KWallet on KDE,
gnome-keyring on GNOME, secretstorage on Linux generally via `keyring` library).

Flow:
- First run: generate random 32-byte key → store in keyring under service "3am-claude"
- Every start: retrieve from keyring → key in memory only, never on disk
- No re-login needed after daemon restarts
- Bad actor with DB access cannot read or inject valid memories without the key
- Poisoned memory injection is prevented: garbage blobs fail Fernet decryption

Threat model this covers: local filesystem access (malware, shared machine, nosy process)
injecting or reading memories to influence Claude's behavior.

Dependency: `pip install keyring secretstorage` (secretstorage is the D-Bus backend on Linux)

### memory.py changes needed:
- [ ] Remove multi-user (drop `user_id` scoping everywhere, replace with `project_id`)
- [ ] Keep Fernet encryption — replace PBKDF2 password derivation with keyring key retrieval
- [ ] Keep `data_security.py` — used for Fernet encrypt/decrypt
- [ ] Replace `threading.Lock()` with `asyncio.Lock()` — critical for HTTP daemon
- [ ] Fix DB connections to be per-coroutine (not `threading.local()`)
- [ ] Remove `experience_log` table from schema
- [ ] Remove hardcoded `~/.config/3am/config.json` paths — parameterize via constructor
- [ ] Remove local LLM calls: `_extract_durable_facts`, `_generate_cluster_theme`,
      `regenerate_user_profile` → cluster themes can still use LLM but async/optional
- [ ] Add `project_id` field to memory schema (NULL = general/shared)
- [ ] Add `session_id` field to episodic memories for targeted wipe
- [ ] Add `wipe_session_episodic(session_id)` method
- [ ] Update `_ppr_expand` to enforce lane topology: block Project A → Project B walks
- [ ] Keep: clustering, PPR/lanes, retention scoring, FTS5+vec hybrid retrieval

### Renamed:
- `3am_clustering.py` → `clustering.py` (leading digit breaks `importlib.import_module`)
- Remove `use_new_clusterer` feature flag (always use new clusterer)

### New files:
- `mcp_server.py` — FastMCP tools, Starlette HTTP app, lifespan manager
- `session.py` — git root detection → project_id hash, session ID management

### Removed vs 3am-AI:
- server.py, auth.py, autonomous.py, research.py, introspection.py, scheduler.py
- behavior_profile.py, decision_gate.py, experience_log.py, self_improve.py
- commands.py, tools.py

---

## Implementation Phases

### Phase 1 — Core MCP server (deployable MVP)
1. Rename `3am_clustering.py` → `clustering.py`
2. Strip `memory.py`: remove multi-user, replace user_id with project_id, keep Fernet
   encryption with keyring key, fix asyncio locks, remove experience_log, parameterize config
3. Write `session.py`: git root detection → project_id hash
4. Write `mcp_server.py`: FastMCP HTTP app with `store_memory`, `query_memory`,
   `get_session_summary`, `wipe_episodic`, `delete_memory`, `correct_memory`
5. Add secret pattern filter to `store_memory` (regex: API keys, tokens, passwords)
6. Add `max_tokens` budget enforcement to `query_memory`
7. Write `config.json` with db path, clustering threshold, TTL defaults
8. Test: Claude Code can store, retrieve, delete, and correct memories

### Phase 2 — Clustering + lanes
7. Wire in `clustering.py` + torque_clustering
8. Background asyncio task: cluster when unclustered > 20
9. PPR lane walks active in query_memory

### Phase 3 — Document ingestion
10. `ingest_document` tool (Claude extracts propositions, server stores)

### Phase 4 — Polish
11. `install.sh` for local setup + systemd service file
12. Claude Code settings snippet for `.claude/settings.json`
13. `export_memories` CLI command — decrypts → JSON (backup/migration)
14. `import_memories` CLI command — JSON → encrypted DB (restore/new machine)
15. CLAUDE.md bootstrap: on first `get_session_summary` for a project, if a CLAUDE.md
    exists in the project root, auto-ingest it as declarative memories (warm start)
16. **SessionStart hook** — shell script that curls the memory server at session open,
    fetches `get_session_summary` + a broad `query_memory`, injects result as
    `hookSpecificOutput.additionalContext`. This orients Claude at session start without
    a tool call turn sitting in context for the rest of the session.
    - Hook type: `command` (SessionStart only supports command, not HTTP; use curl)
    - Matcher: `"startup"` and `"resume"` (re-inject on resume/compact)
    - Size limit: 40,000 chars — stay well under with tight `max_tokens` (~800)
    - Result: zero context cost for session orientation
17. README

---

## Context Management Research Notes

### `clear_tool_uses_20250919` (beta — not directly usable from Claude Code)

A context editing strategy on the raw Anthropic API that replaces old tool results with
placeholder text server-side. Would be ideal for memory queries (run, inform generation,
disappear from history). However:
- Requires `anthropic-beta: context-management-2025-06-27` header on raw API calls
- Not exposed in Claude Code settings.json or CLI flags — Claude Code manages its own
  API calls internally and doesn't pass this header
- No per-tool filtering — clearing is chronological (oldest first), with `exclude_tools`
  as the only selective control
- **Not actionable for us right now**, but worth revisiting if Claude Code exposes it

Config shape (for reference when it becomes accessible):
```json
{
  "context_management": {
    "edits": [{
      "type": "clear_tool_uses_20250919",
      "trigger": {"type": "input_tokens", "value": 30000},
      "keep": {"type": "tool_uses", "value": 3},
      "clear_at_least": {"type": "input_tokens", "value": 5000},
      "exclude_tools": ["store_memory"]
    }]
  }
}
```

### SessionStart hook `additionalContext` (actionable now)

Claude Code hooks can inject text into Claude's context at session start without it
appearing as a visible turn or persisting as a tool result in conversation history.

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-start.sh", "timeout": 10}]
    }]
  }
}
```

Hook script pattern:
```bash
#!/bin/bash
PROJECT_ID=$(python3 -c "from session import get_project_id; print(get_project_id() or '')")
SUMMARY=$(curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d "{\"tool\": \"get_session_summary\", \"project_id\": \"$PROJECT_ID\"}")
echo "{\"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\", \"additionalContext\": \"$SUMMARY\"}}"
```

### Security notes (implementation reminders)
- **Prompt injection via stored content**: Only store Claude's own reasoning/conclusions,
  never raw text from untrusted external files. The tool descriptions should make this
  explicit. Do not call `store_memory` with content directly lifted from files Claude
  has read — summarize and reason first.
- **Poisoned memory via filesystem**: Covered by Fernet encryption — garbage blobs fail
  decryption and are silently dropped at read time.
- **Key loss**: If keyring is wiped (OS reinstall etc.), encrypted DB is unrecoverable.
  The `export_memories` backup command is the mitigation.

---

## Config (`config.json`)

```json
{
  "db_path": "~/.local/share/3am-claude/memory.db",
  "llm_url": "http://localhost:8080",
  "server_port": 8765,
  "episodic_ttl_days": 1,
  "code_snippet_ttl_days": 7,
  "clustering_threshold": 20
}
```

`llm_url` is only used for optional async cluster theme generation — server works
without it (themes default to top-keyword summary).

---

## MCP Registration (Claude Code)

In `.claude/settings.json`:
```json
{
  "mcpServers": {
    "3am": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```
