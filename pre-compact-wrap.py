#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607, TRY300
"""
Antigravity (agy) Pre-Compact Session Wrap & Auto-Compaction Hook.

Workflow (Option 1 — Interactive Session Wrap before Auto-Compact):
1. Monitors live context window usage (`used_percentage`) per conversation via the
   HUD statusline cache (`~/.gemini/antigravity-cli/hud/cache/sessions/<conv_id>.json`
   and `last_stdin.json`) cross-checked with `transcript.jsonl`.
2. When context usage reaches >= 50% (configurable via `AGY_AUTO_WRAP_THRESHOLD` or
   `config.json`), intercepts the `Stop` hook (`fullyIdle == true`) and instructs the
   agent to execute the interactive `session-wrap` skill (4 parallel analysis subagents
   -> duplicate check -> Markdown report -> `ask_question` multi-select -> execute
   selected actions).
3. Meanwhile, `AGY_CLI_AUTO_COMPACT_OVERRIDE="60%"` provides headroom so `agy`'s
   built-in hard auto-compact does not prematurely truncate context while `session-wrap`
   is running.
4. Once the user answers `ask_question` and the agent finishes executing the selected
   wrap-up actions (`PostInvocation` hook with no remaining tool calls), immediately
   injects a native `CortexStepCheckpoint` (`{"checkpoint": {"sessionSummary": "..."}}`)
   to compact the context window on the spot without waiting for 60%.
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

DEFAULT_WRAP_THRESHOLD_PCT = 50.0
DEFAULT_CONTEXT_WINDOW_SIZE = 1_048_576
SYSTEM_OVERHEAD_TOKENS_ESTIMATE = 45_000
MAX_WRAP_STEPS = 35

WRAP_TRIGGER_PROMPT_TEMPLATE = """\
[Auto Pre-Compact Session Wrap] 현재 컨텍스트 윈도우 사용량이 {used_pct:.1f}% (임계값 {threshold_pct:.0f}%)에 도달했습니다.
컨텍스트 자동 컴팩션(Auto-Compaction)을 실행하기 전에, 지금까지의 세션 작업 내역이 유실되지 않도록 반드시 `session-wrap` 스킬 워크플로우를 지금 바로 진행해 주세요:

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
        "thresholdPct": DEFAULT_WRAP_THRESHOLD_PCT,
    }
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception as e:
        _log(f"load_config error: {e}")

    env_thresh = os.environ.get("AGY_AUTO_WRAP_THRESHOLD", "").strip().rstrip("%").strip()
    if env_thresh:
        try:
            val = float(env_thresh)
            if 5.0 <= val <= 95.0:
                cfg["thresholdPct"] = val
        except ValueError:
            pass
    return cfg


