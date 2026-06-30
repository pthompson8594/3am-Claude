#!/usr/bin/env bash
# install.sh — set up 3am-claude on a new machine
#
# What this does:
#   1. Creates a virtualenv and installs dependencies (including threeam-core)
#   2. Creates ~/.local/share/3am-claude/
#   3. Writes a systemd user service (if systemd is available)
#   4. Writes the SessionStart hook to ~/.claude/hooks/
#   5. Writes the UserPromptSubmit hook to ~/.claude/hooks/
#   6. Writes the Stop hook to ~/.claude/hooks/
#   7. Writes the PreCompact hook to ~/.claude/hooks/
#   8. Writes the SessionEnd hook to ~/.claude/hooks/
#   9. Prints next steps

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${HOME}/.local/share/3am-claude"
HOOKS_DIR="${HOME}/.claude/hooks"
SERVICE_DIR="${HOME}/.config/systemd/user"
VENV="${SCRIPT_DIR}/.venv"
PORT=8765

echo "==> 3am-claude installer"
echo "    Source: ${SCRIPT_DIR}"
echo "    Data:   ${DATA_DIR}"
echo ""

# ── 1. Venv + deps ────────────────────────────────────────────────────────────
if [ ! -d "${VENV}" ]; then
    echo "==> Creating virtualenv..."
    python3 -m venv "${VENV}"
fi

echo "==> Installing dependencies..."
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"

# threeam-core: shared memory/clustering library
# Looks for 3am-AI as a sibling directory; falls back to prompting.
THREEAM_AI_DIR="${SCRIPT_DIR}/../3am-AI"
if [ -f "${THREEAM_AI_DIR}/pyproject.toml" ]; then
    echo "==> Installing threeam-core from ${THREEAM_AI_DIR}..."
    "${VENV}/bin/pip" install --quiet -e "${THREEAM_AI_DIR}"
else
    echo ""
    echo "  threeam-core not found at ${THREEAM_AI_DIR}."
    echo "  Enter the path to your 3am-AI directory (or press Enter to skip):"
    read -r THREEAM_PATH
    if [ -n "${THREEAM_PATH}" ] && [ -f "${THREEAM_PATH}/pyproject.toml" ]; then
        "${VENV}/bin/pip" install --quiet -e "${THREEAM_PATH}"
        echo "==> threeam-core installed from ${THREEAM_PATH}"
    else
        echo "  Skipped. Install manually: pip install -e /path/to/3am-AI"
    fi
fi
echo "    Done."

# ── 2. Data directory ─────────────────────────────────────────────────────────
mkdir -p "${DATA_DIR}"
echo "==> Data directory: ${DATA_DIR}"

# ── 3. Systemd user service ───────────────────────────────────────────────────
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    mkdir -p "${SERVICE_DIR}"
    SERVICE_FILE="${SERVICE_DIR}/3am-claude.service"
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=3am-claude MCP Memory Server
After=network.target

[Service]
Type=simple
ExecStart=${VENV}/bin/uvicorn mcp_server:app --host 127.0.0.1 --port ${PORT}
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable 3am-claude.service
    echo "==> systemd service installed and enabled."
    echo "    Start now:  systemctl --user start 3am-claude"
    echo "    View logs:  journalctl --user -u 3am-claude -f"
else
    echo "==> systemd not available — start the server manually:"
    echo "    ${VENV}/bin/uvicorn mcp_server:app --host 127.0.0.1 --port ${PORT}"
    echo "    WorkingDirectory: ${SCRIPT_DIR}"
fi

# ── 4. SessionStart hook ──────────────────────────────────────────────────────
mkdir -p "${HOOKS_DIR}"
HOOK_FILE="${HOOKS_DIR}/3am-session-start.sh"
cat > "${HOOK_FILE}" <<EOF
#!/usr/bin/env bash
# 3am-claude SessionStart hook
# Bootstraps CLAUDE.md and injects session-level memory context.
# Installed by: ${SCRIPT_DIR}/install.sh

