#!/usr/bin/env bash
# 3am-claude SessionStart hook — template
#
# Copy this to ~/.claude/hooks/3am-session-start.sh (or run install.sh which
# does this automatically with the correct paths filled in).
#
# Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "SessionStart": [{
#         "matcher": "",
#         "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-session-start.sh", "timeout": 10}]
#       }]
#     }
#   }
#
# What this does:
#   - Detects the current project root (git rev-parse)
#   - Computes the project_id hash via session.py
#   - Calls GET /api/session-context on the running daemon
#   - Outputs hookSpecificOutput JSON so Claude gets memory context injected
#     at session start — zero context cost (doesn't appear as a tool call turn)

# Path to 3am-claude source (edit after copying)
CLAUDE_3AM_DIR="REPLACE_WITH_PATH_TO_3AM_CLAUDE"
VENV="${CLAUDE_3AM_DIR}/.venv"
PORT=8765

PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

PROJECT_ID=$(cd "${PROJECT_ROOT}" && \
    "${VENV}/bin/python" -c \
    "import sys; sys.path.insert(0,'${CLAUDE_3AM_DIR}'); from session import get_project_id; print(get_project_id() or '')" \
    2>/dev/null || echo "")

ENCODED_ROOT=$("${VENV}/bin/python" -c \
    "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" \
    "${PROJECT_ROOT}" 2>/dev/null || echo "")

RESPONSE=$(curl -sf --max-time 5 \
    "http://127.0.0.1:${PORT}/api/session-context?project_id=${PROJECT_ID}&project_root=${ENCODED_ROOT}" \
    2>/dev/null || echo "")

if [ -n "${RESPONSE}" ]; then
    echo "${RESPONSE}"
fi
