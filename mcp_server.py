#!/usr/bin/env python3
"""
3am-claude MCP Server — persistent HTTP/SSE daemon.

Run:
    uvicorn mcp_server:app --host 127.0.0.1 --port 8765

Register in .claude/settings.json:
    {
      "mcpServers": {
        "3am": {
          "type": "http",
          "url": "http://127.0.0.1:8765/mcp"
        }
      }
    }

Architecture:
  - FastMCP creates an ASGI app served by uvicorn
  - MemorySystem is initialized once at startup (lifespan)
  - Embedder lazy-loads on first write, stays resident
  - Background asyncio task clusters when unclustered count > threshold
  - Fernet encryption key stored in system keyring (KWallet/gnome-keyring)
"""

import asyncio
import json
import re
import urllib.parse
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from memory import MemorySystem
from session import new_session_id

# ── Secret pattern filter ─────────────────────────────────────────────────────

_SECRET_PATTERNS = [
    # key=value style assignments
    re.compile(
        r'(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token'
        r'|password|passwd|private[_-]?key)\s*[=:]\s*\S{8,}'
    ),
    re.compile(r'\bsk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b'),  # OpenAI / Anthropic keys
    re.compile(r'\bghp_[A-Za-z0-9]{36,}\b'),                 # GitHub PATs
    re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}\b'),             # GitLab PATs
    re.compile(r'-----BEGIN [A-Z ]+PRIVATE KEY-----'),        # PEM private keys
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),                     # AWS access key IDs
    re.compile(r'(?i)(bearer|authorization):\s*[A-Za-z0-9\-_.~+/]{20,}'),
]


def _secrets_warning(content: str) -> Optional[str]:
    """Return an error string if content appears to contain raw secrets, else None."""
    for pat in _SECRET_PATTERNS:
        if pat.search(content):
            return (
                "Content appears to contain secrets or credentials. "
                "Store only reasoning and conclusions (e.g. 'project uses bearer auth, "
                "token in .env'), never raw values. Write rejected."
            )
    return None


# ── Config + encryption ───────────────────────────────────────────────────────

class _FernetEncryptor:
    """
    Minimal Fernet encryptor — a vendored fallback for data_security.DataEncryptor
    (from threeam-core) so encryption at rest still works when that package isn't
    installed. Matches the subset of the interface MemorySystem uses:
    `.config.enabled`, `.encrypt_str`, `.decrypt_str`.
    """

    class _Config:
        def __init__(self, enabled: bool):
            self.enabled = enabled

    def __init__(self, user_key: Optional[bytes]):
        from cryptography.fernet import Fernet
        self._fernet = Fernet(user_key) if user_key else None
        self.config = self._Config(enabled=user_key is not None)

    def encrypt_str(self, s: str) -> str:
        return self._fernet.encrypt(s.encode()).decode() if self._fernet else s

    def decrypt_str(self, s: str) -> str:
        if not self._fernet:
            return s
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(s.encode()).decode()
        except (InvalidToken, Exception):
            # Legacy plaintext written before encryption was enabled — return as-is.
            return s



def _load_config() -> dict:
    """Load config from the first found config file."""
    search = [
        Path.home() / ".local/share/3am-claude/config.json",
        Path(__file__).parent / "config.json",
        Path(__file__).parent / "config.default.json",
    ]
    for p in search:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    return {}


