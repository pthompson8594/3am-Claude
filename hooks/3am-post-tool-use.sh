#!/usr/bin/env bash
# 3am-claude PostToolUse hook — memory capture nudge
#
# Copy this to ~/.claude/hooks/3am-post-tool-use.sh and make it executable.
#
# Register in ~/.claude/settings.json under hooks.PostToolUse:
#   {
#     "PostToolUse": [{
#       "matcher": "Write|Edit",
#       "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-post-tool-use.sh", "timeout": 5}]
#     }]
#   }
#
# Fires after Write/Edit ~30% of the time to avoid noise.
# Injects a quiet background note — not a question, just a reminder.

# Only nudge ~30% of the time
if [ $(( RANDOM % 10 )) -ge 3 ]; then
    exit 0
fi

printf '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[3am] If this edit reflects an architectural decision or non-obvious pattern, store_memory it."}}'
