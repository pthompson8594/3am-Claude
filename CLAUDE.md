# 3am-claude — Claude Code Instructions

This project is the 3am-claude MCP memory server. These rules govern how to use the memory system correctly.

## Session start

The SessionStart hook bootstraps CLAUDE.md and injects cluster-level orientation. The UserPromptSubmit hook injects relevant memories before every prompt — you should already have context when you need it. If something seems missing, call `query_memory` directly.

## Storing memories

Store facts that are non-obvious, architectural, or would take effort to re-derive. Don't store things that are immediately readable from the code.

- `declarative` — architecture decisions, module facts, API shapes, bugs found and fixed
- `procedural` — codebase patterns: how tests work, error handling style, conventions
- `episodic` — in-progress work, what was tried this session, current task context

## Skills & approach playbook

When a Claude Code Skill (e.g. `/openscad`, `/code-review`, `/verify`) or a specific non-obvious approach works well — or notably fails — for a recurring kind of task, store it as a `procedural` general memory (`project_id=None`): which skill/approach, for what task type, and the outcome. Before tackling a task that feels familiar, query for these so you reach for the right tool immediately instead of re-deriving it.

## Memory hygiene (required, no reminders)

When you complete work that was previously stored as "planned", "todo", "not yet implemented", or "needed":

1. Query for related planned-feature memories
2. Delete ones that are fully superseded
3. Correct ones that are partially outdated

Do this in the same turn as completing the work. Do not leave stale planned-feature memories in the DB — they pollute session summaries and mislead future recall.

## Auto-promotion to the general pool

When you store a project memory that closely matches knowledge already in another project, the server promotes it to the general pool automatically (high confidence) or queues it as a candidate (borderline). If you notice cross-project knowledge piling up, call `list_promotion_candidates` and `approve_promotion` / `dismiss_promotion`, or review it visually at the web UI (`/ui`). Promotions are logged (`list_promotions`) and reversible (`revert_promotion`).

## Querying

Query before answering questions about the project — don't guess at things that are likely stored. Use `min_score=0.01` to trim low-signal padding when context budget matters.

This includes user-facing questions: if asked about the user's name, preferences, or context, query `mcp__3am__query_memory` first before guessing or saying "I don't know". The answer is likely already stored.

## Session end

The Stop hook is importance-aware: it always blocks after a **correction** (asking you to store the lesson), captures **work turns** every 2nd turn, and idles otherwise. The PreCompact hook blocks once right before context is compacted — treat that prompt as a hard checkpoint to flush in-flight state before it's compressed away. The SessionEnd hook triggers reclustering and wipes episodic memories automatically. No manual `wipe_episodic` call needed.

## The point of memory: learn, don't repeat

The goal of 3am memory is to make *this* Claude outperform a fresh, memoryless Claude on any task where the fresh one would repeat a mistake. That only happens if you **capture lessons** (corrections, failed approaches, gotchas) as actionable rules and **recall them before acting**. The highest-value memory is a correction phrased as a rule: *"When X, do Y, NOT Z, because …"*. At a task boundary — work finished, bug fixed, correction received — run the `/3am-consolidate` skill (or do it inline): extract the lessons, store them at the right scope, supersede anything now wrong, and prune completed "planned" memories. Litmus test before storing: *which one sentence would stop a fresh Claude from repeating the mistake just made?* Store that.
