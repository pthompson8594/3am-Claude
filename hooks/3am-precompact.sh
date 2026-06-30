#!/usr/bin/env bash
# 3am-claude PreCompact hook — flush memory before context is compacted
#
# Copy to ~/.claude/hooks/3am-precompact.sh (install.sh does this automatically).
#
# Register in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "PreCompact": [{
#         "hooks": [{"type": "command", "command": "~/.claude/hooks/3am-precompact.sh", "timeout": 10}]
#       }]
#     }
#   }
#
# Why this exists:
#   Compaction is the one moment context is actually destroyed. Episodic state
#   ("what I was mid-doing", "what I just tried") is exactly what compression
#   loses. This hook is the last checkpoint before that happens.
#
# PreCompact supports `decision: block` (with a reason) but NOT additionalContext,
# so for AUTO compaction we mirror the Stop hook's two-pass pattern:
#
#   Pass 1 (no flag): create /tmp/3am-precompact-{session_id}, block compaction
#     with an extraction prompt. Claude stores in-flight state, then compaction
#     is retried (auto-compaction re-fires while context is still over budget).
#
#   Pass 2 (flag exists): remove the flag and allow compaction to proceed.
#
# The flag guarantees we block compaction at most once per event — auto-compaction
# can never be deadlocked into a loop, so context can always drain.
#
# MANUAL compaction (the user typed /compact) is NOT blocked: /compact does not
# auto-retry, so blocking it would force the user to run /compact twice every
# time. They asked for it deliberately — respect it and pass straight through.

PAYLOAD=$(cat 2>/dev/null || echo "{}")

read -r SESSION_ID TRIGGER < <(python3 -c \
    "import sys, json; d=json.loads(sys.argv[1]); print(d.get('session_id',''), d.get('trigger','auto'))" \
    "${PAYLOAD}" 2>/dev/null || echo "")

if [ -z "${SESSION_ID}" ]; then
    exit 0
fi

# Manual /compact: the user asked for it — don't block, don't double-prompt.
if [ "${TRIGGER}" = "manual" ]; then
    exit 0
fi

FLAG="/tmp/3am-precompact-${SESSION_ID}"

if [ -f "${FLAG}" ]; then
    # Pass 2: already flushed for this compaction — allow it through.
    rm -f "${FLAG}"
    exit 0
fi

# Pass 1: block once and ask Claude to checkpoint memory before compression.
touch "${FLAG}"

python3 -c "
import json
msg = ('[3am] Context is about to be compacted — detail from this session is about '
       'to be compressed away. Before it is, call store_memory for anything not yet '
       'saved: in-flight task state (universe=episodic), plus any decisions, bugs '
       'found, or conventions discovered (declarative/procedural). This is the last '
       'checkpoint before compression. Once saved, allow the compaction to proceed.')
print(json.dumps({'decision': 'block', 'reason': msg}))
"
