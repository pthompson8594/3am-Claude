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
#   7. Writes the StopSession hook to ~/.claude/hooks/
#   8. Prints next steps

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
# 3am-claude Stop hook — per-turn memory extraction (two-pass)
# Installed by: install.sh

PAYLOAD=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$(python3 -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

HOOK_ACTIVE=$(python3 -c \
    "import sys, json; d=json.loads(sys.argv[1]); print('1' if d.get('stop_hook_active') else '0')" \
    "${PAYLOAD}" 2>/dev/null || echo "0")

if [ -z "${SESSION_ID}" ]; then
    exit 0
fi

FLAG="/tmp/3am-stop-${SESSION_ID}"

if [ "${HOOK_ACTIVE}" = "1" ]; then
    rm -f "${FLAG}"
    exit 0
fi

if [ -f "${FLAG}" ]; then
    rm -f "${FLAG}"
    exit 0
fi

touch "${FLAG}"

python3 -c "
import json
msg = '[3am] Without replying, call store_memory for anything from this turn worth knowing in a future session \u2014 what was looked up, decided, or discovered. Then stop.'
print(json.dumps({'decision': 'block', 'reason': msg}))
"
STOPEOF
chmod +x "${STOP_EXTRACT_FILE}"
echo "==> Stop hook installed: ${STOP_EXTRACT_FILE}"

# ── 7. StopSession hook ───────────────────────────────────────────────────────
STOP_FILE="${HOOKS_DIR}/3am-session-stop.sh"
cat > "${STOP_FILE}" <<EOF
#!/usr/bin/env bash
# 3am-claude StopSession hook
# Triggers recluster + wipes episodic memories at session end.
# Installed by: ${SCRIPT_DIR}/install.sh

PAYLOAD=\$(cat 2>/dev/null || echo "{}")

SESSION_ID=\$("${VENV}/bin/python" -c \\
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''))" \\
    "\${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "\${SESSION_ID}" ]; then
    exit 0
fi

curl -sf --max-time 5 \\
    -X POST \\
    "http://127.0.0.1:${PORT}/api/session-stop?session_id=\${SESSION_ID}" \\
    >/dev/null 2>&1 || true
EOF
chmod +x "${STOP_FILE}"
echo "==> StopSession hook installed: ${STOP_FILE}"

# ── 8. Next steps ─────────────────────────────────────────────────────────────
echo ""
echo "==> Next steps:"
echo ""
echo "  1. Register the MCP server in ~/.claude/settings.json:"
echo '     {'
echo '       "mcpServers": {'
echo '         "3am": {'
echo '           "type": "http",'
echo "           \"url\": \"http://127.0.0.1:${PORT}/mcp\""
echo '         }'
echo '       }'
echo '     }'
echo ""
echo "  2. Register the hooks in ~/.claude/settings.json:"
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
echo '         "StopSession": [{'
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
echo "==> Install complete."
