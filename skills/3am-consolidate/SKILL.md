---
name: 3am-consolidate
description: Consolidate what was learned in the current work into 3am memory at a task boundary. Use after finishing a unit of work, after being corrected, after a non-obvious bug fix, or when the user says "remember this" / "consolidate" / "save what you learned". The point is to LEARN — so a future session (and a future you) avoids the mistakes made here.
---

# 3am Consolidate — turn this session into durable learning

The goal of 3am memory is not trivia recall. It is to make *this* configured Claude
outperform a fresh, memoryless Claude on tasks where the fresh one would repeat a
mistake. That only happens if mistakes, corrections, and hard-won approaches get
captured here as **actionable lessons** that will *fire before you act* next time.

Run this at a natural boundary — a task finishing, a bug fixed, a correction
received — not mid-edit (your context already holds the in-flight detail).

## What to capture (in priority order)

1. **Corrections & mistakes** — the highest-value memories. Anything the user
   corrected, or any approach that failed and had to be redone. Phrase as a rule:
   *"When <situation>, do <right thing>, NOT <wrong thing>, because <reason>."*
   Store as `procedural`, priority 4–5. Make it general (`project_id=None`) if the
   lesson applies beyond this repo; project-scoped if it's specific to this code.

2. **Approaches that worked** — which tool/skill/strategy solved a recurring kind
   of task (the skill/approach playbook). `procedural`, general, priority 4–5.

3. **Non-obvious facts discovered** — architecture, API shapes, gotchas that took
   effort to derive and aren't readable straight from the code. `declarative`.

4. **Changed facts** — if something previously stored is now different, use
   `supersede_memory` (fact changed) or `apply_correction` (it was wrong), not a
   plain new store. This keeps the old value from firing again.

## Steps

1. **Recall first** — `query_memory` for the task area so you build on existing
   lessons and detect anything now contradicted.
2. **Extract lessons** — from the work just done, write each lesson per the
   priorities above. Be specific and actionable; a vague "be careful with configs"
   is useless, "MCP servers load from ~/.claude.json, NOT settings.json" is gold.
3. **Store** — `store_memory` (or `supersede_memory` / `apply_correction`) with the
   right universe, priority, category, and scope. Tag with what makes it findable
   when a similar task starts (the task signature, not just keywords).
4. **Hygiene** — `query_memory` for anything previously stored as "planned"/"todo"
   that this work completed; `delete_memory` or `correct_memory` the stale ones.
5. **Promote** — if a project lesson is clearly universal, it will auto-promote, or
   call `promote_to_general`.

## The litmus test

Before storing, ask: *"If a fresh Claude started this task tomorrow, which one
sentence would stop it from making the mistake I just made (or saw)?"* Store that
sentence. If nothing here would have changed a fresh Claude's behavior, store
nothing — don't pad memory with restatements of what's already obvious from code.
