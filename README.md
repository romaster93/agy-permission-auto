# Permission Reviewer Plugin for Antigravity (`agy`)

AI-assisted permissive permission reviewer and safety gate ported from Claude Code hooks.

## Quick Install (다른 컴퓨터에 설치)

```bash
mkdir -p ~/.gemini/config/plugins
git clone https://github.com/romaster93/agy-permission-auto.git ~/.gemini/config/plugins/permission-reviewer
```

설치 후 `agy`를 실행하면 첫 도구 호출 시 자동으로 `~/.gemini/antigravity-cli/settings.json`에 `permissions.allow` 와일드카드 권한(`command(*)`, `write_file(*)` 등)을 부트스트랩하여, `agy` 내장 Auto mode 심사기가 불필요하게 재차 확인창을 띄우지 않고 **본 플러그인(`auto_approve` / `force_ask`)이 전권을 위임받아 심사**하도록 설정됩니다.

> **참고**: 처음 설치한 직후 실행 중이던 세션이 있다면, 자동 설정된 `settings.json`이 반영되도록 `agy`를 한 번 재시작해 주세요.

업데이트 시:
```bash
git -C ~/.gemini/config/plugins/permission-reviewer pull
```

## Features

- **Built-in Auto Mode Classifier Bypass**:
  - `agy`의 내장 Auto mode 심사기가 반환하는 `"ask"`(우선순위 3)가 플러그인의 `"auto_approve"`(우선순위 2)를 덮어쓰는 문제를 방지하기 위해, `~/.gemini/antigravity-cli/settings.json`의 `permissions.allow`에 와일드카드 규칙을 자동 등록합니다.
  - 반면 플러그인의 보안 게이트나 AI 리뷰어가 위험하다고 판단한 작업은 `"force_ask"`(우선순위 5)를 반환하므로 `permissions.allow`를 무시하고 **반드시 사용자 확인 프롬프트**를 띄웁니다.
- **Multi-tier LLM Reviewer**:
  - Tier 1: `agy --model claude-opus-5-5 -p ...` / `claude-sonnet-5-5`
  - Tier 2: `agy -p ...` (default active model fallback in `agy`)
  - Tier 3: `claude -p --model opus` / `sonnet` (fallback if `agy` fails)
  - Tier 4: Fallback to manual confirmation (`force_ask`)
- **Strict User Policy**: **Never auto-drops or hard-denies**. Operations judged risky always prompt the user with reasons.
- **Fast-path Optimization**: Safe sandboxed commands and safe workspace file edits pass immediately with 0ms overhead.
- **Static Safety Gates**:
  - Blocks automated `.env` file reads/edits (prompts user via `force_ask`).
  - Classifies `psql`: read-only `SELECT` queries auto-approved, `DROP` or write queries prompt user via `force_ask`.

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
