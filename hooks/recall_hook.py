#!/usr/bin/env python3
"""
3am-claude action-triggered recall hook (logic for both PreToolUse and PostToolUse).

Invoked by hooks/3am-recall.sh as:  recall_hook.py <port> <script_dir>
Reads the Claude Code hook payload (JSON) on stdin.

Recall today is input-triggered (SessionStart + UserPromptSubmit match the USER's
message). This hook adds ACTION-triggered recall: as Claude touches files, searches,
and runs commands, it queries 3am on that activity and injects relevant memories the
prompt never matched — so a stored lesson surfaces exactly when Claude reaches for the
thing it's about.

Noise control (this fires on every matched tool call, so it must stay quiet):
  - relevance floor on the server side (min_score / min_cosine gate),
  - the SERVER-SIDE per-session seen-set, shared with prompt-context injection,
    so a memory is injected at most once per session across ALL surfaces (we
    pass session_id; the server dedups and marks only the hits it returns),
  - at most 1 hit on PostToolUse, 2 on the PreToolUse pre-edit guard.
On anything not clearly relevant it emits nothing.
"""
import json
import os
import sys
import urllib.request

def derive_query(tool: str, tin: dict) -> str:
    """Turn the tool call into a topic signal for retrieval. For edits the CODE
    being changed (old_string/content) is the real signal — a bare filename just
    matches generic project memories, so it's only a fallback."""
    if tool == "Edit":
        return f"{os.path.basename(tin.get('file_path',''))} {(tin.get('old_string','') or '')[:300]}".strip()
    if tool == "MultiEdit":
        edits = tin.get("edits") or []
        first = (edits[0].get("old_string", "") if edits else "")[:300]
        return f"{os.path.basename(tin.get('file_path',''))} {first}".strip()
    if tool == "Write":
        return f"{os.path.basename(tin.get('file_path',''))} {(tin.get('content','') or '')[:300]}".strip()
    if tool in ("Read", "NotebookEdit"):
        fp = tin.get("file_path") or tin.get("notebook_path") or ""
        if not fp:
            return ""
        base = os.path.basename(fp)
        parent = os.path.basename(os.path.dirname(fp))
        return f"{base} {parent} {fp}"
    if tool == "Grep":
        return f"{tin.get('pattern','')} {tin.get('path','')}".strip()
    if tool == "Glob":
        return tin.get("pattern", "")
    if tool == "Bash":
        return (tin.get("command", "") or "")[:160]
    return ""


def last_assistant_snippet(transcript_path: str, limit: int = 200) -> str:
    """A short slice of Claude's most recent reasoning text — enriches the signal
    so recall tracks what Claude is thinking about, not only the raw tool input."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        text = ""
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                msg = m.get("message") or m
                if (msg.get("role") or m.get("type")) != "assistant":
                    continue
                c = msg.get("content")
                if isinstance(c, list):
                    parts = [b.get("text", "") for b in c
                             if isinstance(b, dict) and b.get("type") == "text"]
                    if parts:
                        text = " ".join(parts)
        return text[-limit:].strip()
    except Exception:
        return ""


def recall(port: int, project_id, query: str, session_id: str, limit: int) -> list:
    body = json.dumps({
        "project_id": project_id, "query": query,
        "session_id": session_id or None, "limit": limit,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/recall", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read()).get("hits", [])
    except Exception:
        return []


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    script_dir = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()

    raw = sys.stdin.read() or "{}"
    try:
        d = json.loads(raw)
    except Exception:
        return 0

    event = d.get("hook_event_name", "")
    tool = d.get("tool_name", "")
    tin = d.get("tool_input") or {}
    session_id = d.get("session_id", "")
    cwd = d.get("cwd") or os.getcwd()
    transcript = d.get("transcript_path", "")

    sig = derive_query(tool, tin)
    if len(sig.strip()) < 3:
        return 0
    query = (sig + " " + last_assistant_snippet(transcript)).strip()[:400]

    sys.path.insert(0, script_dir)
    project_id = None
    try:
        from session import get_project_id
        project_id = get_project_id(cwd) or None
    except Exception:
        pass

    # Dedup lives SERVER-SIDE (shared seen-set across prompt-context and action
    # recall): ask for exactly what we'll show — the server filters already-seen
    # memories and marks only the hits it returns.
    n = 2 if event == "PreToolUse" else 1
    chosen = recall(port, project_id, query, session_id, n)
    if not chosen:
        return 0

    lines = ["[3am] Possibly relevant to what you're doing:"]
    for h in chosen:
        meta = [h.get("universe", "")]
        if h.get("origin"):
            meta.append(h["origin"])
        if h.get("age"):
            meta.append(h["age"])
        lines.append(f"- [{', '.join(m for m in meta if m)}] {h.get('content','')}")
    out = {"hookSpecificOutput": {
        "hookEventName": event or "PostToolUse",
        "additionalContext": "\n".join(lines),
    }}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
