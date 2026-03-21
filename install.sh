#!/usr/bin/env bash
# install.sh — set up 3am-claude on a new machine
#
# What this does:
#   1. Creates a virtualenv and installs dependencies
#   2. Creates ~/.local/share/3am-claude/
#   3. Writes a systemd user service (if systemd is available)
#   4. Writes the SessionStart hook to ~/.claude/hooks/
#   5. Writes the PostToolUse memory-nudge hook to ~/.claude/hooks/
#   6. Writes the StopSession hook to ~/.claude/hooks/
#   7. Prints next steps

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
# Injects memory context into Claude Code at session start.
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

# ── 5. PostToolUse memory-nudge hook ──────────────────────────────────────────
NUDGE_FILE="${HOOKS_DIR}/3am-post-tool-use.sh"
cat > "${NUDGE_FILE}" <<'EOF'
#!/usr/bin/env bash
# 3am-claude PostToolUse hook — memory capture nudge
# Fires after Write/Edit ~30% of the time to avoid noise.

# Only nudge ~30% of the time
if [ $(( RANDOM % 10 )) -ge 3 ]; then
    exit 0
fi

printf '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[3am] If this edit reflects an architectural decision or non-obvious pattern, store_memory it."}}'
EOF
chmod +x "${NUDGE_FILE}"
echo "==> PostToolUse hook installed: ${NUDGE_FILE}"

# ── 6. StopSession hook ───────────────────────────────────────────────────────
STOP_FILE="${HOOKS_DIR}/3am-session-stop.sh"
cat > "${STOP_FILE}" <<EOF
#!/usr/bin/env bash
# 3am-claude StopSession hook
# Auto-wipes episodic memories at session end.
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

# ── 7. Next steps ─────────────────────────────────────────────────────────────
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
echo '         "PostToolUse": [{'
echo '           "matcher": "Write|Edit",'
echo '           "hooks": [{"type": "command", "command": "'"${NUDGE_FILE}"'", "timeout": 5}]'
echo '         }],'
echo '         "StopSession": [{'
echo '           "hooks": [{"type": "command", "command": "'"${STOP_FILE}"'", "timeout": 10}]'
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
