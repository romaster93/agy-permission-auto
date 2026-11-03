#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607
"""
Antigravity (agy) Stop-Time Review Gate & Companion for OpenAI Codex CLI.

- Equivalent to Claude Code's `/codex:setup --enable-review-gate`, `/codex:review`,
  and `/codex:adversarial-review`.
- Disabled by default (`stopReviewGate: false`). Toggle per-workspace or globally via:
    python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --enable-review-gate
    python3 ~/.gemini/config/plugins/codex-review-gate/codex-review-gate.py setup --disable-review-gate
- When enabled, runs at the `Stop` hook lifecycle event:
  - Fast-paths (0ms) if the turn did not execute any file edits or shell commands
    by inspecting `transcriptPath` (`transcript.jsonl`).
  - If code changes occurred in the turn, runs `codex exec -s read-only --ephemeral`
    with the stop-review-gate rubric.
  - On `ALLOW:`, returns `{}` so `agy` finishes the turn normally.
  - On `BLOCK: <한국어 사유>`, returns `{"decision": "continue", "reason": "..."}` so
    `agy` blocks stopping and immediately re-enters the loop to fix the issue.
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

STATE_ROOT = os.path.expanduser("~/.gemini/antigravity-cli/codex-review-gate")
GLOBAL_CONFIG_PATH = os.path.join(STATE_ROOT, "global-config.json")
WORKSPACES_DIR = os.path.join(STATE_ROOT, "workspaces")

EDIT_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

STOP_REVIEW_PROMPT_TEMPLATE = """\
<task>
Run a stop-gate review of the previous agent turn.
Only review the work from the previous turn.
Only review it if the agent actually did code changes in that turn.
Pure status, setup, or reporting output does not count as reviewable work.
For example, the output of /codex-review-gate, /codex:setup, or /codex:status does not count.
Only direct edits made in that specific turn count.
If the previous turn was only a status update, a summary, a setup/login check, a review result, or output from a command that did not itself make direct edits in that turn, return ALLOW immediately and do no further work.
Challenge whether that specific work and its design choices should ship.

{agent_response_block}
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
Use ALLOW if the previous turn did not make code changes or if you do not see a blocking issue.
Use ALLOW immediately, without extra investigation, if the previous turn was not an edit-producing turn.
Use BLOCK only if the previous turn made code changes and you found something concrete that still needs to be fixed before stopping.
</default_follow_through_policy>

<grounding_rules>
Ground every blocking claim in the repository context or tool outputs you inspected during this run.
Do not treat the previous agent response as proof that code changes happened; verify that from the repository state before you block.
Do not block based on older edits from earlier turns when the immediately previous turn did not itself make direct edits.
</grounding_rules>

<dig_deeper_nudge>
If the previous turn did make code changes, check for second-order failures, empty-state behavior, retries, stale state, rollback risk, and design tradeoffs before you finalize.
</dig_deeper_nudge>
"""

ADVERSARIAL_REVIEW_PROMPT_TEMPLATE = """\
Run an adversarial code and design review of the current uncommitted or branch changes in this repository.
Position this as a challenge review that questions the chosen implementation, design choices, tradeoffs, and assumptions — not just a surface lint pass.

User focus: {focus}

Check for:
1. Broken invariants, hidden edge cases, race conditions, or error-handling gaps
2. Stale state, partial failure/rollback risks, and security/sandbox boundary issues
3. Simpler or more principled alternatives to the current design