def _get_encryptor():
    """
    Load (or generate) the Fernet key from the system keyring.
    Returns a DataEncryptor instance, or None if keyring is unavailable.

    Key storage: system keyring under service="3am-claude", username="enc-key".
    First run: generates a random Fernet key and stores it.
    Subsequent runs: retrieves the stored key.

    Threat model: protects against filesystem access (malware, shared machine).
    A valid Fernet blob cannot be forged without the key — poisoned injections fail.
    """
    try:
        import keyring
        from cryptography.fernet import Fernet

        # Prefer the full DataEncryptor from threeam-core; fall back to the vendored
        # _FernetEncryptor so encryption still works without that package installed.
        try:
            from data_security import DataEncryptor
        except ImportError:
            DataEncryptor = _FernetEncryptor

        service = "3am-claude"
        username = "enc-key"
        stored = keyring.get_password(service, username)
        if stored:
            key = stored.encode()
        else:
            key = Fernet.generate_key()
            keyring.set_password(service, username, key.decode())
            print("[3am-claude] Generated new encryption key in system keyring.")
        return DataEncryptor(key)
    except Exception as e:
        print(f"[3am-claude] Keyring unavailable ({e}). Running without encryption.")
        return None


# ── Globals ───────────────────────────────────────────────────────────────────

_memory: Optional[MemorySystem] = None
_config: dict = {}
_bg_task: Optional[asyncio.Task] = None
_debounce_task: Optional[asyncio.Task] = None


# ── Clustering helpers ────────────────────────────────────────────────────────

async def _debounced_cluster(delay: float):
    """Wait for writes to settle, then run a full clustering pass."""
    await asyncio.sleep(delay)
    if _memory:
        print("[3am-claude] Write-triggered clustering...")
        result = await _memory.run_clustering()
        print(f"[3am-claude] Write clustering result: {result}")
        _memory.save_stats()


def _schedule_cluster():
    """Schedule a debounced clustering pass. Resets timer on each call."""
    global _debounce_task
    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    delay = _config.get("debounce_seconds", 10.0)
    _debounce_task = asyncio.create_task(_debounced_cluster(delay))


# ── Background task ───────────────────────────────────────────────────────────

async def _background_loop():
    """Full re-cluster every hour, regardless of unclustered count."""
    while True:
        await asyncio.sleep(3600)
        try:
            if _memory:
                n = len(_memory.memories)
                print(f"[3am-claude] Hourly recluster ({n} memories)...")
                result = await _memory.run_clustering()
                print(f"[3am-claude] Hourly recluster result: {result}")
                _memory.save_stats()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[3am-claude] Background clustering error: {e}")


# ── FastMCP app ───────────────────────────────────────────────────────────────
# No lifespan= here — _CompositeApp handles the ASGI lifespan at server startup
# so _memory is ready before the first request (including hook calls before any
# MCP session has been created).

mcp = FastMCP(
    name="3am-memory",
    instructions=(
        "Persistent memory system for Claude Code. "
        "Call get_session_summary at session start to orient. "
        "Use store_memory to save facts you want to remember across sessions. "
        "Use query_memory to retrieve relevant context before answering. "
        "Never store raw secrets, API keys, passwords, or PII — only reasoning and conclusions."
    ),
)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def store_memory(
    content: str,
    project_id: Optional[str],
    universe: str,
    priority: int,
    tags: Optional[list] = None,
    ttl_days: Optional[int] = None,
    session_id: Optional[str] = None,
    event_time: Optional[float] = None,
    category: str = "general",
) -> dict:
    """
    Store a memory in the persistent knowledge base.

    Classify before calling:
      universe: "declarative" — architecture decisions, module facts, API shapes, bugs
                "procedural"  — codebase patterns: how tests work, error handling style
                "episodic"    — current session context, in-progress work, what was tried
      priority: 5=permanent, 4=months, 3=weeks, 2=days, 1=skip
      project_id: sha256[:16] of git root (from session.get_project_id()), or None for
                  general/cross-project memories (code style, preferences, datasheets)
      ttl_days: None=permanent, 7=code snippets that will become stale
      session_id: current session ID — tag episodic memories so wipe_episodic can clean up
      event_time: epoch seconds the fact is ABOUT (e.g. when an event happened),
                  for time-grounded recall ("when did X"). None → uses store time.
      category: identity|preferences|relationship|projects|activities|general|skill —
                drives category-aware decay (identity/preferences resist decay; a
                project or activity is time-bound and fades normally).

    Memory hygiene: when you complete work that was previously stored as "planned",
    "todo", or "not yet implemented", use correct_memory or delete_memory to update
    those entries in the same turn — do not leave stale planned-feature memories in the DB.
    When a previously-true fact has CHANGED (not just a typo fix), use supersede_memory
    so the old value fades but stays auditable.

    Security: Do NOT store raw secrets, API keys, passwords, or PII.
    Store only reasoning and conclusions (e.g. "project uses bearer auth, token in .env").
    """
    if not content or not content.strip():
        return {"error": "content cannot be empty"}

    warning = _secrets_warning(content)
    if warning:
        return {"error": warning}

    result = await _memory.store_memory(
        content=content.strip(),
        project_id=project_id or None,
        universe=universe,
        priority=priority,
        tags=tags or [],
        ttl_days=ttl_days,
        session_id=session_id,
        event_time=event_time,
        category=category,
    )
    if "id" in result:
        _schedule_cluster()
    return result