PROJECT_ROOT=\$(git rev-parse --show-toplevel 2>/dev/null || pwd)

PROJECT_ID=\$(cd "\${PROJECT_ROOT}" && \\
    "${VENV}/bin/python" -c \\
    "import sys; sys.path.insert(0,'${SCRIPT_DIR}'); from session import get_project_id; print(get_project_id() or '')" \\
    2>/dev/null || echo "")

ENCODED_ROOT=\$("${VENV}/bin/python" -c \\
    "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" \\
    "\${PROJECT_ROOT}" 2>/dev/null || echo "")

RESPONSE=\$(curl -sf --max-time 5 \\
    "http://127.0.0.1:${PORT}/api/session-context?project_id=\${PROJECT_ID}&project_root=\${ENCODED_ROOT}" \\
    2>/dev/null || echo "")

if [ -n "\${RESPONSE}" ]; then
    echo "\${RESPONSE}"
fi
EOF
chmod +x "${HOOK_FILE}"
echo "==> SessionStart hook installed: ${HOOK_FILE}"

# ── 5. UserPromptSubmit hook ──────────────────────────────────────────────────
PROMPT_FILE="${HOOKS_DIR}/3am-prompt-context.sh"
cat > "${PROMPT_FILE}" <<EOF
#!/usr/bin/env bash
# 3am-claude UserPromptSubmit hook
# Queries memory against each prompt and injects the top 4-5 relevant memories.
# Installed by: ${SCRIPT_DIR}/install.sh

PAYLOAD=\$(cat 2>/dev/null || echo "{}")

PROMPT=\$("${VENV}/bin/python" -c \\
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('prompt',''))" \\
    "\${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "\${PROMPT}" ]; then
    exit 0
fi

PROJECT_ROOT=\$(git rev-parse --show-toplevel 2>/dev/null || pwd)

PROJECT_ID=\$(cd "\${PROJECT_ROOT}" && \\
    "${VENV}/bin/python" -c \\
    "import sys; sys.path.insert(0,'${SCRIPT_DIR}'); from session import get_project_id; print(get_project_id() or '')" \\
    2>/dev/null || echo "")

PROMPT_JSON=\$("${VENV}/bin/python" -c \\
    "import sys, json; print(json.dumps({'project_id': sys.argv[1] or None, 'prompt': sys.argv[2], 'limit': 5}))" \\
    "\${PROJECT_ID}" "\${PROMPT}" 2>/dev/null || echo "")

if [ -z "\${PROMPT_JSON}" ]; then
    exit 0
fi

RESPONSE=\$(curl -sf --max-time 5 \\
    -X POST \\
    -H "Content-Type: application/json" \\
    -d "\${PROMPT_JSON}" \\
    "http://127.0.0.1:${PORT}/api/prompt-context" \\
    2>/dev/null || echo "")

if [ -n "\${RESPONSE}" ]; then
    CONTEXT=\$("${VENV}/bin/python" -c \\
        "import sys, json; d=json.loads(sys.argv[1]); print(d.get('additionalContext',''))" \\
        "\${RESPONSE}" 2>/dev/null || echo "")
    if [ -n "\${CONTEXT}" ]; then
        "${VENV}/bin/python" -c \\
            "import sys, json; print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': sys.argv[1]}}))" \\
            "\${CONTEXT}" 2>/dev/null
    fi
fi
EOF
chmod +x "${PROMPT_FILE}"
echo "==> UserPromptSubmit hook installed: ${PROMPT_FILE}"

