#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607, TRY300
"""
Antigravity (agy) 3-Stage Pre-Compact Session Wrap & Auto-Compaction Hook.

3-Stage Escalation Policy:
1. 60% Context (1st Suggestion — Non-intrusive):
   - Does NOT automatically run `session-wrap` or `compact`.
   - At turn completion (`PostInvocation`), displays a suggestion banner asking if the
     user wants to run `session-wrap` + `compact`.
   - If accepted: runs `session-wrap` -> `ask_question` -> executes selected actions ->
     immediately compacts (`CortexStepCheckpoint` injection).
   - If declined/ignored: does NEITHER `session-wrap` NOR `compact`, continues working.
2. 70% Context (2nd Suggestion — Limit Re-Suggestion):
   - If declined at 60% and context reaches 70%, asks the user AGAIN at 70%.
   - If accepted: runs `session-wrap` -> executes selected actions -> immediately compacts.
   - If declined/ignored: does NEITHER `session-wrap` NOR `compact`, continues working.
3. 90% Context (Forced Auto-Wrap & Auto-Compact):
   - If declined at 70% and context reaches 90%, does NOT ask — intercepts the `Stop`
     hook (`{"decision": "continue", ...}`) to automatically run `session-wrap` right
     away, and as soon as `session-wrap` finishes, immediately compacts the context!
   - Built-in hard ceiling (`AGY_CLI_AUTO_COMPACT_OVERRIDE`) is set to 95% so `agy`
     does not prematurely truncate context while the 90% `session-wrap` is running.
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time

STATE_ROOT = os.path.expanduser("~/.gemini/antigravity-cli/pre-compact-wrap")
CONFIG_PATH = os.path.join(STATE_ROOT, "config.json")
STATE_DIR = os.path.join(STATE_ROOT, "state")
LOG_PATH = os.path.expanduser("~/.local/share/agy-pre-compact-wrap.log")

HUD_LAST_STDIN = os.path.expanduser("~/.gemini/antigravity-cli/hud/cache/last_stdin.json")
HUD_SESSIONS_DIR = os.path.expanduser("~/.gemini/antigravity-cli/hud/cache/sessions")

DEFAULT_SUGGEST_THRESHOLDS = [60.0, 70.0]
DEFAULT_FORCE_THRESHOLD_PCT = 90.0
HARD_SAFETY_CEILING_PCT = 95.0
DEFAULT_CONTEXT_WINDOW_SIZE = 1_048_576
SYSTEM_OVERHEAD_TOKENS_ESTIMATE = 45_000
MAX_WRAP_STEPS = 40

WRAP_SUGGESTION_MODEL_MSG_TEMPLATE = """\
[Pre-Compact Session Wrap Suggestion ({tier_label}: {tier_pct:.0f}%)]
Context window usage is currently {used_pct:.1f}% (suggestion tier: {tier_pct:.0f}%, {next_tier_note}).
A suggestion banner was just shown to the user asking if they want to run `session-wrap` and compact now.
- If the user's next message agrees to wrap (e.g., "응", "어", "그래", "진행해", "해줘", "wrap", "wrap 해줘", "/wrap", "/session-wrap", "세션 정리"), immediately execute the full `session-wrap` skill workflow:
  1. Check `git status --short` and `git diff --stat`
  2. Launch the 4 parallel `research` subagents (`Doc Updater`, `Automation Scout`, `Learning Extractor`, `Follow-up Suggester`)
  3. Validate duplicates and present `## Session Wrap Analysis Results`
  4. Call `ask_question` (`is_multi_select: true`) so the user can select which wrap-up actions to execute
  5. Execute the selected actions and include `<!-- SESSION_WRAP_COMPLETE -->` on the last line of your final response so automatic context compaction runs immediately.