@mcp.tool()
async def supersede_memory(
    old_memory_id: str,
    new_content: str,
    priority: Optional[int] = None,
    event_time: Optional[float] = None,
) -> dict:
    """
    Record that a fact CHANGED over time. Stores new_content as a fresh memory
    (inheriting the old one's project/universe/category) and marks the old one
    superseded — it fades fast (10× decay) and drops out of retrieval but stays
    in the DB for audit. Use for "we migrated from X to Y", "renamed A to B",
    "moved from Saskatoon to Regina". For a simple correction of a wrong value
    in place, use correct_memory instead.

    Returns {ok, new_memory_id, superseded} or {ok: false, error}
    """
    if not new_content or not new_content.strip():
        return {"error": "new_content cannot be empty"}
    warning = _secrets_warning(new_content)
    if warning:
        return {"error": warning}
    result = await _memory.supersede_memory(
        old_memory_id, new_content.strip(), priority=priority, event_time=event_time)
    if result.get("ok"):
        _schedule_cluster()
    return result


@mcp.tool()
async def apply_correction(
    wrong_claim: str,
    correct_fact: str,
    project_id: Optional[str],
) -> dict:
    """
    Apply an explicit user correction. Finds the stored memory closest to
    wrong_claim (within this project's visibility) and supersedes it with a
    high-priority memory of correct_fact. If no close match exists, just stores
    correct_fact. Use when the user says something previously stored was wrong
    (e.g. "no, it's 3,000 acres not 2,000").

    Returns {ok, corrected, superseded?, new_memory_id}
    """
    if not correct_fact or not correct_fact.strip():
        return {"error": "correct_fact cannot be empty"}
    warning = _secrets_warning(correct_fact)
    if warning:
        return {"error": warning}
    result = await _memory.apply_correction(
        wrong_claim or "", correct_fact.strip(), project_id or None)
    if result.get("ok"):
        _schedule_cluster()
    return result


@mcp.tool()
async def list_compression_candidates(project_id: Optional[str] = None) -> list:
    """
    List verbose memories worth compressing (long, non-episodic, not superseded).
    Read each and rewrite it tighter via correct_memory to keep recall context
    efficient as the DB grows. Returns [{id, chars, content, universe, priority}].
    """
    return await _memory.list_compression_candidates(project_id or None)


@mcp.tool()
async def query_memory(
    query: str,
    project_id: Optional[str],
    limit: int = 10,
    max_tokens: int = 2000,
    min_score: float = 0.0,
) -> list:
    """
    Query the memory system for relevant context.

    Returns project-scoped + general memories. Never returns other projects' memories.
    Results are sorted by PPR relevance score.

      query:      natural language query
      project_id: current project (also retrieves general/shared memories)
      limit:      max results to return (default 10, max 20)
      max_tokens: approximate token budget for all results (default 2000)
      min_score:  minimum PPR score to include — results below this are dropped even
                  if under the limit. Use ~0.01-0.03 to cut low-signal padding.

    Temporal questions ("when did X", "before/after") automatically widen the
    retrieval beam. Superseded (replaced/corrected) memories are excluded.

    Each result: {id, content, universe, score, cluster_theme, project_id, tags,
                  priority, category, event_time, timestamp}
    """
    if not query or not query.strip():
        return []
    return await _memory.query_memory(
        query=query.strip(),
        project_id=project_id or None,
        limit=min(limit, 20),
        max_tokens=max_tokens,
        min_score=min_score,
    )


