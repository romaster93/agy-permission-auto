# Opus 5.5 Review Gate Plugin for Antigravity (`agy`)

Claude Opus 5.5 (`claude-opus-5-5` via `agy -p`) stop-time review gate (`Stop` hook) and on-demand code/adversarial review plugin for Antigravity (`agy`).

## Key Features
- **Disabled by Default (`reviewGateEnabled: false`)**:
  - Zero overhead until explicitly enabled for a workspace (or globally) via toggle command.
- **0ms Q&A Turn Fast-Path (`transcript.jsonl` Inspection)**:
  - Even when enabled, if a turn only involved reading files or answering questions (no `write_file`, `edit_file`, or `run_command`), the hook exits immediately in 0ms without invoking Opus 5.5.
- **Automatic Turn Continuation on `BLOCK:`**:
  - When code changes were made in the turn and Claude Opus 5.5 finds concrete bugs, broken invariants, or regressions, it returns `{"decision": "continue", "reason": "[Opus Review Gate (Claude Opus 5.5)] ..."}` in Korean so `agy` immediately fixes them before ending the turn.

## Usage (Toggle Commands)

```bash
# Check status
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup

# Enable review gate for current workspace
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --enable-review-gate

# Disable review gate for current workspace
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --disable-review-gate

# Enable / Disable globally across all workspaces
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --enable-review-gate --global
python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --disable-review-gate --global
```
