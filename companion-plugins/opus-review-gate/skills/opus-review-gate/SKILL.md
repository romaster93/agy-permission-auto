---
name: opus-review-gate
description: >-
  Manage the Claude Opus 5.5 (`claude-opus-5-5` in `agy`) stop-time review gate and run on-demand
  Opus 5.5 code/adversarial reviews in Antigravity (agy). Use when the user asks to enable, disable,
  or check the Opus review gate ("/opus-review-gate", "opus 리뷰 게이트 켜줘/꺼줘"), or asks to run an
  Opus code review or adversarial review.
---

# Opus 5.5 Review Gate Skill (`opus-review-gate`)

Manage the stop-time Claude Opus 5.5 (`claude-opus-5-5`) review gate (**disabled by default**) or run on-demand Opus 5.5 reviews (`review`, `adversarial-review`) in Antigravity (`agy`).

## 1. Setup & Toggle Stop-Time Review Gate

Run `opus-review-gate.py setup` via `run_command`:

- **Check status**:
  ```bash
  python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup
  ```
- **Enable Opus 5.5 Stop Review Gate (current workspace)**:
  ```bash
  python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --enable-review-gate
  ```
- **Disable Opus 5.5 Stop Review Gate (current workspace)**:
  ```bash
  python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --disable-review-gate
  ```
- **Enable / Disable Globally (all workspaces)**:
  ```bash
  python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --enable-review-gate --global
  python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --disable-review-gate --global
  ```

### How the Opus 5.5 Stop Review Gate Works When Enabled
1. At the end of each turn (`Stop` hook), `opus-review-gate.py` checks if the review gate is enabled (`reviewGateEnabled: true`).
2. **0ms Fast-Path**: It inspects `transcript.jsonl` for the current turn. If no code-editing tools (`write_file`, `edit_file`, `run_command`, etc.) were executed in the turn that just ended, it exits immediately in 0ms without calling Opus.
3. **Code-Change Review**: If code was modified in the turn, it collects the turn's edited files and `git diff` and runs `agy --model claude-opus-5-5 -p`:
   - `ALLOW:` -> The turn finishes normally.
   - `BLOCK: <한국어 사유>` -> Returns `{"decision": "continue", "reason": "..."}` to `agy`, automatically continuing the agent turn so the agent fixes the issue before stopping.

---

## 2. On-Demand Opus 5.5 Code Review

To run a standalone Claude Opus 5.5 review of the current git working tree or branch without modifying files:

```bash
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py review
```

Options:
- `--base <branch>` (e.g., `--base main`)

---

## 3. On-Demand Opus 5.5 Adversarial Review

To run an adversarial design/tradeoff review challenging assumptions and edge cases with Claude Opus 5.5:

```bash
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py adversarial-review "<optional focus text>"
```
