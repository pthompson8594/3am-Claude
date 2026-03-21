# 3am-claude — Claude Code Instructions

This project is the 3am-claude MCP memory server. These rules govern how to use the memory system correctly.

## Session start

Call `get_session_summary` at the start of every session to orient. The SessionStart hook does this automatically — if context was injected, you're already oriented. If not, call it manually before doing anything else.

## Storing memories

Store facts that are non-obvious, architectural, or would take effort to re-derive. Don't store things that are immediately readable from the code.

- `declarative` — architecture decisions, module facts, API shapes, bugs found and fixed
- `procedural` — codebase patterns: how tests work, error handling style, conventions
- `episodic` — in-progress work, what was tried this session, current task context

## Memory hygiene (required, no reminders)

When you complete work that was previously stored as "planned", "todo", "not yet implemented", or "needed":

1. Query for related planned-feature memories
2. Delete ones that are fully superseded
3. Correct ones that are partially outdated

Do this in the same turn as completing the work. Do not leave stale planned-feature memories in the DB — they pollute session summaries and mislead future recall.

## Querying

Query before answering questions about the project — don't guess at things that are likely stored. Use `min_score=0.01` to trim low-signal padding when context budget matters.

This includes user-facing questions: if asked about the user's name, preferences, or context, query `mcp__3am__query_memory` first before guessing or saying "I don't know". The answer is likely already stored.

## Session end

Call `wipe_episodic` on clean session end to clear transient work context. Declarative and procedural memories persist.
