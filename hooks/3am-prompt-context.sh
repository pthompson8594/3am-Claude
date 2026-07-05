#!/usr/bin/env bash
# 3am-claude UserPromptSubmit hook — per-prompt memory injection
#
# Copy to ~/.claude/hooks/3am-prompt-context.sh (install.sh does this automatically).
#
# Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "UserPromptSubmit": [{
#         "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-prompt-context.sh", "timeout": 8}]
#       }]
#     }
#   }
#
# What this does:
#   - Reads the user's prompt from the hook payload (stdin JSON)
#   - Queries the 3am-claude daemon for the most relevant memories
#   - Injects them as additionalContext before Claude sees the prompt
#   - Replaces the static SessionStart summary with per-prompt targeted recall

CLAUDE_3AM_DIR="REPLACE_WITH_PATH_TO_3AM_CLAUDE"
VENV="${CLAUDE_3AM_DIR}/.venv"
PORT=8765

PAYLOAD=$(cat 2>/dev/null || echo "{}")

PROMPT=$("${VENV}/bin/python" -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('prompt',''))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "${PROMPT}" ]; then
    exit 0
fi

# session_id feeds the server-side shared seen-set: a memory is injected at most
# once per session across BOTH this hook and action-triggered recall.
SESSION_ID=$("${VENV}/bin/python" -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

PROJECT_ID=$(cd "${PROJECT_ROOT}" && \
    "${VENV}/bin/python" -c \
    "import sys; sys.path.insert(0,'${CLAUDE_3AM_DIR}'); from session import get_project_id; print(get_project_id() or '')" \
    2>/dev/null || echo "")

PROMPT_JSON=$("${VENV}/bin/python" -c \
    "import sys, json; print(json.dumps({'project_id': sys.argv[1] or None, 'prompt': sys.argv[2], 'limit': 5, 'session_id': sys.argv[3] or None}))" \
    "${PROJECT_ID}" "${PROMPT}" "${SESSION_ID}" 2>/dev/null || echo "")

if [ -z "${PROMPT_JSON}" ]; then
    exit 0
fi

RESPONSE=$(curl -sf --max-time 5 \
    -X POST \
    -H "Content-Type: application/json" \
    -d "${PROMPT_JSON}" \
    "http://127.0.0.1:${PORT}/api/prompt-context" \
    2>/dev/null || echo "")

if [ -n "${RESPONSE}" ]; then
    CONTEXT=$("${VENV}/bin/python" -c \
        "import sys, json; d=json.loads(sys.argv[1]); print(d.get('additionalContext',''))" \
        "${RESPONSE}" 2>/dev/null || echo "")
    if [ -n "${CONTEXT}" ]; then
        "${VENV}/bin/python" -c \
            "import sys, json; print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': sys.argv[1]}}))" \
            "${CONTEXT}" 2>/dev/null
    fi
fi
