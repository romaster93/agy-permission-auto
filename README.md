# Permission Reviewer Plugin for Antigravity (`agy`)

AI-assisted permissive permission reviewer and safety gate ported from Claude Code hooks.

## Quick Install (다른 컴퓨터에 설치)

```bash
mkdir -p ~/.gemini/config/plugins
git clone https://github.com/romaster93/agy-permission-auto.git ~/.gemini/config/plugins/permission-reviewer
```

설치 후 `agy`를 실행하면 첫 도구 호출 시 자동으로 `~/.gemini/antigravity-cli/settings.json`에 `permissions.allow` 와일드카드 권한(`command(*)`, `write_file(*)` 등), **컨텍스트 60%·70% `session-wrap` 제안 알림 & 90% 도달 시 자동 Wrap 후 즉시 컴팩션(`pre-compact-wrap.py` + `"contextWindow": "1M"`, `~/.bashrc`의 `AGY_CLI_AUTO_COMPACT_OVERRIDE="95%"`)**, 그리고 **하단 상태 표시줄 HUD(`statusLine` → `~/.local/bin/agy-statusline.sh`)**를 부트스트랩하여, `agy` 내장 Auto mode 심사기가 불필요하게 재차 확인창을 띄우지 않고 **본 플러그인(`auto_approve` / `force_ask`)이 전권을 위임받아 심사**하도록 설정됩니다.

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
- **Fast-path Optimization**:
  - Safe sandboxed commands and safe workspace file edits (`write_file`, `edit_file`, `write_to_file`, `replace_file_content`, `multi_replace_file_content`) pass immediately with 0ms overhead.
  - Non-sensitive read-only operations (`read_file`, `view_file`, `grep_search`, `list_dir`, `find_by_name`, `search_web`) pass immediately even outside the workspace (e.g. `~/.cache`), regardless of whether `agy` Auto mode is on or off.
- **Static Safety Gates**:
  - Blocks automated `.env` and sensitive credential path (`~/.ssh`, `~/.aws`, `~/.gnupg`) reads/edits (prompts user via `force_ask`).
  - Classifies `psql`: read-only `SELECT` queries auto-approved, `DROP` or write queries prompt user via `force_ask`.
- **3-Stage Pre-Compact Session Wrap & Auto-Compaction (`pre-compact-wrap.py`)**:
  - **1차 제안 (60% 도달)**: 자동으로 바로 실행하지 않고 턴 종료 시점에 **`session-wrap` + `compact` 1차 제안 알림 배너**를 띄웁니다. 거절(또는 일반 작업 지시) 시 Wrap·Compact 모두 하지 않고 계속 작업합니다.
  - **2차 제안 (70% 도달)**: 60%에서 거절 후 **70%**에 도달하면 다시 한 번 **`session-wrap` + `compact` 2차 제안 배너**를 띄웁니다. 거절 시 역시 Wrap·Compact 없이 계속 작업합니다.
  - **강제 자동 실행 (90% 도달)**: 70%에서도 거절 후 **90%**에 도달하면 묻지 않고 `Stop` 훅에서 **자동으로 즉시 `session-wrap`을 실행**하고, 마무리 작업이 끝나자마자 **곧바로 컨텍스트 컴팩션(Checkpoint)**을 수행합니다.
  - 어느 단계에서든 `session-wrap`이 완료되면 `PostInvocation` 훅에서 네이티브 `CortexStepCheckpoint` (`{"checkpoint": {"sessionSummary": "..."}}`)를 주입하여 그 자리에서 즉시 컴팩션을 수행합니다.
- **Bundled HUD (`hud/agy-statusline.sh`)**:
  - `agy` 하단 상태 표시줄(Status Line HUD)에 현재 활성 모델(`◆ Model`), 컨텍스트 윈도우 사용률(`Ctx [████░░░░] % (In/Out)`), 모델 버킷별 남은 쿼터 및 리셋 타이머(`Quota`)를 실시간으로 표시합니다.
  - `~/.local/bin/agy-statusline.sh` 및 `~/.gemini/antigravity-cli/hud/agy-statusline.sh`로 자동 동기화되며, `claude-usage-monitor` 트레이 앱과 연동할 수 있도록 `~/.gemini/antigravity-cli/hud/cache/last_stdin.json`에 최신 상태 캐시를 기록합니다.
- **Bundled Skills (`skills/`)**:
  - **`motivation-not-novelty`**: 연구/논문 작성 시 단순 현상 관찰(Motivation)에서 근본 원인 진단·방법론 도출·필수성 입증(Novelty)으로 끌어올리는 가이드라인 스킬
  - **`session-wrap`**: 세션 종료 전 4개의 병렬 분석 서브에이전트(`Doc Updater`, `Automation Scout`, `Learning Extractor`, `Follow-up Suggester`) + 중복 검증 서브에이전트(`Duplicate Checker`)를 실행하여 문서화·자동화·TIL·후속 작업을 정리하는 워크플로우 스킬
- **Companion Stop-Time Review Gate Plugins (`companion-plugins/`, 자동 분리 설치 & 기본 비활성화)**:
  - **`codex-review-gate`** (`~/.gemini/config/plugins/codex-review-gate`): OpenAI Codex CLI (`gpt-6.1-sol` 기본값) 기반 턴 종료 리뷰 게이트 (`setup --enable-review-gate` / `--disable-review-gate`)
  - **`opus-review-gate`** (`~/.gemini/config/plugins/opus-review-gate`): `agy` 내장 Claude Opus 5.5 (`claude-opus-5-5`) 기반 턴 종료 리뷰 게이트 (`setup --enable-review-gate` / `--disable-review-gate`)

## Structure

```text
permission-reviewer/
├── plugin.json             # Plugin manifest
├── hooks.json              # PreToolUse, PostInvocation & Stop hook registration
├── permission-reviewer.py  # Reviewer & bootstrap script
├── pre-compact-wrap.py     # 60% context session-wrap suggestion & post-wrap checkpoint compaction hook
├── hud/                    # Bundled Antigravity statusLine HUD (auto-synced)
│   └── agy-statusline.sh
├── skills/                 # Bundled Antigravity skills (auto-loaded & synced)
│   ├── motivation-not-novelty/
│   │   └── SKILL.md
│   └── session-wrap/
│       ├── SKILL.md
│       └── references/
│           ├── agents.md
│           └── multi-agent-patterns.md
├── companion-plugins/      # Separate Stop Review Gate plugins (auto-synced to ~/.gemini/config/plugins/)
│   ├── codex-review-gate/
│   └── opus-review-gate/
└── README.md
```

## Management

To enable or disable via `agy`:
```bash
agy plugin enable permission-reviewer
agy plugin disable permission-reviewer
```