@mcp.tool()
async def get_session_summary(project_id: Optional[str]) -> list:
    """
    Get a summary of stored knowledge for the current project.

    Call this at the start of each session to orient quickly.
    Returns cluster themes grouped by scope:
      [{theme, size, sample_memories, scope: "project"|"general", cluster_id}]

    project_id: current project (from session.get_project_id())
    """
    return await _memory.get_session_summary(project_id or None)


@mcp.tool()
async def promote_to_general(memory_id: str) -> dict:
    """
    Promote a project-specific memory to general (project_id=NULL).

    Use when a pattern clearly generalizes beyond one project — e.g. a code style
    preference, a debugging pattern, or a tool preference learned in one project
    that applies everywhere.

    Returns {ok: true} or {ok: false, error: "..."}
    """
    return await _memory.promote_to_general(memory_id)


@mcp.tool()
async def list_promotion_candidates() -> list:
    """
    List pending auto-promotion candidates: project memories that closely match
    knowledge in OTHER projects but weren't auto-promoted (borderline similarity).

    Each candidate shows the memory, its best cross-project cosine, the projects
    the concept spans, and previews of the matching memories. Review these and
    call approve_promotion (move to the general pool) or dismiss_promotion.

    Returns [{candidate_id, memory_id, content, best_cosine, projects, matches}]
    """
    return await _memory.list_promotion_candidates()


@mcp.tool()
async def approve_promotion(candidate_id: int) -> dict:
    """
    Approve a queued promotion candidate — moves its memory to the general pool
    (project_id=NULL) so it's visible across all projects. Logged and reversible.

    Returns {ok: true, memory_id} or {ok: false, error: "..."}
    """
    return await _memory.approve_promotion(candidate_id)


@mcp.tool()
async def dismiss_promotion(candidate_id: int) -> dict:
    """
    Dismiss a queued promotion candidate without promoting it. Use when the match
    is coincidental and the memory should stay project-scoped.

    Returns {ok: true} or {ok: false, error: "..."}
    """
    return await _memory.dismiss_promotion(candidate_id)


@mcp.tool()
async def list_promotions() -> list:
    """
    The promotion audit log: memories that have been moved to the general pool
    automatically or by approval. Use to review or find a promotion to revert.

    Returns [{promotion_id, memory_id, content, original_project, projects,
              best_cosine, trigger, timestamp, reverted}]
    """
    return await _memory.list_promotions()


@mcp.tool()
async def revert_promotion(promotion_id: int) -> dict:
    """
    Undo a promotion — returns the memory to its original project scope. Use when
    a memory was promoted to general but turns out to be project-specific.

    Returns {ok: true, restored_project} or {ok: false, error: "..."}
    """
    return await _memory.revert_promotion(promotion_id)


@mcp.tool()
async def resolve_conflicts(project_id: Optional[str] = None) -> dict:
    """
    Manually run the contradiction pass: same-category memories that are
    close-but-not-duplicate get resolved by keeping the NEWER (by timestamp) and
    soft-superseding the older. Runs automatically during clustering; call this to
    force it now. Returns {superseded: N}.
    """
    return await _memory.detect_conflicts(project_id or None)


@mcp.tool()
async def wipe_episodic(session_id: str) -> dict:
    """
    Wipe all episodic memories tagged with this session_id.

    Call at clean session end to remove in-progress context, tried approaches,
    and temporary episodic notes. Declarative and procedural memories persist.

    Returns {wiped: N}
    """
    return await _memory.wipe_session_episodic(session_id)