# ── 6. Stop hook ──────────────────────────────────────────────────────────────
STOP_EXTRACT_FILE="${HOOKS_DIR}/3am-stop.sh"
cat > "${STOP_EXTRACT_FILE}" <<'STOPEOF'
#!/usr/bin/env bash
# 3am-claude Stop hook -- importance-aware memory extraction
#
# Copy to ~/.claude/hooks/3am-stop.sh (install.sh does this automatically).
#
# Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "Stop": [{
#         "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-stop.sh", "timeout": 10}]
#       }]
#     }
#   }
#
# Why importance-aware (not just every-Nth-turn):
#
#   The point of memory is to LEARN -- so a future session avoids mistakes made
#   here. The single highest-value moment to capture is a CORRECTION (the user
#   said something was wrong) or a substantive work turn (edits/commands). A blind
#   turn counter is most likely to miss exactly those. So:
#
#     • correction detected in the last user turn → ALWAYS block, ask for the
#       LESSON (what went wrong, the right approach, why) as a procedural memory.
#     • the turn did real work (tool use)          → block every 2nd such turn.
#     • chitchat / no work                          → block rarely (every 5th).
#
#   Pass 2 (stop_hook_active: true) always passes through. PreCompact remains the
#   safety net before any context is lost.
#
# Tunables: THREEAM_STOP_EVERY_WORK (default 2), THREEAM_STOP_EVERY_IDLE (5).

PAYLOAD=$(cat 2>/dev/null || echo "{}")

read -r SESSION_ID HOOK_ACTIVE TRANSCRIPT < <(python3 -c '
import sys, json
d = json.loads(sys.argv[1])
print(d.get("session_id",""), "1" if d.get("stop_hook_active") else "0",
      d.get("transcript_path",""))
' "${PAYLOAD}" 2>/dev/null || echo "  ")

if [ -z "${SESSION_ID}" ]; then
    exit 0
fi

# Pass 2: the stop that follows an extraction block -- always allow.
if [ "${HOOK_ACTIVE}" = "1" ]; then
    exit 0
fi

# Inspect the transcript: was the last user turn a correction? did this turn work?
# Emits two flags: "<correction 0|1> <did_work 0|1>". Falls back to "0 1" (treat
# as a work turn) if the transcript can't be parsed.
read -r CORRECTION DID_WORK < <(python3 - "${TRANSCRIPT}" <<'PY' 2>/dev/null || echo "0 1"
import sys, json, re
path = sys.argv[1] if len(sys.argv) > 1 else ""
msgs = []
try:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try: msgs.append(json.loads(line))
                except Exception: pass
except Exception:
    print("0 1"); sys.exit(0)

def role_of(m): return (m.get("message") or m).get("role") or m.get("type") or ""
def content_of(m): return (m.get("message") or m).get("content")

last_text, human_idx = "", -1
for i, m in enumerate(msgs):
    if role_of(m) != "user":
        continue
    c = content_of(m)
    if isinstance(c, str) and c.strip():
        last_text, human_idx = c, i
    elif isinstance(c, list):
        t = [b.get("text","") for b in c
             if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if t:
            last_text, human_idx = " ".join(t), i

did_work = False
for m in msgs[human_idx+1:]:
    if role_of(m) == "assistant":
        c = content_of(m)
        if isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in c):
            did_work = True
            break

CORR = re.compile(
    r"\b(no|nope|actually|wrong|incorrect|that'?s not|that is not|don'?t|stop|"
    r"undo|revert|you (missed|forgot|broke|misunderstood)|not what|isn'?t right|"
    r"shouldn'?t|instead of)\b", re.IGNORECASE)
# Only treat as a real correction when the message is SHORT and FEW-LINE. Long or
# multi-line turns (pasted docs/output, or a slash-command skill's injected prompt
# like /3am-consolidate) contain correction vocabulary without being corrections --
# the keyword scan over those was the source of false 'you were corrected' fires.
is_brief = last_text and len(last_text) < 240 and last_text.count("\n") < 3
correction = 1 if (is_brief and CORR.search(last_text)) else 0
print(f"{correction} {1 if did_work else 0}")
PY
)

CORRECTION="${CORRECTION:-0}"
DID_WORK="${DID_WORK:-1}"

COUNT_FILE="/tmp/3am-stop-${SESSION_ID}.count"

# Correction → capture the lesson now, no throttle.
if [ "${CORRECTION}" = "1" ]; then
    echo "0" > "${COUNT_FILE}"
    python3 -c "
import json
msg = ('[3am] You were corrected this turn. Store the LESSON so a future session '
       'avoids it: call store_memory (universe=procedural, priority 4-5) phrased as '
       'a rule -- \"When <situation>, do <right thing>, NOT <wrong thing>, because '
       '<reason>\". Make it general (project_id=None) if it applies beyond this repo. '
       'If a previously stored fact was wrong, use apply_correction/supersede_memory. '
       'Then stop.')
print(json.dumps({'decision': 'block', 'reason': msg}))
"
    exit 0
fi

# Otherwise throttle by turn type: work turns captured sooner than idle ones.
if [ "${DID_WORK}" = "1" ]; then
    EVERY="${THREEAM_STOP_EVERY_WORK:-2}"
else
    EVERY="${THREEAM_STOP_EVERY_IDLE:-5}"
fi

COUNT=$(cat "${COUNT_FILE}" 2>/dev/null || echo "0")
case "${COUNT}" in (*[!0-9]*|"") COUNT=0 ;; esac
COUNT=$((COUNT + 1))

if [ "${COUNT}" -lt "${EVERY}" ]; then
    echo "${COUNT}" > "${COUNT_FILE}"
    exit 0
fi

echo "0" > "${COUNT_FILE}"

python3 -c "
import json
msg = ('[3am] Silently store anything from the last few turns worth knowing in a '
       'future session -- prioritise LESSONS (a failed approach, a gotcha, what '
       'worked) as actionable procedural memories, plus any non-obvious facts '
       '(declarative). Ask: which one sentence would stop a fresh Claude from '
       'repeating a mistake made here? Store it via store_memory only. Do NOT write '
       'any user-facing message about this step -- no preamble, no "nothing to store" '
       'narration. If nothing qualifies, end the turn with no text at all.')
print(json.dumps({'decision': 'block', 'reason': msg}))
"
STOPEOF
chmod +x "${STOP_EXTRACT_FILE}"
echo "==> Stop hook installed: ${STOP_EXTRACT_FILE}"

# \u2500\u2500 7. PreCompact hook \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
PRECOMPACT_FILE="${HOOKS_DIR}/3am-precompact.sh"
cat > "${PRECOMPACT_FILE}" <<'PCEOF'
#!/usr/bin/env bash
# 3am-claude PreCompact hook \u2014 flush memory before context is compacted
# Installed by: install.sh
# AUTO compaction: blocks once (PreCompact supports decision:block) so Claude can
# checkpoint in-flight state, then allows it on the retry.
# MANUAL /compact: passes straight through (it does not auto-retry, so blocking
# would force the user to run /compact twice).

PAYLOAD=$(cat 2>/dev/null || echo "{}")

read -r SESSION_ID TRIGGER < <(python3 -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''), d.get('trigger','auto'))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "${SESSION_ID}" ]; then
    exit 0
