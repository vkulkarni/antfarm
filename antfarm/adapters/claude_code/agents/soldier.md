# Antfarm Soldier — Claude Code Agent (v0.2 Placeholder)

> **v0.1 note:** In v0.1, the Soldier role is implemented as a deterministic Python script (`antfarm/soldier.py`). It detects conflicts, pauses tasks, and escalates to the Queen without requiring an AI model. No agent definition is used at runtime in v0.1.

## v0.2 — AI-Assisted Conflict Resolution

This agent definition will be activated in **v0.2** to bring AI reasoning into the Soldier's conflict-resolution loop.

In v0.2, when the Soldier detects a merge conflict or a task collision that cannot be resolved deterministically, it will spawn this Claude Code agent to:

1. **Inspect the conflict** — read the differing files, understand intent from task descriptions and commit messages
2. **Propose a resolution** — suggest a merge strategy or reorder tasks to eliminate the conflict
3. **Apply and verify** — implement the resolution, run tests, confirm correctness
4. **Report back** — return a structured decision so the deterministic Soldier can continue orchestration

Until v0.2 is implemented, this file serves as documentation of the planned interface and a placeholder for future agent logic.