Write your review report clearly in Korean (한국어), citing specific file paths and line numbers.
"""


def _build_env_with_node_and_codex():
    """Ensure nvm node/codex and ~/.local/bin are in PATH even in non-interactive hook shells."""
    env = os.environ.copy()
    extra_dirs = []
    nvm_bins = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    extra_dirs.extend(nvm_bins)
    for d in (os.path.expanduser("~/.local/bin"), "/usr/local/bin"):
        if os.path.isdir(d):
            extra_dirs.append(d)
    current_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(extra_dirs + ([current_path] if current_path else []))
    return env


def _find_codex_bin(env):
    found = shutil.which("codex", path=env.get("PATH"))
    if found:
        return found
    return None


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
        return {"stopReviewGate": False}
    try:
        with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"stopReviewGate": bool(data.get("stopReviewGate", False))}
    except Exception:
        return {"stopReviewGate": False}


def save_global_config(enabled):
    os.makedirs(STATE_ROOT, exist_ok=True)
    data = {"stopReviewGate": bool(enabled)}
    with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def get_workspace_review_gate(workspace_root):
    """Return (effective_enabled, workspace_override_or_none, global_enabled)."""
    global_cfg = load_global_config()
    global_enabled = bool(global_cfg.get("stopReviewGate", False))
    state_file = _workspace_state_file(workspace_root)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("stopReviewGate"), bool):
                ws_val = data["stopReviewGate"]
                return ws_val, ws_val, global_enabled
        except Exception:
            pass
    return global_enabled, None, global_enabled


def set_workspace_review_gate(workspace_root, enabled):
    os.makedirs(WORKSPACES_DIR, exist_ok=True)
    state_file = _workspace_state_file(workspace_root)
    data = {
        "workspaceRoot": workspace_root,
        "stopReviewGate": bool(enabled),
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def _check_codex_status(env):
    codex_bin = _find_codex_bin(env)
    if not codex_bin:
        return {
            "available": False,
            "version": None,
            "loggedIn": False,
            "authDetail": "codex binary not found",
        }
    version_str = "unknown"
    try:
        v = subprocess.run(
            [codex_bin, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        if v.returncode == 0:
            version_str = v.stdout.strip() or v.stderr.strip()
    except Exception:
        pass

    logged_in = False
    auth_detail = "unknown"
    try:
        auth = subprocess.run(
            [codex_bin, "login", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        out = (auth.stdout + "\n" + auth.stderr).strip()
        auth_detail = out.splitlines()[0] if out else f"exit {auth.returncode}"
        logged_in = auth.returncode == 0 and "not logged in" not in out.lower()
    except Exception as e:
        auth_detail = str(e)

    return {
        "available": True,
        "binary": codex_bin,
        "version": version_str,
        "loggedIn": logged_in,
        "authDetail": auth_detail,
    }


def _inspect_antigravity_turn(transcript_path):
    """
    Inspect transcript.jsonl to determine if the latest turn (since last USER_INPUT
    or last [Codex Review Gate] continuation) executed any file edits or shell commands.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return True, ""

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
                or "[Codex Review Gate]" in content
            ):
                turn_start_idx = i + 1
                break

        has_edits = False
        last_assistant_message = ""

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
                if name in EDIT_TOOLS:
                    has_edits = True
                elif name == "run_command":
                    args = tc.get("args") or {}
                    cmd = str(args.get("CommandLine") or "")
                    if "codex-review-gate.py" not in cmd and "codex-companion.mjs" not in cmd:
                        has_edits = True

        return has_edits, last_assistant_message
    except Exception:
        return True, ""


def _parse_stop_review_output(raw_text):
    text = (raw_text or "").strip()
    if not text:
        return False, "[Codex Review Gate] Codex 리뷰 응답이 비어 있습니다."

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0] if lines else ""

    if first_line.startswith("ALLOW:"):
        return True, first_line[len("ALLOW:") :].strip()

    if first_line.startswith("BLOCK:"):
        reason = text[text.find("BLOCK:") + len("BLOCK:") :].strip()
        return (
            False,
            f"[Codex Review Gate] 턴 종료 전 수정이 필요한 사항이 발견되었습니다:\n{reason}",
        )

    for ln in lines:
        if ln.startswith("ALLOW:"):
            return True, ln[len("ALLOW:") :].strip()
        if ln.startswith("BLOCK:"):
            idx = text.find("BLOCK:")
            reason = text[idx + len("BLOCK:") :].strip()
            return (
                False,
                f"[Codex Review Gate] 턴 종료 전 수정이 필요한 사항이 발견되었습니다:\n{reason}",
            )

    return True, "형식 외 응답 통과"