fi

# Manual /compact: the user asked for it \u2014 don't block, don't double-prompt.
if [ "${TRIGGER}" = "manual" ]; then
    exit 0
fi

FLAG="/tmp/3am-precompact-${SESSION_ID}"

if [ -f "${FLAG}" ]; then
    rm -f "${FLAG}"
    exit 0
fi

touch "${FLAG}"

python3 -c "
import json
msg = '[3am] Context is about to be compacted \u2014 detail from this session is about to be compressed away. Before it is, call store_memory for anything not yet saved: in-flight task state (universe=episodic), plus any decisions, bugs found, or conventions discovered (declarative/procedural). Once saved, allow the compaction.'
print(json.dumps({'decision': 'block', 'reason': msg}))
"
PCEOF
chmod +x "${PRECOMPACT_FILE}"
echo "==> PreCompact hook installed: ${PRECOMPACT_FILE}"

# ── 9. SessionEnd hook ────────────────────────────────────────────────────────
STOP_FILE="${HOOKS_DIR}/3am-session-stop.sh"
cat > "${STOP_FILE}" <<EOF
#!/usr/bin/env bash
# 3am-claude SessionEnd hook
# Triggers recluster + wipes episodic memories at session end.
# Installed by: ${SCRIPT_DIR}/install.sh