@mcp.tool()
async def delete_memory(memory_id: str) -> dict:
    """
    Hard delete a specific memory by ID.

    Use when a stored fact is wrong, stale, or was stored by mistake.
    This is permanent — prefer correct_memory when you know the right value.

    Returns {ok: true} or {ok: false, error: "..."}
    """
    return await _memory.delete_memory(memory_id)


@mcp.tool()
async def correct_memory(memory_id: str, new_content: str) -> dict:
    """
    Replace the content of an existing memory in-place.

    Re-embeds the new content and rebuilds semantic lane connections.
    Preferred over delete+store when the memory_id is known — preserves
    cluster membership and avoids duplicate IDs.

    Returns {id, ok: true} or {ok: false, error: "..."}
    """
    if not new_content or not new_content.strip():
        return {"error": "new_content cannot be empty"}

    warning = _secrets_warning(new_content)
    if warning:
        return {"error": warning}

    result = await _memory.correct_memory(memory_id, new_content.strip())
    if result.get("ok"):
        _schedule_cluster()
    return result


@mcp.tool()
async def trigger_clustering() -> dict:
    """
    Manually trigger a full clustering pass immediately.

    Use after storing a batch of memories to reorganize clusters without
    waiting for the debounce delay, or when get_session_summary themes
    feel stale or mis-grouped.

    Returns {status, clusters, memories} or {status, reason} if skipped.
    """
    if not _memory:
        return {"status": "error", "error": "memory not initialized"}
    result = await _memory.run_clustering()
    if result.get("status") == "success":
        _memory.save_stats()
    return result


@mcp.tool()
async def ingest_document(
    propositions: list,
    project_id: Optional[str],
    source_path: Optional[str] = None,
    universe: str = "declarative",
    priority: int = 3,
) -> dict:
    """
    Store a list of pre-extracted propositions from a document.

    Extract propositions from the document BEFORE calling this tool — summarize
    each section or bullet into a self-contained declarative fact.

    Use project_id=None for datasheets and reference docs that should be
    accessible across all projects (API docs, specs, style guides).

    Security: Do NOT pass raw text that contains secrets, keys, or PII.
    Store only structured facts and summaries.

    Returns {stored: N, total: N}
    """
    if not propositions:
        return {"stored": 0, "total": 0}

    for p in propositions:
        warning = _secrets_warning(str(p))
        if warning:
            return {"error": warning}

    result = await _memory.ingest_document(
        propositions=propositions,
        project_id=project_id or None,
        source_path=source_path,
        universe=universe,
        priority=max(1, min(5, priority)),
    )
    if result.get("stored", 0) > 0:
        _schedule_cluster()
    return result


# ── Session-context endpoint (used by SessionStart hook) ──────────────────────

class _SessionContextApp:
    """
    Minimal ASGI handler for GET /api/session-context

    Returns hookSpecificOutput JSON for the Claude Code SessionStart hook.
    Optionally ingests CLAUDE.md on first visit for a project.

    Query params:
      project_id   — sha256[:16] of git root (may be empty)
      project_root — absolute path to git root (for CLAUDE.md bootstrap)
    """

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        project_id = request.query_params.get("project_id") or None
        project_root = request.query_params.get("project_root") or None
        if project_root:
            project_root = urllib.parse.unquote(project_root)

        if _memory is None:
            resp = JSONResponse({"error": "memory not ready"}, status_code=503)
            await resp(scope, receive, send)
            return

        # CLAUDE.md bootstrap — fires once per project
        if project_root and project_id:
            try:
                await _memory.maybe_ingest_claude_md(project_root, project_id)
            except Exception as e:
                print(f"[3am-claude] CLAUDE.md ingest error: {e}")

        summary = await _memory.get_session_summary(project_id)

        lines = [f"## 3am Memory Context — query mcp__3am tools for recall.\n**project_id:** {project_id or 'none'}\n"]
        project_clusters = [s for s in summary if s["scope"] == "project"]
        general_clusters = [s for s in summary if s["scope"] == "general"]

        if project_clusters:
            lines.append("**Project knowledge:**")
            for s in project_clusters[:6]:
                sample = s["sample_memories"][0][:80] if s["sample_memories"] else ""
                lines.append(f"- {s['theme']} ({s['size']}): {sample}")
            lines.append("")

        if general_clusters:
            lines.append("**General knowledge:**")
            for s in general_clusters[:4]:
                sample = s["sample_memories"][0][:60] if s["sample_memories"] else ""
                lines.append(f"- {s['theme']} ({s['size']}): {sample}")
            lines.append("")

        if not project_clusters and not general_clusters:
            lines.append("No memories stored yet for this project.")

        resp = JSONResponse({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(lines),
            }
        })
        await resp(scope, receive, send)


