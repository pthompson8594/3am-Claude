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

---

## Installation

```bash
git clone <repo>
cd 3am-claude
./install.sh
```

`install.sh` will:
1. Create a virtualenv and install dependencies
2. Create `~/.local/share/3am-claude/`
3. Install a systemd user service (if systemd is available)
4. Write `~/.claude/hooks/3am-session-start.sh` (session orientation hook)
5. Write `~/.claude/hooks/3am-post-tool-use.sh` (memory capture nudge hook)
6. Write `~/.claude/hooks/3am-session-stop.sh` (episodic cleanup hook)
7. Print registration instructions for `~/.claude/settings.json`

### Register in Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "3am": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  },
  "hooks": {
    "SessionStart": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-start.sh", "timeout": 10}]
    }],
    "PostToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-post-tool-use.sh", "timeout": 5}]
    }],
    "StopSession": [{
      "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-stop.sh", "timeout": 10}]
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
| `store_memory` | Store a fact. Claude classifies universe and priority before calling. |
| `query_memory` | Retrieve relevant memories for a query. Hybrid FTS5 + vector + PPR. |
| `get_session_summary` | Cluster themes for the current project + general pool. Call at session start. |
| `ingest_document` | Batch-store a list of propositions (Claude extracts them from docs). |
| `correct_memory` | Replace memory content in-place, re-embeds, rebuilds lanes. |
| `delete_memory` | Hard-delete a specific memory by ID. |
| `promote_to_general` | Promote a project-specific memory to the general pool. |
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
Fires when Claude Code opens. Calls `/api/session-context` on the daemon, which returns cluster themes and sample memories as `additionalContext`. Claude is oriented before the first turn — no tool call turn sitting in context.

**PostToolUse** (`~/.claude/hooks/3am-post-tool-use.sh`)
Fires after every `Write` or `Edit` tool call. Injects a brief nudge reminding Claude to consider storing architectural decisions or patterns. Does not auto-store — just surfaces the memory system at decision points.

**StopSession** (`~/.claude/hooks/3am-session-stop.sh`)
Fires when the Claude Code session ends. Reads the `session_id` from the hook payload and POSTs to `/api/session-stop` on the daemon, which wipes all episodic memories tagged with that session ID. Closes the session lifecycle — no need to manually call `wipe_episodic`.

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
  "llm_url": "http://localhost:8080"
}
```

`llm_url` is only used for optional async cluster theme generation. The server works without it — themes fall back to top-keyword summaries.

`debounce_seconds` controls how long the server waits after the last write before triggering a clustering pass. Each new write resets the timer, so rapid batches of memories coalesce into a single recluster.

`min_cosine` is the internal cosine similarity gate — queries with no seed above this threshold return `[]` before PPR runs, preventing noise amplification. `0.50` is calibrated for `nomic-embed-text-v1.5`. If you switch embedding models, recalibrate using the process in [PPR_PRECISION_CHANGES.md](PPR_PRECISION_CHANGES.md).

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

**Keyring unavailable** (headless/CI) — set `SECRET_KEY_3AM` as an environment variable; the server falls back to it if the keyring is inaccessible.

**Stale cluster themes** — run `cli.py cluster` or call `trigger_clustering` from inside Claude Code.

**Key loss** — if the keyring is wiped, use a `cli.py export` backup to recover memories on a new machine.

---

## Requirements

- Python 3.11+
- Linux (systemd optional; KWallet or gnome-keyring recommended for keyring)
- ~2 GB disk for the embedding model (`nomic-ai/nomic-embed-text-v1.5`, downloaded on first use)
