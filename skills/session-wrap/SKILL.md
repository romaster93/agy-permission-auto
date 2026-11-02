---
name: session-wrap
description: >-
  Comprehensive session wrap-up workflow with multi-agent analysis. Use when the user asks to
  "wrap up session", "end session", "session wrap", "/wrap", "/session-wrap", "세션 정리",
  "document learnings", "what should I commit", or wants to analyze completed work before
  ending a coding session.
---

# Session Wrap Skill (`session-wrap`)

Comprehensive session wrap-up workflow with parallel multi-agent analysis adapted for Antigravity (`agy`).

## Execution Flow

```text
┌──────────────────────────────────────────────────────────┐
│  1. Check Git Status (`run_command`)                     │
├──────────────────────────────────────────────────────────┤
│  2. Phase 1: 4 Analysis Subagents (Parallel)             │
│     (`invoke_subagent` with `TypeName: "research"`)      │
│     ┌──────────────────────┬──────────────────────┐      │
│     │  doc-updater         │  automation-scout    │      │
│     │  (GEMINI.md/AGENTS/  │  (skills/rules/      │      │
│     │   CLAUDE.md/context) │   plugins/hooks)     │      │
│     ├──────────────────────┼──────────────────────┤      │
│     │  learning-extractor  │  followup-suggester  │      │
│     │  (TIL & mistakes)    │  (P0-P3 next tasks)  │      │
│     └──────────────────────┴──────────────────────┘      │
├──────────────────────────────────────────────────────────┤
│  3. Phase 2: Validation Subagent (Sequential)            │
│     ┌────────────────────────────────────────────┐       │
│     │             duplicate-checker              │       │
│     │     (Validate Phase 1 doc & automation)    │       │
│     └────────────────────────────────────────────┘       │
├──────────────────────────────────────────────────────────┤
│  4. Integrate Results & Prompt User (`ask_question`)     │
├──────────────────────────────────────────────────────────┤
│  5. Execute Selected Actions                             │
└──────────────────────────────────────────────────────────┘
```

For detailed role rubrics and output templates of each subagent, read [references/agents.md](./references/agents.md).
For multi-agent orchestration principles, see [references/multi-agent-patterns.md](./references/multi-agent-patterns.md).

---

## Step 1: Check Git Status

Run `git status` and `git diff` via `run_command`:

```bash
git status --short
git diff --stat HEAD~3 2>/dev/null || git diff --stat
```

*(If the user passed an explicit commit message argument with the wrap request, e.g. `session-wrap "feat: ..."`, you may fast-path directly to committing or ask if they want the full analysis first.)*

---

## Step 2: Phase 1 — Launch 4 Analysis Subagents in Parallel

Prepare a concise **Session Summary** block summarizing:
- **Work**: Main tasks performed in this session
- **Files**: Created or modified files
- **Decisions**: Key technical/design decisions and root-cause findings

Then invoke **all 4 `research` subagents in parallel in a single turn** using `invoke_subagent` (`TypeName: "research"`, `Model: "inherit"`), and end your turn to wait for their reactive notifications:

1. **Doc Updater** (`Role: "Doc Updater"`):
   - Prompt the subagent with `[Session Summary]` and instruct it to check project/global `GEMINI.md`, `AGENTS.md`, `CLAUDE.md`, `context.md`, and `~/.gemini/config/GEMINI.md` for worth-documenting updates (exact markdown additions, target section, rationale).
2. **Automation Scout** (`Role: "Automation Scout"`):
   - Prompt the subagent with `[Session Summary]` and instruct it to check `~/.gemini/config/skills/`, `.agents/skills/`, `~/.gemini/config/rules/`, `.agents/rules/`, and `~/.claude/skills/` to detect repetitive patterns that should become an Antigravity Skill (`SKILL.md`), Rule (`GEMINI.md` / `.agents/rules/*.md`), or Hook/Plugin.
3. **Learning Extractor** (`Role: "Learning Extractor"`):
   - Prompt the subagent with `[Session Summary]` and instruct it to extract technical discoveries, what worked well, mistakes/root causes, and process improvements in structured TIL format.
4. **Follow-up Suggester** (`Role: "Follow-up Suggester"`):
   - Prompt the subagent with `[Session Summary]` and instruct it to scan modified files (and `TODO`/`FIXME`/`WIP` markers if relevant) and prioritize incomplete tasks and next-session steps (`P0` Urgent to `P3` Low, plus Quick Wins `< 1 hour`).

---

## Step 3: Phase 2 — Validation Subagent (Sequential)

Once all 4 Phase 1 subagents report back:
- If `doc-updater` and `automation-scout` proposed any additions, invoke a `research` subagent (`Role: "Duplicate Checker"`, `Model: "flash"` or `"inherit"`) with their proposals to verify whether each proposal is:
  1. **Approved** (Unique — safe to add)
  2. **Merge Recommended** (Partial overlap — show existing file/line and merged text)
  3. **Skip Recommended** (Already documented or automated)
- *(Optimization: If Phase 1 produced 0 doc/automation proposals, or if the workspace is small enough that you can verify duplicates directly with 1-2 `read_file` calls, you may perform this check directly without spawning an extra subagent.)*

---

## Step 4: Integrate Results & Present Summary

Present a structured Markdown report to the user:

```markdown
## Session Wrap Analysis Results

### 1. Git & Changes Summary
[Brief summary of modified/untracked files]

### 2. Documentation Updates (`GEMINI.md` / `CLAUDE.md` / `context.md`)
[doc-updater proposals + duplicate-checker status (Approved / Merge / Skip)]

### 3. Automation Suggestions (Skills / Rules / Hooks)
[automation-scout proposals + duplicate-checker status]

### 4. Key Learnings (TIL & Root Causes)
[learning-extractor highlights]

### 5. Follow-up Tasks & Next Session Priorities
[followup-suggester prioritized list (P0–P3 & Quick Wins)]
```

---

## Step 5: Action Selection (`ask_question`)

Call `ask_question` with `is_multi_select: true` so the user can choose which follow-up actions to execute immediately:

```json
{
  "questions": [
    {
      "question": "세션 마무리 작업 중 실행할 항목을 선택해 주세요.",
      "is_multi_select": true,
      "options": [
        "(Recommended) 변경사항 Git 커밋 생성",
        "문서 업데이트 반영 (GEMINI.md / CLAUDE.md / context.md)",
        "제안된 자동화 생성 (Skill / Rule)",
        "아무 작업도 하지 않고 종료 (Skip)"
      ]
    }
  ]
}
```

---

## Step 6: Execute Selected Actions

Execute **only** the actions selected by the user (e.g., staging & committing changes, editing `GEMINI.md`/`CLAUDE.md`/`context.md`, or creating a new `SKILL.md` under `~/.gemini/config/skills/` or `.agents/skills/`).

---

## Quick Reference

### When to Use
- End of a significant coding or research session
- Before switching to a different project or branch
- After completing a feature, debugging a complex issue, or finishing a paper/patent milestone

### When to Skip
- Very short session with trivial or zero changes
- Purely exploratory read-only Q&A where no actionable learnings or changes occurred
