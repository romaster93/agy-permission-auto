#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607
"""
Antigravity (agy) Stop-Time Review Gate & Companion powered by Claude Opus 5.5 (`claude-opus-5-5` in `agy`).

- Disabled by default (`stopReviewGate: false`). Toggle per-workspace or globally via:
    python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --enable-review-gate
    python3 ~/.gemini/config/plugins/opus-review-gate/opus-review-gate.py setup --disable-review-gate
- When enabled, runs at the `Stop` hook lifecycle event:
  - Fast-paths (0ms) if the turn did not execute any file edits or shell commands
    by inspecting `transcriptPath` (`transcript.jsonl`).
  - If code changes occurred in the turn, collects the turn's edited files & git diff
    and invokes `agy --model claude-opus-5-5 -p` with the stop-review-gate rubric.
  - On `ALLOW:`, returns `{}` so `agy` finishes the turn normally.
  - On `BLOCK: <한국어 사유>`, returns `{"decision": "continue", "reason": "..."}` so
    `agy` blocks stopping and immediately re-enters the loop to fix the issue.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

STATE_ROOT = os.path.expanduser("~/.gemini/antigravity-cli/opus-review-gate")
GLOBAL_CONFIG_PATH = os.path.join(STATE_ROOT, "global-config.json")
WORKSPACES_DIR = os.path.join(STATE_ROOT, "workspaces")
LOG_PATH = os.path.expanduser("~/.local/share/agy-opus-review-gate.log")

DEFAULT_OPUS_MODEL = "claude-opus-5-5"

EDIT_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

STOP_REVIEW_PROMPT_TEMPLATE = """\
<task>
Run a strict stop-gate review of the previous agent turn using Claude Opus 5.5.
Only review the work from the previous turn.
Only review it if the agent actually did code or configuration changes in that turn.
Pure status, setup, or reporting output does not count as reviewable work.
If the previous turn was only a status update, a summary, a setup check, or a command that did not make direct edits in that turn, return ALLOW immediately.
Challenge whether that specific work and its design choices should ship.

{agent_response_block}

<turn_actions>
{turn_actions_block}
</turn_actions>

<repository_git_context>
{git_context_block}
</repository_git_context>
</task>

<compact_output_contract>
Return a compact final answer.
Your first line must be exactly one of:
- ALLOW: <한국어로 작성된 짧은 승인 사유>
- BLOCK: <턴 종료 전 반드시 수정해야 할 구체적인 문제점과 이유 (한국어로 상세히 작성)>
Do not put anything before that first line.
IMPORTANT: Always write the reason after `ALLOW:` or `BLOCK:` in Korean (한국어).
</compact_output_contract>

<default_follow_through_policy>
Use ALLOW if the previous turn did not make code changes or if you do not see a concrete blocking issue.
Use BLOCK only if the previous turn made code changes and you found a real bug, broken invariant, missing edge case, regression, or design flaw that still needs to be fixed before stopping.
</default_follow_through_policy>

<grounding_rules>
Ground every blocking claim in the actual diffs, files, or tool outputs shown above (or inspected in the workspace).
Do not block based on older unrelated edits from earlier turns when the immediately previous turn did not touch them.
</grounding_rules>

<dig_deeper_nudge>
If the previous turn did make code changes, check for second-order failures, empty-state behavior, retries, stale state, rollback risk, and design tradeoffs before you finalize.
</dig_deeper_nudge>
"""

CODE_REVIEW_PROMPT_TEMPLATE = """\
Run a thorough code review of the changes shown below in `{workspace_root}`.
Focus on bugs, regressions, edge cases, error handling, and maintainability issues.
Write the entire review in Korean (한국어), citing exact file paths and line numbers.

<repository_git_context>
{git_context_block}
</repository_git_context>
"""

ADVERSARIAL_REVIEW_PROMPT_TEMPLATE = """\
Run an adversarial code and design review of the changes shown below in `{workspace_root}`.
Position this as a challenge review that questions the chosen implementation, design choices, tradeoffs, and assumptions — not just a surface lint pass.

User focus: {focus}

