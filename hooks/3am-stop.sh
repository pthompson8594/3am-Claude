#!/usr/bin/env bash
# 3am-claude Stop hook — per-turn memory extraction
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
# What this does (two-pass loop):
#
#   Pass 1 (stop_hook_active: false):
#     — Creates a flag file at /tmp/3am-stop-{session_id}
#     — Blocks Claude from stopping with a memory extraction prompt
#     — Claude calls store_memory for anything worth keeping, then stops
#
#   Pass 2 (stop_hook_active: true):
#     — Flag file exists — passes through, allows the stop
#     — Cleans up the flag file
#
# The flag file prevents infinite loops: Claude can only be blocked once per turn.

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
    # Pass 2: Claude has done extraction — allow stop, clean up flag
    rm -f "${FLAG}"
    exit 0
fi

if [ -f "${FLAG}" ]; then
    # Flag exists but hook isn't active — stale flag, clean up and pass through
    rm -f "${FLAG}"
    exit 0
fi

# Pass 1: first stop this turn — block and ask Claude to extract memories
touch "${FLAG}"

python3 -c "
import json, sys
msg = '[3am] Without replying, call store_memory for anything from this turn worth knowing in a future session \u2014 what was looked up, decided, or discovered. Then stop.'
print(json.dumps({'decision': 'block', 'reason': msg}))
"
