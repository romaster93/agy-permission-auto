---
name: codex-review-gate
description: >-
  Manage the OpenAI Codex stop-time review gate and run on-demand Codex code/adversarial reviews in Antigravity (agy).
  Use when the user asks to enable, disable, or check the Codex review gate ("/codex:setup --enable-review-gate",
  "/codex-review-gate", "codex 리뷰 게이트 켜줘/꺼줘"), or asks to run a Codex review or adversarial review.
---

# Codex Review Gate Skill (`codex-review-gate`)

Manage the stop-time Codex review gate (**disabled by default**) or run on-demand Codex reviews (`review`, `adversarial-review`) in Antigravity (`agy`).

## 1. Setup & Toggle Stop-Time Review Gate (`/codex:setup` equivalent)

Run `codex-review-gate.py setup` via `run_command`:

- **Check status**:
  ```bash
  python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup
  ```
- **Enable Stop Review Gate (current workspace)**:
  ```bash
  python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --enable-review-gate
  ```
- **Disable Stop Review Gate (current workspace)**:
  ```bash
  python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --disable-review-gate
  ```
- **Enable / Disable Globally (all workspaces)**:
  ```bash
  python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --enable-review-gate --global
  python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --disable-review-gate --global
  ```

### How the Stop Review Gate Works When Enabled
1. At the end of each turn (`Stop` hook), `codex-review-gate.py` checks if the review gate is enabled (`reviewGateEnabled: true`).
2. **0ms Fast-Path**: It inspects `transcript.jsonl` for the current turn. If no code-editing tools (`write_file`, `edit_file`, `run_command`, etc.) were executed in the turn that just ended, it exits immediately in 0ms without calling Codex.
3. **Code-Change Review**: If code was modified in the turn, it runs `codex exec -s read-only --ephemeral` to review the turn's changes:
   - `ALLOW:` -> The turn finishes normally.
   - `BLOCK: <한국어 사유>` -> Returns `{"decision": "continue", "reason": "..."}` to `agy`, automatically continuing the agent turn so the agent fixes the issue before stopping.

---

## 2. On-Demand Codex Code Review (`/codex:review` equivalent)

To run a standalone Codex review of the current git working tree or branch without modifying files:

```bash
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py review
```

Options:
- `--base <branch>` (e.g., `--base main`)

---

## 3. On-Demand Adversarial Review (`/codex:adversarial-review` equivalent)

To run an adversarial design/tradeoff review challenging assumptions and edge cases:

```bash
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py adversarial-review "<optional focus text>"
```