def load_conv_state(conv_id):
    safe = _safe_id(conv_id)
    if not safe:
        return {"status": "idle"}
    path = os.path.join(STATE_DIR, f"{safe}.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        _log(f"load_conv_state error ({conv_id}): {e}")
    return {"status": "idle"}


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
    """Parse transcript.jsonl and return list of step dicts sorted by step_index."""
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

    # Find latest PLANNER_RESPONSE after the last checkpoint
    latest_planner = None
    for s in reversed(steps):
        idx = int(s.get("step_index", -1))
        if idx <= last_checkpoint_step_idx:
            break
        if s.get("type") == "PLANNER_RESPONSE":
            latest_planner = s
            break

    # If no model response has occurred since the last compaction, context is freshly compacted
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
                # HUD cache predates the latest checkpoint
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
        # If the conversation grew significantly since the HUD snapshot was written,
        # add the positive token delta so we catch crossing 50% on the exact turn.
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


def analyze_wrap_progress(steps, trigger_step_idx, model_output=""):
    """
    Inspect steps after `trigger_step_idx` to determine the progress of `session-wrap`.
    Returns a dict with:
      - `is_final_planner_step`: True if the last step is PLANNER_RESPONSE with no tool_calls
      - `ask_question_step_idx`: step_index where `ask_question` was called (> trigger_step_idx), or -1
      - `user_input_step_idx`: step_index where a new USER_INPUT arrived (> trigger_step_idx), or -1
      - `last_subagent_step_idx`: step_index where `invoke_subagent` was last called, or -1
      - `has_wrap_report`: True if `Session Wrap Analysis Results` or equivalent appeared
      - `has_complete_marker`: True if `SESSION_WRAP_COMPLETE` appeared in the latest response
      - `wrap_completed`: True if the interactive session-wrap + user action execution is finished
    """
    ask_question_step_idx = -1
    user_input_step_idx = -1
    last_subagent_step_idx = -1
    has_wrap_report = False

    for s in steps:
        idx = int(s.get("step_index", -1))
        if idx <= trigger_step_idx:
            continue
        stype = s.get("type", "")
        if stype == "USER_INPUT":
            user_input_step_idx = idx
        elif stype == "PLANNER_RESPONSE":
            content = str(s.get("content") or "")
            if (
                "Session Wrap Analysis Results" in content
                or "세션 마무리" in content
                or "Documentation Updates" in content
            ):
                has_wrap_report = True
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

    # Interactive completion criteria:
    # 1. Must be at the end of a model response with no pending tool calls in this step.
    # 2. Either:
    #    a) `ask_question` was called during wrap AND answered (`last_step_idx > ask_question_step_idx`),
    #       and no new subagent was spawned after `ask_question`; OR
    #    b) The user sent a direct follow-up `USER_INPUT` during wrap (`last_step_idx > user_input_step_idx`)
    #       and no new subagent was spawned after `user_input_step_idx`; OR
    #    c) Explicit `SESSION_WRAP_COMPLETE` marker is present after the wrap report/question.
    answered_ask = (
        ask_question_step_idx > trigger_step_idx
        and last_step_idx > ask_question_step_idx
        and last_subagent_step_idx < ask_question_step_idx
    )
    answered_user_input = (
        user_input_step_idx > trigger_step_idx
        and last_step_idx > user_input_step_idx
        and last_subagent_step_idx < user_input_step_idx
    )
    marker_after_interaction = has_complete_marker and (
        answered_ask or answered_user_input or has_wrap_report
    )

    wrap_completed = is_final_planner_step and (
        answered_ask or answered_user_input or marker_after_interaction
    )

    return {
        "is_final_planner_step": is_final_planner_step,
        "last_step_idx": last_step_idx,
        "ask_question_step_idx": ask_question_step_idx,
        "user_input_step_idx": user_input_step_idx,
        "last_subagent_step_idx": last_subagent_step_idx,
        "has_wrap_report": has_wrap_report,
        "has_complete_marker": has_complete_marker,
        "wrap_completed": wrap_completed,
    }


def _extract_user_request_text(raw_content):
    if not isinstance(raw_content, str):
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw_content, flags=re.DOTALL)
    text = m.group(1).strip() if m else raw_content.strip()
    return text