class _PromptContextApp:
    """
    Minimal ASGI handler for POST /api/prompt-context

    Called by the UserPromptSubmit hook on every user prompt.
    Runs query_memory against the prompt and returns the top results
    as additionalContext — gives Claude relevant memories before each turn.

    Body (JSON):
      project_id — sha256[:16] of git root (may be null)
      prompt     — the raw user prompt text
      limit      — max memories to return (default 5)
    """

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)

        if _memory is None:
            resp = JSONResponse({"error": "memory not ready"}, status_code=503)
            await resp(scope, receive, send)
            return

        try:
            body = await request.json()
        except Exception:
            resp = JSONResponse({"error": "invalid JSON"}, status_code=400)
            await resp(scope, receive, send)
            return

        project_id = body.get("project_id") or None
        prompt = body.get("prompt", "").strip()
        limit = min(int(body.get("limit", 5)), 10)

        if not prompt:
            resp = JSONResponse({"additionalContext": ""})
            await resp(scope, receive, send)
            return

        memories = await _memory.query_memory(
            query=prompt,
            project_id=project_id,
            limit=limit,
            max_tokens=800,
            min_score=0.06,
        )

        if not memories:
            resp = JSONResponse({"additionalContext": ""})
            await resp(scope, receive, send)
            return

        import datetime as _dt
        now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
        lines = [f"[3am] Current time: {now_str}", "Relevant memories (with age):"]
        for m in memories:
            age = m.get("age")
            age_tag = f" ({age})" if age else ""
            lines.append(f"- [{m['universe']}{age_tag}] {m['content']}")

        resp = JSONResponse({"additionalContext": "\n".join(lines)})
        await resp(scope, receive, send)


class _SessionStopApp:
    """
    Minimal ASGI handler for POST /api/session-stop

    Called by the SessionEnd hook at session end.
    Triggers a full recluster to incorporate memories stored this session,
    then wipes episodic memories for the ended session.

    Query params:
      session_id — the Claude Code session ID (from hook payload)
    """

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        session_id = request.query_params.get("session_id") or None

        if _memory is None:
            resp = JSONResponse({"error": "memory not ready"}, status_code=503)
            await resp(scope, receive, send)
            return

        if not session_id:
            resp = JSONResponse({"error": "session_id required"}, status_code=400)
            await resp(scope, receive, send)
            return

        # Recluster first — incorporates memories stored this session
        _schedule_cluster()
        print(f"[3am-claude] SessionEnd: clustering scheduled for {session_id}")

        result = await _memory.wipe_session_episodic(session_id)
        wiped = result.get("wiped", 0)
        if wiped > 0:
            print(f"[3am-claude] SessionEnd: wiped {wiped} episodic memories for {session_id}")
        resp = JSONResponse(result)
        await resp(scope, receive, send)


_STATIC_DIR = Path(__file__).parent / "static"


