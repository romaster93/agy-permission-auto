# Permission Reviewer Plugin for Antigravity (`agy`)

AI-assisted permissive permission reviewer and safety gate ported from Claude Code hooks.

## Quick Install (다른 컴퓨터에 설치)

```bash
mkdir -p ~/.gemini/config/plugins
git clone https://github.com/romaster93/agy-permission-auto.git ~/.gemini/config/plugins/permission-reviewer
```

설치 후 `agy`를 실행하면 자동으로 플러그인이 로드 및 활성화됩니다.

업데이트 시:
```bash
git -C ~/.gemini/config/plugins/permission-reviewer pull
```

## Features

- **Multi-tier LLM Reviewer**:
  - Tier 1: `agy --model claude-opus-5-5 -p ...` / `claude-sonnet-5-5`
  - Tier 2: `agy -p ...` (default active model fallback in `agy`)
  - Tier 3: `claude -p --model opus` / `sonnet` (fallback if `agy` fails)
  - Tier 4: Fallback to manual confirmation (`force_ask`)
- **Strict User Policy**: **Never auto-drops or hard-denies**. Operations judged risky always prompt the user with reasons.
- **Fast-path Optimization**: Safe sandboxed commands and safe workspace file edits pass immediately with 0ms overhead.
- **Static Safety Gates**:
  - Blocks automated `.env` file reads/edits (prompts user).
  - Classifies `psql`: read-only `SELECT` queries auto-approved, `DROP` or write queries prompt user.

## Structure

```text
permission-reviewer/
├── plugin.json             # Plugin manifest
├── hooks.json              # PreToolUse lifecycle hook registration
├── permission-reviewer.py  # Reviewer script
└── README.md
```

## Management

To enable or disable via `agy`:
```bash
agy plugin enable permission-reviewer
agy plugin disable permission-reviewer
```
