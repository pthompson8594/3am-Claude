#!/usr/bin/env bash
# 3am-claude tool-recall hook — action-triggered memory recall.
#
# Register for BOTH PreToolUse and PostToolUse in ~/.claude/settings.json:
#   "PreToolUse":  [{"matcher": "Edit|Write|MultiEdit",
#                    "hooks": [{"type":"command","command":"~/.claude/hooks/3am-recall.sh","timeout":8}]}],
#   "PostToolUse": [{"matcher": "Read|Edit|Write|MultiEdit|Grep|Glob|Bash",
#                    "hooks": [{"type":"command","command":"~/.claude/hooks/3am-recall.sh","timeout":8}]}]
#
# PreToolUse(Edit/Write) = "before you touch X, here's what you know about X".
# PostToolUse(others)     = general recall as you read/search/run.
# The Python helper reads the payload on stdin and emits hookSpecificOutput
# additionalContext only when a not-yet-seen, clearly-relevant memory exists.

# Path to 3am-claude source (edit after copying, or let install.sh fill it in).
SCRIPT_DIR="REPLACE_WITH_PATH_TO_3AM_CLAUDE"
VENV="${SCRIPT_DIR}/.venv"
PORT=8765

exec "${VENV}/bin/python" "${SCRIPT_DIR}/hooks/recall_hook.py" "${PORT}" "${SCRIPT_DIR}"