class _UIApp:
    """
    Serves the memory visualizer (GET /ui) and its JSON API (/api/ui/*):

      GET  /ui                         → the visualizer page
      GET  /api/ui/graph               → nodes + links + clusters + stats
      GET  /api/ui/candidates          → pending promotion candidates
      GET  /api/ui/promotions          → promotion audit log
      POST /api/ui/candidates/approve?id=N
      POST /api/ui/candidates/dismiss?id=N
      POST /api/ui/promotions/revert?id=N
    """

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if path == "/ui" or path == "/ui/":
            index = _STATIC_DIR / "index.html"
            if not index.exists():
                resp = JSONResponse({"error": "UI not installed"}, status_code=404)
            else:
                from starlette.responses import HTMLResponse
                resp = HTMLResponse(index.read_text(encoding="utf-8"))
            await resp(scope, receive, send)
            return

        if _memory is None:
            resp = JSONResponse({"error": "memory not ready"}, status_code=503)
            await resp(scope, receive, send)
            return

        request = Request(scope, receive)

        if path == "/api/ui/graph":
            resp = JSONResponse(_memory.export_graph())
        elif path == "/api/ui/candidates":
            resp = JSONResponse({"candidates": await _memory.list_promotion_candidates()})
        elif path == "/api/ui/promotions":
            resp = JSONResponse({"promotions": await _memory.list_promotions()})
        elif path == "/api/ui/candidates/approve" and method == "POST":
            cid = int(request.query_params.get("id", 0))
            resp = JSONResponse(await _memory.approve_promotion(cid))
        elif path == "/api/ui/candidates/dismiss" and method == "POST":
            cid = int(request.query_params.get("id", 0))
            resp = JSONResponse(await _memory.dismiss_promotion(cid))
        elif path == "/api/ui/promotions/revert" and method == "POST":
            pid = int(request.query_params.get("id", 0))
            resp = JSONResponse(await _memory.revert_promotion(pid))
        else:
            resp = JSONResponse({"error": "not found"}, status_code=404)

        await resp(scope, receive, send)