Check for:
1. Broken invariants, hidden edge cases, race conditions, or error-handling gaps
2. Stale state, partial failure/rollback risks, and security/sandbox boundary issues
3. Simpler or more principled alternatives to the current design

Write your review report clearly in Korean (한국어), citing specific file paths and line numbers.

<repository_git_context>
{git_context_block}
</repository_git_context>
"""


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _find_agy_bin():
    found = shutil.which("agy")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/agy"),
        os.path.expanduser("~/AppData/Local/agy/bin/agy.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return "agy"


def _resolve_workspace_root(cwd):
    cwd = os.path.abspath(cwd or os.getcwd())
    try:
        res = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            return os.path.realpath(res.stdout.strip())
    except Exception:
        pass
    return os.path.realpath(cwd)


def _workspace_state_file(workspace_root):
    norm = os.path.realpath(workspace_root)
    slug = os.path.basename(norm) or "workspace"
    slug = "".join(c if (c.isalnum() or c in "._-") else "-" for c in slug).strip("-") or "workspace"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return os.path.join(WORKSPACES_DIR, f"{slug}-{digest}.json")


def load_global_config():
    if not os.path.exists(GLOBAL_CONFIG_PATH):
        return {"stopReviewGate": False, "model": DEFAULT_OPUS_MODEL}
    try:
        with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "stopReviewGate": bool(data.get("stopReviewGate", False)),
            "model": str(data.get("model") or DEFAULT_OPUS_MODEL),
        }
    except Exception:
        return {"stopReviewGate": False, "model": DEFAULT_OPUS_MODEL}


def save_global_config(enabled, model=None):
    os.makedirs(STATE_ROOT, exist_ok=True)
    current = load_global_config()
    data = {
        "stopReviewGate": bool(enabled),
        "model": model or current.get("model") or DEFAULT_OPUS_MODEL,
    }
    with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def get_workspace_review_gate(workspace_root):
    """Return (effective_enabled, workspace_override_or_none, global_enabled, model)."""
    global_cfg = load_global_config()
    global_enabled = bool(global_cfg.get("stopReviewGate", False))
    model = str(global_cfg.get("model") or DEFAULT_OPUS_MODEL)
    state_file = _workspace_state_file(workspace_root)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("model"):
                model = str(data["model"])
            if isinstance(data.get("stopReviewGate"), bool):
                ws_val = data["stopReviewGate"]
                return ws_val, ws_val, global_enabled, model
        except Exception:
            pass
    return global_enabled, None, global_enabled, model


def set_workspace_review_gate(workspace_root, enabled, model=None):
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    state_file = _workspace_state_file(workspace_root)
    _, _, _, current_model = get_workspace_review_gate(workspace_root)
    data = {
        "workspaceRoot": workspace_root,
        "stopReviewGate": bool(enabled),
        "model": model or current_model or DEFAULT_OPUS_MODEL,
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def _inspect_antigravity_turn(transcript_path):
    """
    Inspect transcript.jsonl to determine if the latest turn (since last USER_INPUT
    or last Review Gate continuation) executed any file edits or shell commands.
    Returns (has_edits, last_assistant_message, turn_actions_summary).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return True, "", "No transcript available."

    try:
        steps = []
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    steps.append(json.loads(line))
                except Exception:
                    continue

        turn_start_idx = 0
        for i in range(len(steps) - 1, -1, -1):
            step = steps[i]
            content = str(step.get("content") or "")
            if (
                step.get("type") == "USER_INPUT"
                or step.get("source") == "USER_EXPLICIT"
                or "[Opus Review Gate]" in content
                or "[Codex Review Gate]" in content
            ):
                turn_start_idx = i + 1
                break

        has_edits = False
        last_assistant_message = ""
        actions = []

        for step in steps[turn_start_idx:]:
            if step.get("type") == "PLANNER_RESPONSE" and step.get("content"):
                last_assistant_message = str(step["content"])

            tool_calls = step.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                continue
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = str(tc.get("name") or "")
                args = tc.get("args") or {}
                if name in EDIT_TOOLS:
                    has_edits = True
                    target = (
                        args.get("Path")
                        or args.get("TargetFile")
                        or args.get("File")
                        or ""
                    )
                    actions.append(f"- {name}: {target}")
                elif name == "run_command":
                    cmd = str(args.get("CommandLine") or "")
                    if (
                        "opus-review-gate.py" not in cmd
                        and "codex-review-gate.py" not in cmd
                        and "codex-companion.mjs" not in cmd
                    ):
                        has_edits = True
                        actions.append(f"- run_command: {cmd[:300]}")

        turn_actions_summary = "\n".join(actions) if actions else "None"
        return has_edits, last_assistant_message, turn_actions_summary
    except Exception:
        return True, "", "Error reading transcript."


