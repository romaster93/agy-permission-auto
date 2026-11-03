# Codex Review Gate Plugin for Antigravity (`agy`)

OpenAI Codex (`gpt-6.1-sol` by default) stop-time review gate (`Stop` hook) and on-demand code/adversarial review plugin for Antigravity (`agy`).

## Key Features
- **Default Model `gpt-6.1-sol` (`--model` Override Supported)**:
  - Uses `gpt-6.1-sol` by default to preserve `gpt-6-astra` quota while maintaining strong review quality.
- **Disabled by Default (`reviewGateEnabled: false`)**:
  - Zero overhead until explicitly enabled for a workspace (or globally) via toggle command.
- **0ms Q&A Turn Fast-Path (`transcript.jsonl` Inspection)**:
  - Even when enabled, if a turn only involved reading files or answering questions (no `write_file`, `edit_file`, or `run_command`), the hook exits immediately in 0ms without invoking Codex.
- **Automatic Turn Continuation on `BLOCK:`**:
  - When code changes were made in the turn and Codex finds issues that must be fixed before stopping, it returns `{"decision": "continue", "reason": "[Codex Review Gate] ..."}` in Korean so `agy` immediately fixes them before ending the turn.

## Usage (Toggle Commands)

```bash
# Check status
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup

# Enable review gate for current workspace
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --enable-review-gate

# Disable review gate for current workspace
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --disable-review-gate

# Enable / Disable globally across all workspaces
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --enable-review-gate --global
python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --disable-review-gate --global
```

## Structure

```text
codex-review-gate/
├── plugin.json             # Plugin manifest
├── hooks.json              # Stop lifecycle hook registration
├── codex-review-gate.py    # Stop hook & CLI companion script
├── skills/
│   └── codex-review-gate/
│       └── SKILL.md        # Skill for toggling & running reviews
└── README.md
```