class _CompositeApp:
    """
    Owns the ASGI lifespan (initialises _memory at server startup, not lazily
    per MCP session) and routes /api/* to custom handlers; everything else to MCP.
    """

    def __init__(self, mcp_app):
        self._mcp = mcp_app
        self._session_ctx = _SessionContextApp()
        self._prompt_ctx = _PromptContextApp()
        self._session_stop = _SessionStopApp()
        self._ui = _UIApp()

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
        elif scope["type"] == "http" and path == "/api/session-context":
            await self._session_ctx(scope, receive, send)
        elif scope["type"] == "http" and path == "/api/prompt-context":
            await self._prompt_ctx(scope, receive, send)
        elif scope["type"] == "http" and path == "/api/session-stop":
            await self._session_stop(scope, receive, send)
        elif scope["type"] == "http" and (path == "/ui" or path == "/ui/" or path.startswith("/api/ui/")):
            await self._ui(scope, receive, send)
        elif scope["type"] == "http" and scope.get("path") == "/health":
            ready = _memory is not None
            resp = JSONResponse(
                {
                    "status": "ok" if ready else "starting",
                    "memories": len(_memory.memories) if ready else 0,
                    "clusters": len(_memory.clusters) if ready else 0,
                    "encrypted": bool(ready and _memory.encryptor
                                      and getattr(_memory.encryptor, "config", None)
                                      and _memory.encryptor.config.enabled),
                },
                status_code=200 if ready else 503,
            )
            await resp(scope, receive, send)
        else:
            await self._mcp(scope, receive, send)

    async def _handle_lifespan(self, receive, send):
        """
        Coordinate two ASGI lifespans:
          1. Our own startup (initialise _memory, start bg task)
          2. FastMCP's lifespan (initialises its StreamableHTTP session manager)
        Both must complete before signalling startup.complete to uvicorn.
        """
        global _memory, _config, _bg_task

        msg = await receive()
        assert msg["type"] == "lifespan.startup"

        # Queues bridge uvicorn ↔ our code ↔ FastMCP lifespan
        mcp_in: asyncio.Queue = asyncio.Queue()   # messages sent TO the mcp app
        mcp_out: asyncio.Queue = asyncio.Queue()  # messages received FROM the mcp app

        async def mcp_receive():
            return await mcp_in.get()

        async def mcp_send(m):
            await mcp_out.put(m)

        lifespan_scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        mcp_task = asyncio.create_task(
            self._mcp(lifespan_scope, mcp_receive, mcp_send)
        )

        try:
            # Kick off FastMCP startup
            await mcp_in.put({"type": "lifespan.startup"})
            mcp_reply = await mcp_out.get()
            if mcp_reply.get("type") == "lifespan.startup.failed":
                raise RuntimeError(
                    f"MCP startup failed: {mcp_reply.get('message', '')}"
                )

            # Our startup
            _config = _load_config()
            encryptor = _get_encryptor()
            db_path = Path(
                _config.get("db_path", "~/.local/share/3am-claude/memory.db")
            ).expanduser()
            _memory = MemorySystem(
                db_path=db_path,
                encryptor=encryptor,
                llm_url=_config.get("llm_url"),
                clustering_config=_config,
            )
            _memory.initialize()
            print(
                f"[3am-claude] Ready — "
                f"{len(_memory.memories)} memories, "
                f"{len(_memory.clusters)} clusters"
            )
            _bg_task = asyncio.create_task(_background_loop())
            seeded = await _memory.seed_behavioral_memories()
            if seeded:
                print(f"[3am-claude] Seeded {seeded} behavioral memories.")
            await send({"type": "lifespan.startup.complete"})

        except Exception as e:
            await send({"type": "lifespan.startup.failed", "message": str(e)})
            mcp_task.cancel()
            return

        # Wait for shutdown signal from uvicorn
        msg = await receive()
        assert msg["type"] == "lifespan.shutdown"

        # Shutdown our stuff
        if _debounce_task and not _debounce_task.done():
            _debounce_task.cancel()
            try:
                await _debounce_task
            except asyncio.CancelledError:
                pass
        if _bg_task:
            _bg_task.cancel()
            try:
                await _bg_task
            except asyncio.CancelledError:
                pass
        if _memory:
            # Cluster any unclustered memories before shutdown — no threshold check,
            # so the next session gets a populated get_session_summary immediately.
            n = _memory.unclustered_count()
            if n > 0:
                print(f"[3am-claude] Shutdown: clustering {n} unclustered memories...")
                try:
                    result = await _memory.run_clustering()
                    print(f"[3am-claude] Shutdown clustering result: {result}")
                except Exception as e:
                    print(f"[3am-claude] Shutdown clustering error: {e}")
            _memory.save_stats()

        # Shutdown FastMCP
        await mcp_in.put({"type": "lifespan.shutdown"})
        await mcp_out.get()  # lifespan.shutdown.complete
        await mcp_task

        print("[3am-claude] Shutdown complete.")
        await send({"type": "lifespan.shutdown.complete"})


# ── ASGI app ──────────────────────────────────────────────────────────────────

# Claude Code "type": "http" uses the Streamable HTTP transport (/mcp endpoint).
# FastMCP >= 1.3.0: streamable_http_app()
# FastMCP < 1.3.0 fallback: sse_app() (/sse endpoint; update settings.json type to "sse")
try:
    app = _CompositeApp(mcp.streamable_http_app())
except AttributeError:
    try:
        _sse = mcp.sse_app()
        app = _CompositeApp(_sse)
        print(
            "[3am-claude] WARNING: streamable_http_app() not available. "
            "Falling back to SSE transport. Update .claude/settings.json: "
            '"type": "sse", "url": "http://127.0.0.1:8765/sse"'
        )
    except AttributeError:
        app = _CompositeApp(mcp.get_asgi_app())
        print("[3am-claude] WARNING: Using get_asgi_app() fallback.")
