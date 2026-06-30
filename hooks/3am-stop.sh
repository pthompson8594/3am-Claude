#!/usr/bin/env bash
# 3am-claude Stop hook — importance-aware memory extraction
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
#   The point of memory is to LEARN — so a future session avoids mistakes made
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

# Pass 2: the stop that follows an extraction block — always allow.
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
# like /3am-consolidate) contain correction vocabulary without being corrections —
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
       'a rule — \"When <situation>, do <right thing>, NOT <wrong thing>, because '
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
       'future session — prioritise LESSONS (a failed approach, a gotcha, what '
       'worked) as actionable procedural memories, plus any non-obvious facts '
       '(declarative). Ask: which one sentence would stop a fresh Claude from '
       'repeating a mistake made here? Store it via store_memory only. Do NOT write '
       'any user-facing message about this step — no preamble, no "nothing to store" '
       'narration. If nothing qualifies, end the turn with no text at all.')
print(json.dumps({'decision': 'block', 'reason': msg}))
"
