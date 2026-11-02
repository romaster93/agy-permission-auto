# Session Wrap Subagent Specifications

Detailed prompts, rubrics, and output formats for the 5 specialized subagents used in `session-wrap`.

---

## 1. Doc Updater (`Role: "Doc Updater"`)

### Core Responsibilities
1. **Session Context Analysis**: Identify session discoveries, configurations, and architectural decisions worth documenting.
2. **Update Classification**: Determine which file to update:
   - Project `GEMINI.md` / `AGENTS.md` / `CLAUDE.md`
   - Global `~/.gemini/config/GEMINI.md` / `~/.gemini/config/rules/*.md`
   - Project `context.md`
3. **Specific Proposals**: Provide exact Markdown blocks to add, not vague suggestions.
4. **Duplicate Prevention**: Read existing docs before proposing additions.

### Targets & Criteria
- **`GEMINI.md` / `AGENTS.md` / `CLAUDE.md`**:
  - New skills (`~/.gemini/config/skills/`, `.agents/skills/`), rules, hooks, or plugins
  - Environment changes (env vars, dependencies, build/test commands)
  - Project structure changes or cross-project standards
- **`context.md`**:
  - Project-specific domain/business constraints, technical caveats, or historical "why" behind non-intuitive decisions

### Output Template
```markdown
# Documentation Update Analysis

## Summary
- GEMINI.md / CLAUDE.md updates recommended: [X]
- context.md updates recommended: [X]

## Proposed Updates
### [Target File Path] — Section: [Section Name]
**Content to Add:**
```markdown
[Exact markdown to insert]
```
**Rationale:** [Why needed for future sessions]
**Location:** [Exact section/line context]
**Duplicate Check:** [Not found / Similar content at line X]
```

---

## 2. Automation Scout (`Role: "Automation Scout"`)

### Core Responsibilities
1. **Pattern Detection**: Identify repetitive workflows (frequency >= 2), multi-tool chains, or recurring verification/formatting tasks from the session.
2. **Automation Classification (Antigravity Customizations)**:
   - **Skill (`~/.gemini/config/skills/<name>/SKILL.md` or `.agents/skills/<name>/SKILL.md`)**: Multi-step procedures, runbooks, external tool orchestration, or specialized checklists.
   - **Rule (`GEMINI.md`, `.agents/rules/*.md`, `~/.gemini/config/rules/*.md`)**: Standing behavioral constraints, coding/documentation conventions, or domain guardrails.
   - **Hook / Plugin (`hooks.json`, `plugin.json`)**: PreToolUse/PostToolUse lifecycle enforcement.
3. **Duplicate Check**: Check `~/.gemini/config/skills/`, `.agents/skills/`, and `~/.claude/skills/` first.

### Output Template
```markdown
# Automation Opportunity Analysis

## Summary
- Skills recommended: [X]
- Rules recommended: [X]
- Hooks/Plugins recommended: [X]

## Recommendations
### [Automation Name] ([High / Medium / Low Priority])
- **Type**: [Skill / Rule / Hook]
- **Target Path**: `[e.g. ~/.gemini/config/skills/example/SKILL.md]`
- **Detected Pattern**: [Workflow and frequency]
- **Proposed Skeleton**:
```markdown
---
name: [skill-name]
description: >-
  [When and how to use this skill]
---
# [Skill Title]
[Key steps]
```
- **Similar Existing Automation**: [None / Path to existing skill]
```

---

## 3. Learning Extractor (`Role: "Learning Extractor"`)

### Core Responsibilities
1. **Technical Discoveries**: New APIs, CLI flags, internal behaviors, or architectural patterns discovered during the session.
2. **Problem-Solving Lessons**: What worked well and why; failed attempts and root causes.
3. **Mistakes & Prevention**: Misleading symptoms, actual root causes, and how to prevent recurrence.
4. **Process Improvements**: Faster or cleaner ways to run builds, tests, or debugging.

### Output Template
```markdown
# Session Learning Extraction (TIL)

## Summary
- Technical discoveries: [X]
- Success patterns: [X]
- Mistakes & root causes documented: [X]

## Technical Discoveries
### [Discovery Name]
- **What & Context**: [Description]
- **Key Insight**: [Main lesson]
- **Example / Snippet**: `[Code or command]`
- **Gotchas**: [Warnings or limitations]

## Mistakes & Lessons
### [Issue / Mistake]
- **Symptoms vs. Root Cause**: [What looked wrong vs. actual cause]
- **Fix & Prevention**: [How it was solved and how to avoid next time]
```

---

## 4. Follow-up Suggester (`Role: "Follow-up Suggester"`)

### Core Responsibilities
1. **Incomplete Task Detection**: Scan modified files and session context for unfinished features, `TODO`/`FIXME`/`WIP` comments, or deferred decisions.
2. **Testing & Validation Gaps**: Identify untested code paths or edge cases.
3. **Prioritization**:
   - **P0 (Urgent)**: Blockers, broken builds/tests, security or data integrity issues
   - **P1 (High)**: Incomplete core features, important tests, major tech debt
   - **P2 (Medium)**: Refactoring, documentation gaps, minor features
   - **P3 (Low)**: Future ideas, optional enhancements
4. **Quick Wins**: Highlight high-impact tasks taking `< 1 hour`.

### Output Template
```markdown
# Follow-up Tasks & Next Session Priorities

## Recommended Focus for Next Session
[1-2 sentence summary of what to tackle first]

## Prioritized Tasks (P0–P3)
### [P0/P1/P2/P3] [Task Title]
- **Category**: [Feature / Bug / Test / Docs / Refactor]
- **Specific Steps**:
  1. [Step 1]
  2. [Step 2]
- **Done Criteria**: [How to verify completion]
- **Related Files**: `[file paths]`
- **Expected Effort**: [Quick (<1h) / Medium (1-4h) / Large (>4h)]

## Quick Wins (< 1 hour, High Impact)
1. **[Task Name]** — [Brief justification & target files]
```

---

## 5. Duplicate Checker (`Role: "Duplicate Checker"` — Phase 2)

### Core Responsibilities
1. Receive Phase 1 proposals from `Doc Updater` and `Automation Scout`.
2. Search existing `GEMINI.md`, `AGENTS.md`, `CLAUDE.md`, `context.md`, `~/.gemini/config/skills/`, `.agents/skills/`, and `~/.claude/skills/`.
3. Classify each proposal into:
   - **Approved**: Unique content, safe to add
   - **Merge Recommended**: Partial overlap with existing file/line (provide merged block)
   - **Skip Recommended**: Complete duplicate already covered at `[file:line]`

### Output Template
```markdown
# Phase 2 Duplicate Validation Results

| Proposal Source | Total | Approved | Merge | Skip |
|---|---|---|---|---|
| doc-updater | [X] | [X] | [X] | [X] |
| automation-scout | [X] | [X] | [X] | [X] |

## Detailed Verdicts
- **[Proposal Title]**: [Approved / Merge / Skip] — [File path & line reference + merged text if applicable]
```
