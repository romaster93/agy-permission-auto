# Multi-Agent Orchestration Patterns for Antigravity (`agy`)

Detailed patterns for designing multi-agent workflows in Antigravity (`agy`), adapted from Anthropic's Multi-Agent Research principles.

## Core Principles

> **"Agent architecture should reflect the dependency graph of the task."**

- If subtasks do not read or modify each other's state, run them **in parallel** (invoke multiple `invoke_subagent` tool calls in a single turn).
- If a subsequent step depends on the output of previous steps, run it **sequentially** after the parallel wave finishes.

## Parallel vs. Sequential Decision Criteria

| Condition | Recommended Pattern |
|---|---|
| Subtasks are independent (no shared mutable state) | **Parallel** (`invoke_subagent` fan-out in one turn) |
| Previous step output is next step input | **Sequential** |
| Diverse perspectives / specialized analysis needed | **Parallel** (Fan-out) |
| Proposals need deduplication/validation before user prompt | **2-Phase** (Generate in Parallel -> Validate Sequentially) |

## 2-Phase Pipeline Pattern in `session-wrap`

```text
Phase 1: Analysis / Generation (Parallel Fan-out)
┌───────────────┬──────────────────┬────────────────────┬────────────────────┐
│ Doc Updater   │ Automation Scout │ Learning Extractor │ Followup Suggester │
└───────┬───────┴────────┬─────────┴─────────┬──────────┴─────────┬──────────┘
        │                │                   │                    │
        └────────────────┴─────────┬─────────┴────────────────────┘
                                   ↓
Phase 2: Validation (Sequential)
┌────────────────────────────────────────────────────────────────────────────┐
│                             Duplicate Checker                              │
│          (Validates Doc Updater & Automation Scout against codebase)       │
└────────────────────────────────────────────────────────────────────────────┘
```

## Implementing in Antigravity (`agy`)

### Parallel Fan-Out (`invoke_subagent`)
Call `invoke_subagent` multiple times in the **same turn** with `TypeName: "research"` (read-only sandbox, safe for concurrent analysis) and distinct `Role` titles:

1. `invoke_subagent(TypeName="research", Role="Doc Updater", Prompt="...")`
2. `invoke_subagent(TypeName="research", Role="Automation Scout", Prompt="...")`
3. `invoke_subagent(TypeName="research", Role="Learning Extractor", Prompt="...")`
4. `invoke_subagent(TypeName="research", Role="Follow-up Suggester", Prompt="...")`

Then **stop calling tools** to end your turn. The reactive wakeup system will automatically resume you when the subagents finish and report their findings.

### Model Selection Guidelines
- **`inherit`** (Default): Uses the parent session's active model for deep contextual analysis.
- **`flash` / `flash_lite`**: Fast, lightweight validation (e.g., Phase 2 `Duplicate Checker`).
- **`pro`**: Complex multi-file reasoning when extra depth is explicitly requested.