- If the user explicitly asks to compact ONLY without session-wrap (e.g., "compact만 해줘", "wrap 말고 compact만"), briefly summarize the session in your response and include `<!-- COMPACT_ONLY_COMPLETE -->` on the last line.
- If the user declines (e.g., "아니", "안해", "그냥 진행해", "나중에") or gives a normal task instruction, do NEITHER `session-wrap` NOR `compact`. Simply acknowledge briefly (if they said "아니") or execute their task instruction normally.\
"""

WRAP_SUGGESTION_USER_MARKDOWN_60 = """
> 💡 **[Context {used_pct:.1f}% 도달 — Session Wrap 1차 제안 (60%)]**
> 현재 컨텍스트 사용량이 **60%**를 넘었습니다. 지금 `session-wrap` 진행 후 컴팩션할까요?
> - **수락 (Wrap + Compact)**: **`응`** 또는 **`wrap 해줘`** 입력 (`session-wrap` 완료 직후 자동 컴팩션)
> - **거절 (계속 작업)**: **`아니`** 입력 또는 다음 작업 지시 입력 → **Wrap·Compact 모두 하지 않고 계속 작업**하며, 한도 **70%** 도달 시 다시 물어봅니다.
"""

WRAP_SUGGESTION_USER_MARKDOWN_70 = """
> ⚠️ **[Context {used_pct:.1f}% 한도 도달 — Session Wrap 2차 제안 ({tier_pct:.0f}%)]**
> 컨텍스트 사용량이 **{tier_pct:.0f}%**에 도달했습니다. 지금 `session-wrap` 진행 후 컴팩션할까요?
> - **수락 (Wrap + Compact)**: **`응`** 또는 **`wrap 해줘`** 입력 (`session-wrap` 완료 직후 즉시 컴팩션)
> - **Compact만 진행**: **`compact만 해줘`** 입력 (`session-wrap` 없이 즉시 컴팩션)
> - **거절 (계속 작업)**: **`아니`** 입력 또는 다음 작업 지시 입력 → **Wrap·Compact 모두 하지 않고 계속 작업**하며, **{force_pct:.0f}%** 도달 시에는 묻지 않고 자동으로 Wrap 후 Compact가 실행됩니다.
"""

FORCE_WRAP_90_PROMPT_TEMPLATE = """\
[Auto Pre-Compact Session Wrap — {force_pct:.0f}% 도달 자동 실행]
현재 컨텍스트 윈도우 사용량이 {used_pct:.1f}% (강제 실행 임계값 {force_pct:.0f}%)에 도달했습니다.
60% 및 70% 제안 이후 컨텍스트가 90%에 도달했으므로, 컨텍스트 유실 방지를 위해 사용자에게 추가로 묻지 않고 지금 즉시 `session-wrap` 스킬 워크플로우를 실행해 주세요:

1. **Git 상태 확인**: `run_command`로 `git status --short` 및 `git diff --stat`를 확인하세요.
2. **Phase 1 — 4개 분석 서브에이전트 병렬 실행**: `invoke_subagent` (`TypeName: "research"`, `Model: "inherit"`)로 다음 4개를 한 턴에 병렬 호출하세요:
   - `Doc Updater`: `GEMINI.md` / `CLAUDE.md` / `AGENTS.md` / `context.md` 문서화 대상 분석
   - `Automation Scout`: 반복 패턴의 Skill / Rule / Hook 자동화 대상 분석
   - `Learning Extractor`: 핵심 기술 발견(TIL), 시행착오 및 근본 원인 정리
   - `Follow-up Suggester`: 미완료 작업 및 다음 단계 우선순위(P0~P3, Quick Wins) 도출