def run_stop_hook():
    """Handle Antigravity Stop hook JSON on stdin -> JSON on stdout."""
    if (
        os.environ.get("CODEX_REVIEW_GATE_ACTIVE") == "1"
        or os.environ.get("OPUS_REVIEW_GATE_ACTIVE") == "1"
        or os.environ.get("AGY_PERMISSION_REVIEWER_ACTIVE") == "1"
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
    enabled, _, _ = get_workspace_review_gate(workspace_root)
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
    has_edits, last_assistant_msg = _inspect_antigravity_turn(transcript_path)
    if not has_edits:
        sys.stdout.write("{}\n")
        return

    env = _build_env_with_node_and_codex()
    env["CODEX_REVIEW_GATE_ACTIVE"] = "1"
    codex_bin = _find_codex_bin(env)
    if not codex_bin:
        sys.stdout.write("{}\n")
        return

    response_block = (
        f"Previous Agent response:\n{last_assistant_msg.strip()}"
        if last_assistant_msg.strip()
        else ""
    )
    prompt = STOP_REVIEW_PROMPT_TEMPLATE.format(agent_response_block=response_block)

    fd, out_file = tempfile.mkstemp(prefix="agy-codex-gate-", suffix=".txt")
    os.close(fd)
    try:
        cmd = [
            codex_bin,
            "exec",
            "-s",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            workspace_root,
            "-o",
            out_file,
            prompt,
        ]
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        raw_output = ""
        if os.path.exists(out_file):
            with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                raw_output = f.read()

        ok, reason = _parse_stop_review_output(raw_output)
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
    except Exception:
        pass
    finally:
        try:
            os.remove(out_file)
        except Exception:
            pass

    sys.stdout.write("{}\n")


def cmd_setup(args):
    env = _build_env_with_node_and_codex()
    workspace_root = _resolve_workspace_root(args.cwd)
    actions = []

    if args.enable_review_gate and args.disable_review_gate:
        sys.stderr.write("Error: Choose either --enable-review-gate or --disable-review-gate.\n")
        sys.exit(1)

    if args.enable_review_gate:
        if args.global_scope:
            save_global_config(True)
            set_workspace_review_gate(workspace_root, True)
            actions.append(f"전역 및 현재 워크스페이스({workspace_root})의 Codex Stop Review Gate를 활성화했습니다.")
        else:
            set_workspace_review_gate(workspace_root, True)
            actions.append(f"현재 워크스페이스({workspace_root})의 Codex Stop Review Gate를 활성화했습니다.")
    elif args.disable_review_gate:
        if args.global_scope:
            save_global_config(False)
            set_workspace_review_gate(workspace_root, False)
            actions.append(f"전역 및 현재 워크스페이스({workspace_root})의 Codex Stop Review Gate를 비활성화했습니다.")
        else:
            set_workspace_review_gate(workspace_root, False)
            actions.append(f"현재 워크스페이스({workspace_root})의 Codex Stop Review Gate를 비활성화했습니다.")

    eff_enabled, ws_override, global_enabled = get_workspace_review_gate(workspace_root)
    codex_info = _check_codex_status(env)

    report = {
        "ready": bool(codex_info["available"] and codex_info["loggedIn"]),
        "workspaceRoot": workspace_root,
        "reviewGateEnabled": eff_enabled,
        "workspaceOverride": ws_override,
        "globalReviewGateEnabled": global_enabled,
        "codex": codex_info,
        "actionsTaken": actions,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        status_str = "ENABLED (활성화됨)" if eff_enabled else "DISABLED (비활성화됨 - 기본값)"
        print(f"=== Codex Review Gate Setup ({workspace_root}) ===")
        for act in actions:
            print(f"- {act}")
        print(f"- Stop Review Gate 상태: {status_str} (전역 기본값: {'ON' if global_enabled else 'OFF'})")
        print(f"- Codex CLI: {codex_info.get('version') or '미설치'} ({codex_info.get('binary') or 'N/A'})")
        print(f"- 인증 상태: {codex_info.get('authDetail')}")


def cmd_review(args):
    env = _build_env_with_node_and_codex()
    codex_bin = _find_codex_bin(env)
    if not codex_bin:
        sys.stderr.write("Codex CLI를 찾을 수 없습니다.\n")
        sys.exit(1)
    workspace_root = _resolve_workspace_root(args.cwd)
    cmd = [codex_bin, "-C", workspace_root, "review"]
    if args.base:
        cmd.extend(["--base", args.base])
    else:
        cmd.append("--uncommitted")
    res = subprocess.run(cmd, env=env)
    sys.exit(res.returncode)


def cmd_adversarial_review(args):
    env = _build_env_with_node_and_codex()
    codex_bin = _find_codex_bin(env)
    if not codex_bin:
        sys.stderr.write("Codex CLI를 찾을 수 없습니다.\n")
        sys.exit(1)
    workspace_root = _resolve_workspace_root(args.cwd)
    focus = " ".join(args.focus).strip() if args.focus else "특정 포커스 지정 없음 (전반적인 설계 및 엣지 케이스 검증)"
    prompt = ADVERSARIAL_REVIEW_PROMPT_TEMPLATE.format(focus=focus)
    cmd = [
        codex_bin,
        "exec",
        "-s",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        workspace_root,
        prompt,
    ]
    res = subprocess.run(cmd, env=env)
    sys.exit(res.returncode)


def main():
    if len(sys.argv) == 1:
        run_stop_hook()
        return

    parser = argparse.ArgumentParser(description="Antigravity Codex Review Gate")
    sub = parser.add_subparsers(dest="subcommand")

    p_setup = sub.add_parser("setup", help="Check Codex readiness and toggle Stop Review Gate")
    p_setup.add_argument("--enable-review-gate", action="store_true")
    p_setup.add_argument("--disable-review-gate", action="store_true")
    p_setup.add_argument("--global", dest="global_scope", action="store_true")
    p_setup.add_argument("--cwd", default=None)
    p_setup.add_argument("--json", action="store_true")

    p_rev = sub.add_parser("review", help="Run non-interactive Codex code review")
    p_rev.add_argument("--base", default=None)
    p_rev.add_argument("--cwd", default=None)

    p_adv = sub.add_parser("adversarial-review", help="Run adversarial design/code review")
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