PAYLOAD=\$(cat 2>/dev/null || echo "{}")

SESSION_ID=\$("${VENV}/bin/python" -c \\
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''))" \\
    "\${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "\${SESSION_ID}" ]; then
    exit 0
fi

# Clean up per-session scratch files left by the Stop / PreCompact hooks.
rm -f "/tmp/3am-stop-\${SESSION_ID}" "/tmp/3am-stop-\${SESSION_ID}.count" \\
      "/tmp/3am-precompact-\${SESSION_ID}" 2>/dev/null || true

curl -sf --max-time 5 \\
    -X POST \\
    "http://127.0.0.1:${PORT}/api/session-stop?session_id=\${SESSION_ID}" \\
    >/dev/null 2>&1 || true
EOF
chmod +x "${STOP_FILE}"
echo "==> SessionEnd hook installed: ${STOP_FILE}"

# ── 9b. Skills ────────────────────────────────────────────────────────────────
# Install bundled Claude Code skills (user scope, apply across all projects).
if [ -d "${SCRIPT_DIR}/skills" ]; then
    SKILLS_DIR="${HOME}/.claude/skills"
    mkdir -p "${SKILLS_DIR}"
    for skill in "${SCRIPT_DIR}/skills"/*/; do
        [ -d "$skill" ] || continue
        name="$(basename "$skill")"
        mkdir -p "${SKILLS_DIR}/${name}"
        cp -r "${skill}." "${SKILLS_DIR}/${name}/"
        echo "==> Skill installed: ${SKILLS_DIR}/${name}"
    done
fi

# ── 10. Next steps ────────────────────────────────────────────────────────────
echo ""
echo "==> Next steps:"
echo ""
echo "  1. Register the MCP server (NOT in settings.json — MCP lives in ~/.claude.json):"
echo "       claude mcp add --transport http --scope user 3am http://127.0.0.1:${PORT}/mcp"
echo ""
echo "     Or, with no 'claude' CLI on PATH, add to ~/.claude.json under top-level"
echo "     \"mcpServers\": { \"3am\": { \"type\": \"http\", \"url\": \"http://127.0.0.1:${PORT}/mcp\" } }"
echo ""
echo "  2. Register the hooks in ~/.claude/settings.json (hooks ARE read from here):"
echo '     {'
echo '       "hooks": {'
echo '         "SessionStart": [{'
echo '           "hooks": [{"type": "command", "command": "'"${HOOK_FILE}"'", "timeout": 10}]'
echo '         }],'
echo '         "UserPromptSubmit": [{'
echo '           "hooks": [{"type": "command", "command": "'"${PROMPT_FILE}"'", "timeout": 8}]'
echo '         }],'
echo '         "Stop": [{'
echo '           "hooks": [{"type": "command", "command": "'"${STOP_EXTRACT_FILE}"'", "timeout": 10}]'
echo '         }],'
echo '         "PreCompact": [{'
echo '           "hooks": [{"type": "command", "command": "'"${PRECOMPACT_FILE}"'", "timeout": 10}]'
echo '         }],'
echo '         "SessionEnd": [{'
echo '           "hooks": [{"type": "command", "command": "'"${STOP_FILE}"'", "timeout": 15}]'
echo '         }]'
echo '       }'
echo '     }'
echo ""
echo "  3. Start the server:"
if command -v systemctl &>/dev/null && systemctl --user status &>/dev/null 2>&1; then
    echo "     systemctl --user start 3am-claude"
else
    echo "     ${VENV}/bin/uvicorn mcp_server:app --host 127.0.0.1 --port ${PORT} &"
fi
echo ""
echo "  4. Open the memory map (optional):"
echo "     http://127.0.0.1:${PORT}/ui"
echo ""
echo "==> Install complete."
