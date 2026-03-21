#!/usr/bin/env bash
# 3am-claude StopSession hook — template
#
# Copy this to ~/.claude/hooks/3am-session-stop.sh (or run install.sh which
# does this automatically with the correct paths filled in).
#
# Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "StopSession": [{
#         "matcher": "",
#         "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-stop.sh", "timeout": 10}]
#       }]
#     }
#   }
#
# What this does:
#   - Reads the session_id from the Claude Code hook payload (stdin JSON)
#   - POSTs to /api/session-stop on the running daemon
#   - The server calls wipe_episodic(session_id) to clear transient work context

# Path to 3am-claude source (edit after copying)
CLAUDE_3AM_DIR="REPLACE_WITH_PATH_TO_3AM_CLAUDE"
VENV="${CLAUDE_3AM_DIR}/.venv"
PORT=8765

# Hook payload arrives on stdin as JSON
PAYLOAD=$(cat 2>/dev/null || echo "{}")

SESSION_ID=$("${VENV}/bin/python" -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "${SESSION_ID}" ]; then
    exit 0
fi

curl -sf --max-time 5 \
    -X POST \
    "http://127.0.0.1:${PORT}/api/session-stop?session_id=${SESSION_ID}" \
    >/dev/null 2>&1 || true