3. **Phase 2 — 중복 검증**: 제안된 문서/자동화 항목이 기존 설정과 중복되는지 검증하세요.
4. **결과 통합 리포트 출력**: `## Session Wrap Analysis Results` 마크다운 리포트를 출력하세요.
5. **사용자 액션 선택 (`ask_question` 필수)**: 반드시 `ask_question` 도구(`is_multi_select: true`)를 호출하여 실행할 마무리 작업(변경사항 Git 커밋 생성 / 문서 업데이트 반영 / 제안된 자동화 생성 / Skip)을 사용자에게 질문하세요.
6. **선택 작업 실행 및 자동 컴팩션 트리거**: 사용자가 선택한 작업을 모두 실행한 뒤, 최종 완료 응답의 마지막 줄에 반드시 `<!-- SESSION_WRAP_COMPLETE -->` 마커를 포함해 주세요. 마커가 출력되면 훅이 즉시 컨텍스트 컴팩션(Checkpoint)을 수행합니다.\
"""

WRAP_REMINDER_PROMPT = """\
[Auto Pre-Compact Session Wrap 안내] 아직 `session-wrap`의 사용자 액션 선택(`ask_question`) 단계가 완료되지 않았습니다.
분석 결과를 `## Session Wrap Analysis Results`로 정리한 뒤 반드시 `ask_question` 도구를 호출하여 사용자에게 실행할 마무리 작업을 질문하고, 선택된 작업 완료 후 마지막 줄에 `<!-- SESSION_WRAP_COMPLETE -->`를 포함해 주세요.\
"""


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _safe_id(val):
    if not isinstance(val, str):
        return ""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", val.strip())
    return cleaned[:128]


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def load_config():
    cfg = {
        "enabled": True,
        "suggestThresholdsPct": list(DEFAULT_SUGGEST_THRESHOLDS),
        "forceThresholdPct": DEFAULT_FORCE_THRESHOLD_PCT,
    }
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception as e:
        _log(f"load_config error: {e}")

    raw_list = cfg.get("suggestThresholdsPct")
    if not isinstance(raw_list, list) or not raw_list:
        cfg["suggestThresholdsPct"] = list(DEFAULT_SUGGEST_THRESHOLDS)
    else:
        cfg["suggestThresholdsPct"] = sorted(float(x) for x in raw_list)

    force_val = cfg.get("forceThresholdPct", DEFAULT_FORCE_THRESHOLD_PCT)
    try:
        cfg["forceThresholdPct"] = float(force_val)
    except (TypeError, ValueError):
        cfg["forceThresholdPct"] = DEFAULT_FORCE_THRESHOLD_PCT
    return cfg


def load_conv_state(conv_id):
    safe = _safe_id(conv_id)
    if not safe:
        return {"status": "idle", "last_suggested_tier": 0.0}
    path = os.path.join(STATE_DIR, f"{safe}.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"load_conv_state error ({conv_id}): {e}")
    return {"status": "idle", "last_suggested_tier": 0.0}


def save_conv_state(conv_id, state):
    safe = _safe_id(conv_id)
    if not safe:
        return
    path = os.path.join(STATE_DIR, f"{safe}.json")
    try:
        _atomic_write_json(path, state)
    except Exception as e:
        _log(f"save_conv_state error ({conv_id}): {e}")


def is_subagent_or_internal(payload):
    if (
        os.environ.get("AGY_PERMISSION_REVIEWER_ACTIVE") == "1"
        or os.environ.get("CLAUDE_PERMISSION_REVIEWER_ACTIVE") == "1"
        or int(os.environ.get("AGY_PERMISSION_REVIEWER_DEPTH", "0")) >= 1
    ):
        return True

    conv_id = str(payload.get("conversationId") or "").strip()
    parent_id = str(payload.get("parentConversationId") or "").strip()
    root_id = str(payload.get("rootConversationId") or "").strip()
    agent_name = str(payload.get("agentName") or "").strip().lower()

    if parent_id:
        return True
    if root_id and conv_id and root_id != conv_id:
        return True
    if agent_name in ("research", "image-generator"):
        return True
    return False


def parse_transcript(transcript_path):
    """Parse transcript.jsonl and return list of step dicts."""
    steps = []
    if not transcript_path or not os.path.isfile(transcript_path):
        return steps
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        steps.append(obj)
                except Exception:
                    continue
    except Exception as e:
        _log(f"parse_transcript error ({transcript_path}): {e}")
    return steps


def load_hud_snapshot(conv_id):
    """Load the HUD statusline cache for `conv_id` (or fallback to `last_stdin.json`)."""
    safe = _safe_id(conv_id)
    candidates = []
    if safe:
        candidates.append(os.path.join(HUD_SESSIONS_DIR, f"{safe}.json"))
    candidates.append(HUD_LAST_STDIN)

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            snap_conv = str(data.get("conversation_id") or data.get("session_id") or "").strip()
            if conv_id and snap_conv and snap_conv != conv_id:
                continue
            return data
        except Exception:
            continue
    return None


def mark_hud_snapshot_compacted(conv_id, compacted_step_idx):
    """Reset cached HUD percentage right after injecting a checkpoint step."""
    safe = _safe_id(conv_id)
    paths = []
    if safe:
        paths.append(os.path.join(HUD_SESSIONS_DIR, f"{safe}.json"))
    paths.append(HUD_LAST_STDIN)

    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            snap_conv = str(data.get("conversation_id") or data.get("session_id") or "").strip()
            if conv_id and snap_conv and snap_conv != conv_id:
                continue
            ctx = data.get("context_window")
            if isinstance(ctx, dict):
                ctx["used_percentage"] = 2.0
                ctx["remaining_percentage"] = 98.0
                ctx["total_input_tokens"] = 20_000
                ctx["_compacted_at_step"] = compacted_step_idx
            _atomic_write_json(path, data)
        except Exception:
            continue


def get_context_usage_info(conv_id, steps, state):
    """
    Determine `(used_pct, latest_step_idx, last_checkpoint_step_idx)` for `conv_id`.
    Combines HUD `context_window.used_percentage` with `transcript.jsonl` token counts,
    respecting any checkpoint compaction that occurred at `last_checkpoint_step_idx`.
    """
    latest_step_idx = -1
    transcript_checkpoint_idx = -1
    for s in steps:
        idx = int(s.get("step_index", -1))
        if idx > latest_step_idx:
            latest_step_idx = idx
        stype = str(s.get("type") or "").upper()
        if "CHECKPOINT" in stype or "COMPACT" in stype:
            if idx > transcript_checkpoint_idx:
                transcript_checkpoint_idx = idx

    state_compacted_idx = int(state.get("last_compacted_step_idx", -1))
    last_checkpoint_step_idx = max(transcript_checkpoint_idx, state_compacted_idx)

    latest_planner = None
    for s in reversed(steps):
        idx = int(s.get("step_index", -1))
        if idx <= last_checkpoint_step_idx:
            break
        if s.get("type") == "PLANNER_RESPONSE":
            latest_planner = s
            break

    if latest_planner is None and last_checkpoint_step_idx >= 0:
        return (2.0, latest_step_idx, last_checkpoint_step_idx)

    hud = load_hud_snapshot(conv_id)
    win_size = DEFAULT_CONTEXT_WINDOW_SIZE
    hud_used_pct = None
    hud_cur_tokens = 0

    if isinstance(hud, dict):
        ctx = hud.get("context_window") or {}
        if isinstance(ctx, dict):
            w = ctx.get("context_window_size")
            if isinstance(w, (int, float)) and w > 0:
                win_size = int(w)
            compacted_marker = int(ctx.get("_compacted_at_step", -1))
            if compacted_marker < last_checkpoint_step_idx:
                hud_used_pct = None
            else:
                up = ctx.get("used_percentage")
                if isinstance(up, (int, float)):
                    hud_used_pct = float(up)
                cur_u = ctx.get("current_usage") or {}
                if isinstance(cur_u, dict):
                    hud_cur_tokens = (
                        int(cur_u.get("input_tokens", 0) or 0)
                        + int(cur_u.get("cache_read_input_tokens", 0) or 0)
                        + int(cur_u.get("output_tokens", 0) or 0)
                    )

    planner_tokens = 0
    if latest_planner is not None:
        planner_tokens = (
            int(latest_planner.get("input_tokens", 0) or 0)
            + int(latest_planner.get("cache_read_tokens", 0) or 0)
            + int(latest_planner.get("output_tokens", 0) or 0)
        )

    if hud_used_pct is not None:
        delta_pct = 0.0
        if planner_tokens > hud_cur_tokens > 0 and win_size > 0:
            delta_pct = ((planner_tokens - hud_cur_tokens) / win_size) * 100.0
        used_pct = hud_used_pct + max(0.0, delta_pct)
    elif planner_tokens > 0 and win_size > 0:
        traj_tokens = max(0, planner_tokens - SYSTEM_OVERHEAD_TOKENS_ESTIMATE)
        used_pct = (traj_tokens / win_size) * 100.0
    else:
        used_pct = 0.0

    return (used_pct, latest_step_idx, last_checkpoint_step_idx)


def analyze_wrap_progress(steps, since_step_idx, model_output=""):
    """
    Inspect steps after `since_step_idx` to determine whether `session-wrap` (or
    `compact-only`) was executed and whether its interactive flow has completed.
    """
    report_step_idx = -1
    ask_question_step_idx = -1
    user_input_after_report_idx = -1
    last_subagent_step_idx = -1
    has_wrap_report = False

    for s in steps:
        idx = int(s.get("step_index", -1))
        if idx <= since_step_idx:
            continue
        stype = s.get("type", "")
        if stype == "USER_INPUT":
            if has_wrap_report:
                user_input_after_report_idx = idx
        elif stype == "PLANNER_RESPONSE":
            content = str(s.get("content") or "")
            if (
                "Session Wrap Analysis Results" in content
                or ("### 1." in content and "Documentation Updates" in content)
            ):
                has_wrap_report = True
                if report_step_idx < 0:
                    report_step_idx = idx
            for tc in s.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tname = tc.get("name", "")
                if tname in ("ask_question", "AskUserQuestion"):
                    ask_question_step_idx = idx
                elif tname == "invoke_subagent":
                    last_subagent_step_idx = idx

    last_step = steps[-1] if steps else {}
    last_step_idx = int(last_step.get("step_index", -1))
    is_final_planner_step = (
        last_step.get("type") == "PLANNER_RESPONSE"
        and not last_step.get("tool_calls")
    )
    last_content = str(last_step.get("content") or "") + "\n" + str(model_output or "")
    has_complete_marker = "SESSION_WRAP_COMPLETE" in last_content
    has_compact_only_marker = "COMPACT_ONLY_COMPLETE" in last_content

    answered_ask_after_wrap = (
        (has_wrap_report or last_subagent_step_idx > since_step_idx or has_complete_marker)
        and ask_question_step_idx > since_step_idx
        and last_step_idx > ask_question_step_idx
        and last_subagent_step_idx < ask_question_step_idx
    )
    answered_text_after_report = (
        has_wrap_report
        and user_input_after_report_idx > report_step_idx
        and last_step_idx > user_input_after_report_idx
        and last_subagent_step_idx < user_input_after_report_idx
    )
    explicit_marker_completed = (
        has_complete_marker
        and (has_wrap_report or ask_question_step_idx > since_step_idx)
    )

    wrap_completed = is_final_planner_step and (
        answered_ask_after_wrap
        or answered_text_after_report
        or explicit_marker_completed
        or has_compact_only_marker
    )

    return {
        "is_final_planner_step": is_final_planner_step,
        "last_step_idx": last_step_idx,
        "report_step_idx": report_step_idx,
        "ask_question_step_idx": ask_question_step_idx,
        "user_input_after_report_idx": user_input_after_report_idx,
        "last_subagent_step_idx": last_subagent_step_idx,
        "has_wrap_report": has_wrap_report,
        "has_complete_marker": has_complete_marker,
        "has_compact_only_marker": has_compact_only_marker,
        "wrap_completed": wrap_completed,
    }


def _extract_user_request_text(raw_content):
    if not isinstance(raw_content, str):
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw_content, flags=re.DOTALL)
    text = m.group(1).strip() if m else raw_content.strip()
    return text


def build_checkpoint_session_summary(steps, last_checkpoint_step_idx, since_step_idx):
    """
    Synthesize a rich, structured `sessionSummary` for `CortexStepCheckpoint` from:
    1. User requests since the last checkpoint
    2. Files modified/created and key commands executed
    3. The `## Session Wrap Analysis Results` report produced during `session-wrap`
    4. The final wrap-up actions executed in Step 6
    """
    user_requests = []
    edited_files = []
    commands_run = []
    wrap_report_text = ""
    final_wrap_response = ""

    for s in steps:
        idx = int(s.get("step_index", -1))
        if idx <= last_checkpoint_step_idx:
            continue
        stype = s.get("type", "")
        if stype == "USER_INPUT":
            req = _extract_user_request_text(s.get("content", ""))
            if req:
                short_req = req if len(req) <= 400 else req[:400] + "..."
                user_requests.append(f"- [Step {idx}] {short_req}")
        elif stype == "PLANNER_RESPONSE":
            content = str(s.get("content") or "").strip()
            cleaned_content = (
                content.replace("<!-- SESSION_WRAP_COMPLETE -->", "")
                .replace("<!-- COMPACT_ONLY_COMPLETE -->", "")
                .strip()
            )
            if idx > since_step_idx and cleaned_content:
                if (
                    "Session Wrap Analysis Results" in cleaned_content
                    or "### 1." in cleaned_content
                    or "Documentation Updates" in cleaned_content
                ):
                    wrap_report_text = cleaned_content
                elif not s.get("tool_calls"):
                    final_wrap_response = cleaned_content

            for tc in s.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tname = tc.get("name", "")
                targs = tc.get("args") or {}
                if not isinstance(targs, dict):
                    continue
                if tname in (
                    "write_file",
                    "edit_file",
                    "write_to_file",
                    "replace_file_content",
                    "multi_replace_file_content",
                ):
                    p = str(targs.get("Path") or targs.get("TargetFile") or "").strip('"')
                    if p and p not in edited_files:
                        edited_files.append(p)
                elif tname == "run_command":
                    cmd = str(targs.get("CommandLine") or "").strip('"').strip()
                    if cmd:
                        short_cmd = cmd if len(cmd) <= 140 else cmd[:140] + "..."
                        commands_run.append(short_cmd)

    sections = [
        "# Pre-Compact Session Wrap & Context Summary",
        "",
        "## 1. User Requests in Session",
    ]
    if user_requests:
        sections.extend(user_requests[-15:])
    else:
        sections.append("- (No explicit user requests recorded)")

    if edited_files:
        sections.extend([
            "",
            "## 2. Modified / Created Files",
            *[f"- `{p}`" for p in edited_files[-25:]],
        ])

    if commands_run:
        sections.extend([
            "",
            "## 3. Recent Commands Executed",
            *[f"- `{c}`" for c in commands_run[-12:]],
        ])

    if wrap_report_text:
        sections.extend([
            "",
            "## 4. Session Wrap Analysis Report",
            wrap_report_text[:6000],
        ])

    if final_wrap_response and final_wrap_response != wrap_report_text:
        sections.extend([
            "",
            "## 5. Executed Wrap-Up Actions & Final State",
            final_wrap_response[:3000],
        ])

    return "\n".join(sections).strip()


def _emit_compaction_checkpoint(
    conv_id,
    state,
    steps,
    latest_step_idx,
    last_checkpoint_step_idx,
    since_step_idx,
    trigger_pct,
):
    session_summary = build_checkpoint_session_summary(
        steps,
        last_checkpoint_step_idx=last_checkpoint_step_idx,
        since_step_idx=since_step_idx,
    )
    compacted_step_marker = latest_step_idx + 5
    state["status"] = "idle"
    state["last_suggested_tier"] = 0.0
    state["last_compacted_step_idx"] = compacted_step_marker
    state["last_compacted_at"] = time.time()
    state["compaction_count"] = int(state.get("compaction_count", 0)) + 1
    state["cooldown_until_step"] = compacted_step_marker + 4
    save_conv_state(conv_id, state)

    mark_hud_snapshot_compacted(conv_id, compacted_step_marker)

    _log(
        f"[PostInvocation] Injected checkpoint compaction after session-wrap "
        f"(trigger_pct={trigger_pct}%, step={latest_step_idx}, conv={conv_id})"
    )

    output = {
        "injectSteps": [
            {
                "checkpoint": {
                    "sessionSummary": session_summary,
                }
            },
            {
                "systemMessage": {
                    "systemMessage": "",
                    "metadata": {
                        "user_markdown": (
                            f"\n> 🗜️ **[Pre-Compact Wrap & 컴팩션 완료]** "
                            f"세션 정리 반영 후 컨텍스트 컴팩션(Checkpoint)이 "
                            f"자동 실행되었습니다 (트리거 시점: `{trigger_pct}%`).\n"
                        ),
                    },
                }
            },
        ]
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


def handle_post_invocation(payload):
    if is_subagent_or_internal(payload):
        sys.stdout.write("{}")
        return

    cfg = load_config()
    if not cfg.get("enabled", True):
        sys.stdout.write("{}")
        return

    conv_id = str(payload.get("conversationId") or "").strip()
    transcript_path = str(payload.get("transcriptPath") or "").strip()
    model_output = str(payload.get("modelOutput") or "")

    state = load_conv_state(conv_id)
    steps = parse_transcript(transcript_path)
    if not steps:
        sys.stdout.write("{}")
        return

    last_step = steps[-1]
    is_final_planner_step = (
        last_step.get("type") == "PLANNER_RESPONSE"
        and not last_step.get("tool_calls")
    )
    if not is_final_planner_step:
        sys.stdout.write("{}")
        return

    used_pct, latest_step_idx, last_checkpoint_step_idx = get_context_usage_info(
        conv_id, steps, state
    )
    suggest_thresholds = cfg.get("suggestThresholdsPct", DEFAULT_SUGGEST_THRESHOLDS)
    force_pct = float(cfg.get("forceThresholdPct", DEFAULT_FORCE_THRESHOLD_PCT))
    status = state.get("status", "idle")
    first_suggest_step_idx = int(
        state.get("first_suggest_step_idx", state.get("trigger_step_idx", -1))
    )
    trigger_step_idx = int(state.get("trigger_step_idx", -1))
    last_suggested_tier = float(state.get("last_suggested_tier", 0.0))

    # If a built-in or manual compaction happened after our suggestion, reset state to idle
    if status in ("suggested", "wrapping_forced") and last_checkpoint_step_idx > first_suggest_step_idx:
        state["status"] = "idle"
        state["last_suggested_tier"] = 0.0
        save_conv_state(conv_id, state)
        status = "idle"
        last_suggested_tier = 0.0

    # 1. If in "suggested" or "wrapping_forced", check if `session-wrap` just completed!
    if status in ("suggested", "wrapping_forced"):
        check_since = trigger_step_idx if status == "wrapping_forced" else first_suggest_step_idx
        if check_since >= 0:
            progress = analyze_wrap_progress(steps, check_since, model_output=model_output)
            if progress["wrap_completed"]:
                trigger_pct = state.get("trigger_pct", round(used_pct, 1))
                _emit_compaction_checkpoint(
                    conv_id=conv_id,
                    state=state,
                    steps=steps,
                    latest_step_idx=latest_step_idx,
                    last_checkpoint_step_idx=last_checkpoint_step_idx,
                    since_step_idx=check_since,
                    trigger_pct=trigger_pct,
                )
                return

    # If currently in 90% forced wrap and not yet completed, let Stop hook handle continuation
    if status == "wrapping_forced" or used_pct >= force_pct:
        sys.stdout.write("{}")
        return

    # 2. Check if context usage has crossed a suggestion tier (60% or 70%)
    cooldown_until = int(state.get("cooldown_until_step", -1))
    if latest_step_idx <= cooldown_until or latest_step_idx <= last_checkpoint_step_idx:
        sys.stdout.write("{}")
        return

    crossed_tiers = [t for t in suggest_thresholds if used_pct >= t]
    if not crossed_tiers:
        sys.stdout.write("{}")
        return

    current_tier = max(crossed_tiers)
    if current_tier <= last_suggested_tier:
        # User already declined at this tier; do neither wrap nor compact until next tier!
        sys.stdout.write("{}")
        return

    # If user already manually ran and completed session-wrap on this very turn at >= 60%, compact directly
    recent_start = max(last_checkpoint_step_idx, latest_step_idx - 20)
    manual_progress = analyze_wrap_progress(steps, recent_start, model_output=model_output)
    if manual_progress["wrap_completed"]:
        _emit_compaction_checkpoint(
            conv_id=conv_id,
            state=state,
            steps=steps,
            latest_step_idx=latest_step_idx,
            last_checkpoint_step_idx=last_checkpoint_step_idx,
            since_step_idx=recent_start,
            trigger_pct=round(used_pct, 1),
        )
        return

    # Record the new suggestion tier (60% -> 70%)
    if status == "idle" or first_suggest_step_idx < 0:
        state["first_suggest_step_idx"] = latest_step_idx
    state["status"] = "suggested"
    state["trigger_step_idx"] = latest_step_idx
    state["trigger_pct"] = round(used_pct, 1)
    state["last_suggested_tier"] = current_tier
    state["remind_count"] = 0
    state["triggered_at"] = time.time()
    save_conv_state(conv_id, state)

    higher_suggest_tiers = [t for t in suggest_thresholds if t > current_tier]
    if higher_suggest_tiers:
        next_tier_note = f"re-asks at {higher_suggest_tiers[0]:.0f}% if declined"
    else:
        next_tier_note = f"auto-runs wrap & compact at {force_pct:.0f}% if declined"

    is_first_tier = (current_tier == suggest_thresholds[0])
    tier_label = "1st Suggestion" if is_first_tier else "2nd Suggestion"

    model_msg = WRAP_SUGGESTION_MODEL_MSG_TEMPLATE.format(
        tier_label=tier_label,
        tier_pct=current_tier,
        used_pct=used_pct,
        next_tier_note=next_tier_note,
    )
    if is_first_tier:
        user_md = WRAP_SUGGESTION_USER_MARKDOWN_60.format(
            used_pct=used_pct,
        )
    else:
        user_md = WRAP_SUGGESTION_USER_MARKDOWN_70.format(
            used_pct=used_pct,
            tier_pct=current_tier,
            force_pct=force_pct,
        )

    _log(
        f"[PostInvocation] Displayed {current_tier:.0f}% session-wrap suggestion at {used_pct:.1f}% "
        f"(step={latest_step_idx}, conv={conv_id})"
    )
    output = {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": model_msg,
                    "metadata": {
                        "user_markdown": user_md,
                    },
                }
            }
        ]
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


def handle_stop(payload):
    if is_subagent_or_internal(payload):
        sys.stdout.write("{}")
        return

    cfg = load_config()
    if not cfg.get("enabled", True):
        sys.stdout.write("{}")
        return

    conv_id = str(payload.get("conversationId") or "").strip()
    transcript_path = str(payload.get("transcriptPath") or "").strip()
    term_reason = str(payload.get("terminationReason") or "").strip()
    fully_idle = bool(payload.get("fullyIdle", True))

    state = load_conv_state(conv_id)
    steps = parse_transcript(transcript_path)
    used_pct, latest_step_idx, last_checkpoint_step_idx = get_context_usage_info(
        conv_id, steps, state
    )
    force_pct = float(cfg.get("forceThresholdPct", DEFAULT_FORCE_THRESHOLD_PCT))
    status = state.get("status", "idle")
    first_suggest_step_idx = int(
        state.get("first_suggest_step_idx", state.get("trigger_step_idx", -1))
    )
    trigger_step_idx = int(state.get("trigger_step_idx", -1))

    # 1. If user canceled or error occurred during forced wrap, reset cleanly
    if term_reason in ("USER_CANCELED", "ERROR"):
        if status == "wrapping_forced":
            state["status"] = "suggested"
            state["cooldown_until_step"] = latest_step_idx + 6
            save_conv_state(conv_id, state)
            _log(f"[Stop] Reset wrapping_forced on {term_reason} (conv={conv_id})")
        sys.stdout.write("{}")
        return

    # 2. Do not interrupt while subagents or background tasks are running
    if not fully_idle or term_reason != "NO_TOOL_CALL":
        sys.stdout.write("{}")
        return

    # 3. If a checkpoint occurred after our suggestion/trigger, reset to idle
    if status in ("suggested", "wrapping_forced") and last_checkpoint_step_idx > max(first_suggest_step_idx, trigger_step_idx) >= 0:
        state["status"] = "idle"
        state["last_suggested_tier"] = 0.0
        save_conv_state(conv_id, state)
        status = "idle"

    # 4. If currently in 90% forced wrap ("wrapping_forced"), handle completion / reminder
    if status == "wrapping_forced":
        if (latest_step_idx - trigger_step_idx) > MAX_WRAP_STEPS:
            state["status"] = "idle"
            state["cooldown_until_step"] = latest_step_idx + 6
            save_conv_state(conv_id, state)
            sys.stdout.write("{}")
            return

        progress = analyze_wrap_progress(steps, trigger_step_idx)
        # If the agent printed the report in plain text without calling `ask_question`,
        # allow stop so the user can reply in chat.
        if progress["has_wrap_report"] and progress["ask_question_step_idx"] < 0:
            sys.stdout.write("{}")
            return

        remind_count = int(state.get("remind_count", 0))
        if not progress["has_wrap_report"] and progress["ask_question_step_idx"] < 0 and remind_count < 1:
            state["remind_count"] = remind_count + 1
            save_conv_state(conv_id, state)
            _log(f"[Stop] Reminding agent to complete 90% forced session-wrap (conv={conv_id})")
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "continue",
                        "reason": WRAP_REMINDER_PROMPT,
                    },
                    ensure_ascii=False,
                )
            )
            return

        sys.stdout.write("{}")
        return

    # 5. If in "suggested" state and user accepted (subagents already started), remind once if stopped early
    if status == "suggested" and trigger_step_idx >= 0:
        progress = analyze_wrap_progress(steps, trigger_step_idx)
        remind_count = int(state.get("remind_count", 0))
        if (
            progress["last_subagent_step_idx"] > trigger_step_idx
            and not progress["has_wrap_report"]
            and progress["ask_question_step_idx"] < 0
            and remind_count < 1
        ):
            state["remind_count"] = remind_count + 1
            save_conv_state(conv_id, state)
            _log(f"[Stop] Reminding agent to finish user-accepted session-wrap (conv={conv_id})")
            sys.stdout.write(
                json.dumps(
                    {
                        "decision": "continue",
                        "reason": WRAP_REMINDER_PROMPT,
                    },
                    ensure_ascii=False,
                )
            )
            return

    # 6. 90% Forced Auto-Wrap & Auto-Compact trigger!
    cooldown_until = int(state.get("cooldown_until_step", -1))
    if latest_step_idx <= cooldown_until or latest_step_idx <= last_checkpoint_step_idx:
        sys.stdout.write("{}")
        return

    if used_pct >= force_pct:
        if status == "idle" or first_suggest_step_idx < 0:
            state["first_suggest_step_idx"] = latest_step_idx
        state["status"] = "wrapping_forced"
        state["trigger_step_idx"] = latest_step_idx
        state["trigger_pct"] = round(used_pct, 1)
        state["last_suggested_tier"] = force_pct
        state["remind_count"] = 0
        state["triggered_at"] = time.time()
        save_conv_state(conv_id, state)

        prompt = FORCE_WRAP_90_PROMPT_TEMPLATE.format(
            used_pct=used_pct,
            force_pct=force_pct,
        )
        _log(
            f"[Stop] Triggered 90% Forced Auto-Wrap at {used_pct:.1f}% "
            f"(force_pct={force_pct:.1f}%, step={latest_step_idx}, conv={conv_id})"
        )
        sys.stdout.write(
            json.dumps(
                {
                    "decision": "continue",
                    "reason": prompt,
                },
                ensure_ascii=False,
            )
        )
        return

    sys.stdout.write("{}")


def handle_cli_status(args):
    cfg = load_config()
    if args.enable:
        cfg["enabled"] = True
        _atomic_write_json(CONFIG_PATH, cfg)
    elif args.disable:
        cfg["enabled"] = False
        _atomic_write_json(CONFIG_PATH, cfg)
    if args.force_threshold is not None:
        cfg["forceThresholdPct"] = float(args.force_threshold)
        _atomic_write_json(CONFIG_PATH, cfg)

    print(json.dumps(cfg, indent=2, ensure_ascii=False))


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) > 1 and sys.argv[1] in ("status", "config"):
        parser = argparse.ArgumentParser(description="Configure pre-compact session-wrap hook")
        parser.add_argument("cmd")
        parser.add_argument("--enable", action="store_true")
        parser.add_argument("--disable", action="store_true")
        parser.add_argument("--force-threshold", type=float, default=None)
        args = parser.parse_args()
        handle_cli_status(args)
        return

    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    try:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
    except Exception as e:
        _log(f"stdin parse error ({mode}): {e}")
        sys.stdout.write("{}")
        return

    try:
        if mode == "post-invocation":
            handle_post_invocation(payload)
        else:
            handle_stop(payload)
    except Exception as e:
        _log(f"unhandled exception in {mode}: {e}")
        sys.stdout.write("{}")


if __name__ == "__main__":
    main()