def build_checkpoint_session_summary(steps, last_checkpoint_step_idx, trigger_step_idx):
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
            cleaned_content = content.replace("<!-- SESSION_WRAP_COMPLETE -->", "").strip()
            if idx > trigger_step_idx and cleaned_content:
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

    # 1. If user canceled or an error terminated the loop during wrap, reset cleanly
    if term_reason in ("USER_CANCELED", "ERROR"):
        if state.get("status") == "wrapping":
            state["status"] = "idle"
            state["cooldown_until_step"] = latest_step_idx + 6
            save_conv_state(conv_id, state)
            _log(f"[Stop] Reset wrapping state on {term_reason} (conv={conv_id})")
        sys.stdout.write("{}")
        return

    # 2. Do not interrupt while background tasks or subagents are still active
    if not fully_idle or term_reason != "NO_TOOL_CALL":
        sys.stdout.write("{}")
        return

    threshold_pct = float(cfg.get("thresholdPct", DEFAULT_WRAP_THRESHOLD_PCT))
    status = state.get("status", "idle")

    # 3. If currently in "wrapping" state, check why Stop was reached
    if status == "wrapping":
        trigger_step_idx = int(state.get("trigger_step_idx", -1))
        progress = analyze_wrap_progress(steps, trigger_step_idx)

        # Safety bound: if too many steps elapsed or checkpoint already occurred after trigger
        if (
            last_checkpoint_step_idx > trigger_step_idx
            or (latest_step_idx - trigger_step_idx) > MAX_WRAP_STEPS
        ):
            state["status"] = "idle"
            state["cooldown_until_step"] = latest_step_idx + 6
            save_conv_state(conv_id, state)
            _log(f"[Stop] Wrap expired or already compacted (conv={conv_id})")
            sys.stdout.write("{}")
            return

        # If the agent printed the report in plain text without calling `ask_question`,
        # let it stop so the user can reply in the next turn.
        if progress["has_wrap_report"] and progress["ask_question_step_idx"] < 0:
            _log(f"[Stop] Wrap report printed without ask_question; waiting for user reply (conv={conv_id})")
            sys.stdout.write("{}")
            return

        # If the agent stopped before even producing the report/ask_question, remind it once
        remind_count = int(state.get("remind_count", 0))
        if not progress["has_wrap_report"] and progress["ask_question_step_idx"] < 0 and remind_count < 1:
            state["remind_count"] = remind_count + 1
            save_conv_state(conv_id, state)
            _log(f"[Stop] Reminding agent to complete session-wrap (conv={conv_id})")
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

        # Otherwise allow stop
        sys.stdout.write("{}")
        return

    # 4. In "idle" state: check if context usage has reached >= threshold_pct (50%)
    cooldown_until = int(state.get("cooldown_until_step", -1))
    if latest_step_idx <= cooldown_until or latest_step_idx <= last_checkpoint_step_idx:
        sys.stdout.write("{}")
        return

    if used_pct >= threshold_pct:
        # Transition to "wrapping" and instruct the agent to run interactive session-wrap
        state["status"] = "wrapping"
        state["trigger_step_idx"] = latest_step_idx
        state["trigger_pct"] = round(used_pct, 2)
        state["remind_count"] = 0
        state["triggered_at"] = time.time()
        save_conv_state(conv_id, state)

        prompt = WRAP_TRIGGER_PROMPT_TEMPLATE.format(
            used_pct=used_pct,
            threshold_pct=threshold_pct,
        )
        _log(
            f"[Stop] Triggered Pre-Compact Session Wrap at {used_pct:.1f}% "
            f"(threshold={threshold_pct:.1f}%, step={latest_step_idx}, conv={conv_id})"
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


def handle_post_invocation(payload):
    if is_subagent_or_internal(payload):
        sys.stdout.write("{}")
        return

    conv_id = str(payload.get("conversationId") or "").strip()
    state = load_conv_state(conv_id)

    # Fast-path (0ms) when not in an active pre-compact wrap
    if state.get("status") != "wrapping":
        sys.stdout.write("{}")
        return

    transcript_path = str(payload.get("transcriptPath") or "").strip()
    model_output = str(payload.get("modelOutput") or "")
    steps = parse_transcript(transcript_path)
    if not steps:
        sys.stdout.write("{}")
        return

    trigger_step_idx = int(state.get("trigger_step_idx", -1))
    progress = analyze_wrap_progress(steps, trigger_step_idx, model_output=model_output)

    if not progress["wrap_completed"]:
        sys.stdout.write("{}")
        return

    # Interactive session-wrap and user-selected actions are complete!
    # Build the checkpoint summary and immediately inject a CortexStepCheckpoint.
    _, latest_step_idx, last_checkpoint_step_idx = get_context_usage_info(
        conv_id, steps, state
    )
    session_summary = build_checkpoint_session_summary(
        steps,
        last_checkpoint_step_idx=last_checkpoint_step_idx,
        trigger_step_idx=trigger_step_idx,
    )

    compacted_step_marker = max(latest_step_idx, progress["last_step_idx"]) + 5
    state["status"] = "idle"
    state["last_compacted_step_idx"] = compacted_step_marker
    state["last_compacted_at"] = time.time()
    state["compaction_count"] = int(state.get("compaction_count", 0)) + 1
    state["cooldown_until_step"] = compacted_step_marker + 4
    save_conv_state(conv_id, state)

    mark_hud_snapshot_compacted(conv_id, compacted_step_marker)

    trigger_pct = state.get("trigger_pct", DEFAULT_WRAP_THRESHOLD_PCT)
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
                            f"\n> 🗜️ **[Auto Pre-Compact Wrap 완료]** "
                            f"`session-wrap` 마무리 작업 반영 후 컨텍스트 컴팩션(Checkpoint)이 "
                            f"자동 실행되었습니다 (트리거 시점: `{trigger_pct}%`).\n"
                        ),
                    },
                }
            },
        ]
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()


def handle_cli_status(args):
    cfg = load_config()
    if args.enable:
        cfg["enabled"] = True
        _atomic_write_json(CONFIG_PATH, cfg)
    elif args.disable:
        cfg["enabled"] = False
        _atomic_write_json(CONFIG_PATH, cfg)
    if args.threshold is not None:
        cfg["thresholdPct"] = float(args.threshold)
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
        parser.add_argument("--threshold", type=float, default=None)
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