def _collect_git_context(workspace_root, base_ref=None, max_chars=24000):
    """Collect git status and git diff for the review prompt."""
    parts = []
    try:
        st = subprocess.run(
            ["git", "-C", workspace_root, "status", "--short", "--untracked-files=all"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if st.returncode == 0 and st.stdout.strip():
            parts.append("git status --short:\n" + st.stdout.strip()[:4000])
    except Exception:
        pass

    diff_cmd = ["git", "-C", workspace_root, "diff"]
    if base_ref:
        diff_cmd.append(f"{base_ref}...HEAD")
    else:
        diff_cmd.append("HEAD")

    try:
        df = subprocess.run(
            diff_cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
        )
        if df.returncode == 0 and df.stdout.strip():
            diff_text = df.stdout.strip()
            if len(diff_text) > max_chars:
                diff_text = diff_text[:max_chars] + "\n... [diff truncated]"
            parts.append(f"{' '.join(diff_cmd[3:])}:\n{diff_text}")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else "No git changes detected in working tree."


def _pick_fallback_opus_from_stderr(stderr_text):
    m = re.search(r"available models:\s*(.+)", stderr_text or "", re.IGNORECASE)
    if not m:
        return None
    models = [x.strip() for x in m.group(1).split(",") if x.strip()]
    for mod in models:
        if "opus" in mod.lower():
            return mod
    for mod in models:
        if "claude" in mod.lower():
            return mod
    return None


def _run_agy_opus(prompt, workspace_root, model=DEFAULT_OPUS_MODEL, timeout_sec=180):
    """Invoke `agy --model claude-opus-5-5 -p <prompt>` safely without recursion."""
    agy_bin = _find_agy_bin()
    env = os.environ.copy()
    env["OPUS_REVIEW_GATE_ACTIVE"] = "1"
    env["CODEX_REVIEW_GATE_ACTIVE"] = "1"
    env["AGY_PERMISSION_REVIEWER_ACTIVE"] = "1"
    env["CLAUDE_PERMISSION_REVIEWER_ACTIVE"] = "1"
    env["AGY_PERMISSION_REVIEWER_DEPTH"] = "2"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    def _invoke(target_model):
        cmd = [
            agy_bin,
            "--disable-slash-commands",
            "--print-timeout",
            f"{timeout_sec - 10}s",
            "--add-dir",
            workspace_root,
        ]
        if target_model:
            cmd.extend(["--model", target_model])
        cmd.extend(["-p", prompt])
        return subprocess.run(
            cmd,
            cwd=workspace_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=env,
        )

    res = _invoke(model)
    if res.returncode != 0:
        alt = _pick_fallback_opus_from_stderr(res.stderr)
        if alt and alt != model:
            _log(f"Primary model {model} unavailable, retrying with {alt}")
            res = _invoke(alt)
    return res


def _parse_stop_review_output(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return True, "빈 응답 통과"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0] if lines else ""

    if first_line.startswith("ALLOW:"):
        return True, first_line[len("ALLOW:") :].strip()

    if first_line.startswith("BLOCK:"):
        reason = text[text.find("BLOCK:") + len("BLOCK:") :].strip()
        return (
            False,
            f"[Opus Review Gate (Claude Opus 5.5)] 턴 종료 전 수정이 필요한 사항이 발견되었습니다:\n{reason}",
        )

    for ln in lines:
        if ln.startswith("ALLOW:"):
            return True, ln[len("ALLOW:") :].strip()
        if ln.startswith("BLOCK:"):
            idx = text.find("BLOCK:")
            reason = text[idx + len("BLOCK:") :].strip()
            return (
                False,
                f"[Opus Review Gate (Claude Opus 5.5)] 턴 종료 전 수정이 필요한 사항이 발견되었습니다:\n{reason}",
            )

    return True, "형식 외 응답 통과"


def run_stop_hook():
    """Handle Antigravity Stop hook JSON on stdin -> JSON on stdout."""
    if (
        os.environ.get("OPUS_REVIEW_GATE_ACTIVE") == "1"
        or os.environ.get("AGY_PERMISSION_REVIEWER_ACTIVE") == "1"
        or os.environ.get("CODEX_REVIEW_GATE_ACTIVE") == "1"
    ):
        sys.stdout.write("{}\n")
        return

    raw = sys.stdin.read().strip()
    payload = {}
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}

    workspace_paths = payload.get("workspacePaths") or []
    cwd = (
        (workspace_paths[0] if workspace_paths else None)
        or payload.get("cwd")
        or os.getcwd()
    )
    workspace_root = _resolve_workspace_root(cwd)

    # 1. Fast-path: check if review gate is enabled (default is False)
    enabled, _, _, model = get_workspace_review_gate(workspace_root)
    if not enabled:
        sys.stdout.write("{}\n")
        return

    # 2. Fast-path: only run on normal turn completion ("NO_TOOL_CALL")
    term_reason = payload.get("terminationReason")
    if term_reason and term_reason != "NO_TOOL_CALL":
        sys.stdout.write("{}\n")
        return

    # 3. Fast-path: check transcript.jsonl to see if any edits/commands happened in this turn
    transcript_path = payload.get("transcriptPath")
    has_edits, last_assistant_msg, turn_actions_summary = _inspect_antigravity_turn(
        transcript_path
    )
    if not has_edits:
        sys.stdout.write("{}\n")
        return

    response_block = (
        f"Previous Agent response:\n{last_assistant_msg.strip()}"
        if last_assistant_msg.strip()
        else ""
    )
    git_context_block = _collect_git_context(workspace_root)
    prompt = STOP_REVIEW_PROMPT_TEMPLATE.format(
        agent_response_block=response_block,
        turn_actions_block=turn_actions_summary,
        git_context_block=git_context_block,
    )

    try:
        res = _run_agy_opus(prompt, workspace_root, model=model, timeout_sec=180)
        if res.returncode == 0 and res.stdout.strip():
            ok, reason = _parse_stop_review_output(res.stdout)
            _log(f"Stop review ({model}) ok={ok} reason={reason[:160]}")
            if not ok:
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "continue",
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                return
        else:
            _log(f"Stop review ({model}) rc={res.returncode} stderr={res.stderr.strip()[:200]}")
    except Exception as e:
        _log(f"Stop review exception: {e}")

    sys.stdout.write("{}\n")


def cmd_setup(args):
    workspace_root = _resolve_workspace_root(args.cwd)
    actions = []

    if args.enable_review_gate and args.disable_review_gate:
        sys.stderr.write("Error: Choose either --enable-review-gate or --disable-review-gate.\n")
        sys.exit(1)

    if args.enable_review_gate:
        if args.global_scope:
            save_global_config(True, model=args.model)
            set_workspace_review_gate(workspace_root, True, model=args.model)
            actions.append(
                f"전역 및 현재 워크스페이스({workspace_root})의 Opus 5.5 Stop Review Gate를 활성화했습니다."
            )
        else:
            set_workspace_review_gate(workspace_root, True, model=args.model)
            actions.append(
                f"현재 워크스페이스({workspace_root})의 Opus 5.5 Stop Review Gate를 활성화했습니다."
            )
    elif args.disable_review_gate:
        if args.global_scope:
            save_global_config(False, model=args.model)
            set_workspace_review_gate(workspace_root, False, model=args.model)
            actions.append(
                f"전역 및 현재 워크스페이스({workspace_root})의 Opus 5.5 Stop Review Gate를 비활성화했습니다."
            )
        else:
            set_workspace_review_gate(workspace_root, False, model=args.model)
            actions.append(
                f"현재 워크스페이스({workspace_root})의 Opus 5.5 Stop Review Gate를 비활성화했습니다."
            )

    eff_enabled, ws_override, global_enabled, model = get_workspace_review_gate(workspace_root)
    agy_bin = _find_agy_bin()

    report = {
        "ready": bool(shutil.which(agy_bin) or os.path.isfile(agy_bin)),
        "workspaceRoot": workspace_root,
        "reviewGateEnabled": eff_enabled,
        "workspaceOverride": ws_override,
        "globalReviewGateEnabled": global_enabled,
        "model": model,
        "agyBinary": agy_bin,
        "actionsTaken": actions,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        status_str = "ENABLED (활성화됨)" if eff_enabled else "DISABLED (비활성화됨 - 기본값)"
        print(f"=== Opus 5.5 Review Gate Setup ({workspace_root}) ===")
        for act in actions:
            print(f"- {act}")
        print(f"- Stop Review Gate 상태: {status_str} (전역 기본값: {'ON' if global_enabled else 'OFF'})")
        print(f"- 리뷰 모델: {model} (via {agy_bin})")


def cmd_review(args):
    workspace_root = _resolve_workspace_root(args.cwd)
    _, _, _, model = get_workspace_review_gate(workspace_root)
    git_context_block = _collect_git_context(workspace_root, base_ref=args.base, max_chars=40000)
    prompt = CODE_REVIEW_PROMPT_TEMPLATE.format(
        workspace_root=workspace_root,
        git_context_block=git_context_block,
    )
    res = _run_agy_opus(prompt, workspace_root, model=model, timeout_sec=240)
    if res.stdout:
        sys.stdout.write(res.stdout)
        if not res.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if res.returncode != 0 and res.stderr:
        sys.stderr.write(res.stderr)
    sys.exit(res.returncode)


def cmd_adversarial_review(args):
    workspace_root = _resolve_workspace_root(args.cwd)
    _, _, _, model = get_workspace_review_gate(workspace_root)
    focus = (
        " ".join(args.focus).strip()
        if args.focus
        else "특정 포커스 지정 없음 (전반적인 설계 및 엣지 케이스 검증)"
    )
    git_context_block = _collect_git_context(workspace_root, max_chars=40000)
    prompt = ADVERSARIAL_REVIEW_PROMPT_TEMPLATE.format(
        workspace_root=workspace_root,
        focus=focus,
        git_context_block=git_context_block,
    )
    res = _run_agy_opus(prompt, workspace_root, model=model, timeout_sec=240)
    if res.stdout:
        sys.stdout.write(res.stdout)
        if not res.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if res.returncode != 0 and res.stderr:
        sys.stderr.write(res.stderr)
    sys.exit(res.returncode)


def main():
    if len(sys.argv) == 1:
        run_stop_hook()
        return

    parser = argparse.ArgumentParser(description="Antigravity Claude Opus 5.5 Review Gate")
    sub = parser.add_subparsers(dest="subcommand")

    p_setup = sub.add_parser("setup", help="Check readiness and toggle Opus 5.5 Stop Review Gate")
    p_setup.add_argument("--enable-review-gate", action="store_true")
    p_setup.add_argument("--disable-review-gate", action="store_true")
    p_setup.add_argument("--global", dest="global_scope", action="store_true")
    p_setup.add_argument("--model", default=None, help="Override review model (default: claude-opus-5-5)")
    p_setup.add_argument("--cwd", default=None)
    p_setup.add_argument("--json", action="store_true")

    p_rev = sub.add_parser("review", help="Run non-interactive Opus 5.5 code review")
    p_rev.add_argument("--base", default=None)
    p_rev.add_argument("--cwd", default=None)

    p_adv = sub.add_parser("adversarial-review", help="Run adversarial Opus 5.5 design/code review")
    p_adv.add_argument("--cwd", default=None)
    p_adv.add_argument("focus", nargs="*")

    sub.add_parser("hook", help="Run Stop hook handler reading JSON from stdin")

    args = parser.parse_args()
    if args.subcommand == "setup":
        cmd_setup(args)
    elif args.subcommand == "review":
        cmd_review(args)
    elif args.subcommand == "adversarial-review":
        cmd_adversarial_review(args)
    else:
        run_stop_hook()


if __name__ == "__main__":
    main()
