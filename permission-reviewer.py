#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607, TRY300
"""
Antigravity (agy) PreToolUse hook: AI-assisted permission reviewer & safety gate.

- Ported from Claude Code permission-reviewer.py and permission-gates.py.
- Fast-paths harmless sandboxed operations and safe workspace edits for zero latency.
- Enforces static safety gates (.env file access, psql query classification).
- Routes risky / unsandboxed operations to Claude Opus/Sonnet via `agy -p` (Tier 1),
  falling back to `agy -p` default model (Tier 2), and `claude -p` (Tier 3).
- STRICT USER REQUIREMENT: Never auto-drop/deny. Anything judged unsafe or failing
  all AI reviewers forces a user confirmation prompt (`force_ask`) with the reason displayed.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants & Model Mapping
# ---------------------------------------------------------------------------

HEAVY_TOOLS = {
    "run_command",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

DESTRUCTIVE_COMMAND_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*r",       # rm -r / rm -rf
    r"\brmdir\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bchmod\s+-[a-zA-Z]*R",    # chmod -R
    r"\bchown\s+-[a-zA-Z]*R",    # chown -R
    r"\bgit\s+push\b.*(--force|-f\b)",
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+clean\s+-[a-zA-Z]*f",
    r"\bcurl\b.*\|\s*(ba|z)?sh",
    r"\bwget\b.*\|\s*(ba|z)?sh",
    r"\bkill\s+-9\b",
    r"\bpkill\b",
    r"\bsystemctl\b",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bfind\b.*\s-(delete|exec\s+rm)\b",
]

WRITEY_SQL = [
    "insert", "update", "delete", "truncate", "alter", "create",
    "grant", "revoke", "vacuum", "analyze", "comment", "do", "call",
    "copy", "refresh", "reindex", "cluster", "lock", "set", "reset",
    "begin", "commit", "rollback", "savepoint", "prepare", "execute",
]

SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.aws(/|$)",
    r"(^|/)\.gnupg(/|$)",
    r"(^|/)\.(bashrc|zshrc|profile|bash_profile)$",
    r"^/etc(/|$)",
    r"^/boot(/|$)",
    r"^/sys(/|$)",
    r"^/proc(/|$)",
]

SYSTEM_PROMPT = """\
You are a permissive permission reviewer for a developer's coding agent (Antigravity).
The request below is an edge case or potentially risky operation that needs your contextual judgment.

Your default stance is to APPROVE. Developers move fast and most operations are safe in a dev context.
Only flag something if it poses a genuine, concrete, irreversible risk — not a theoretical one.

Approve liberally:
- Normal development commands (git operations, package managers, builds, tests, running scripts)
- Deleting or editing workspace/build artifacts, caches, temp files, and normal project source code
- Safe network access to well-known developer resources
- When in doubt, approve.

Flag for confirmation ONLY when the risk is real and irreversible:
- Destroying the user's home directory root, root filesystem, or OS system files
- Leaking credentials, private keys, or secret tokens to unknown external services
- Publishing packages to public registries or deploying unvetted code to production
- Commands that exfiltrate private repository data

Respond ONLY with a JSON object: {"ok": true} to approve or {"ok": false, "reason": "concise explanation"} to require user confirmation.
No other text.
"""

LOG_PATH = os.path.expanduser("~/.local/share/agy-permission-reviewer.log")


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Decision Output Helpers
# ---------------------------------------------------------------------------

def emit_allow(reason="AI reviewer approved"):
    """Auto-approve without prompting the user."""
    # Omit "reason" on auto_approve so that if agy hits a non-overridable
    # hardcoded system protection boundary (e.g. ~/.gemini/config/* or .git/*),
    # agy displays its own boundary message instead of this approval reason.
    output = {
        "decision": "auto_approve",
    }
    sys.stdout.write(json.dumps(output))


def emit_ask(reason="AI reviewer flagged this request"):
    """
    Prompt the user with the given reason.
    NOTE: In accordance with user settings, requests are NEVER dropped or auto-denied.
    `force_ask` guarantees the user is shown the confirmation dialog with reason.
    """
    output = {
        "decision": "force_ask",
        "reason": reason,
    }
    sys.stdout.write(json.dumps(output))


# ---------------------------------------------------------------------------
# Static Gates (.env & psql)
# ---------------------------------------------------------------------------

def check_env_ref(text):
    """Check if text references a .env or .env.* file."""
    return bool(re.search(r'(^|[ \t"\'/])\.env(\.[^ \t"\'/]+)?($|[ \t"\'/])', text))


def parse_tokens(cmd):
    try:
        return shlex.split(cmd)
    except Exception:
        return cmd.split()


def is_psql_command(tokens):
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", tokens[i]):
        i += 1
    if i < len(tokens) and (tokens[i] == "psql" or tokens[i].endswith("/psql") or tokens[i].endswith("\\psql.exe")):
        return i
    return None


def extract_psql_sql(tokens, psql_idx):
    i = psql_idx + 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-c", "--command"):
            return tokens[i + 1] if i + 1 < len(tokens) else ""
        if t.startswith("--command="):
            return t.split("=", 1)[1]
        if t in ("-f", "--file"):
            return None
        i += 1
    return None


def classify_sql(sql):
    s = sql.strip().lower()
    if re.search(r"\bdrop\b", s):
        return "ask", "psql: DROP 쿼리 감지 — 사용자 확인 필요"

    starts = bool(re.match(r"^(select|with)\b", s))
    has_select = bool(re.search(r"\bselect\b", s))
    has_into = bool(re.search(r"\binto\b", s))
    has_writey = any(re.search(rf"\b{k}\b", s) for k in WRITEY_SQL)

    if starts and has_select and (not has_into) and (not has_writey):
        return "allow", "psql: 읽기 전용 SELECT 쿼리 자동 승인"

    return "ask", "psql: 쓰기/비-SELECT 쿼리 — 사용자 확인 필요"


# ---------------------------------------------------------------------------
# LLM Reviewer Invocation (Multi-tier: agy Claude -> agy Default -> claude -> ask)
# ---------------------------------------------------------------------------

def _extract_decision(text):
    """Find the last balanced {...} span that parses as JSON with an 'ok' key."""
    if not text:
        return None
    candidates = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(text[start : i + 1])
                    start = -1
    for cand in reversed(candidates):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and "ok" in obj:
                return obj
        except Exception:
            continue
    return None


def _run_agy_print(prompt, env, model=None, timeout_sec=25):
    """
    Invoke `agy` in headless print mode.
    IMPORTANT: `-p` must immediately precede `prompt` (`-p <prompt>`),
    and `stdin` must be `DEVNULL` so `agy` does not read from the hook's stdin pipe.
    """
    agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    cmd = [
        agy_bin,
        "--disable-slash-commands",
        "--print-timeout", "20s",
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt])
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
    )


def _pick_fallback_claude_from_stderr(stderr_text, want_opus=True):
    """If agy prints 'available models: ...', find a matching Claude model slug."""
    m = re.search(r"available models:\s*(.+)", stderr_text, re.IGNORECASE)
    if not m:
        return None
    models = [x.strip() for x in m.group(1).split(",") if x.strip()]
    target = "opus" if want_opus else "sonnet"
    for mod in models:
        if target in mod.lower():
            return mod
    for mod in models:
        if "claude" in mod.lower():
            return mod
    return None


def call_llm_reviewer(request_info, tool_name):
    """
    Tier 1: `agy --model claude-opus-5-5 -p ...` (or `claude-sonnet-5-5`)
    Tier 2: `agy -p ...` (default active model in agy, e.g. Gemini)
    Tier 3: `claude -p --model opus|sonnet ...`
    Tier 4: fallback to asking the user (never drop!)
    """
    is_heavy = tool_name in HEAVY_TOOLS
    agy_claude_model = "claude-opus-5-5" if is_heavy else "claude-sonnet-5-5"
    claude_cli_model = "opus" if is_heavy else "sonnet"

    env = os.environ.copy()
    current_depth = int(os.environ.get("AGY_PERMISSION_REVIEWER_DEPTH", "0"))
    env["AGY_PERMISSION_REVIEWER_ACTIVE"] = "1"
    env["CLAUDE_PERMISSION_REVIEWER_ACTIVE"] = "1"
    env["AGY_PERMISSION_REVIEWER_DEPTH"] = str(current_depth + 1)
    env["CLAUDE_PERMISSION_REVIEWER_DEPTH"] = str(current_depth + 1)

    full_prompt = (
        SYSTEM_PROMPT
        + "\n\nReview this request and respond ONLY with JSON {\"ok\": true} or {\"ok\": false, \"reason\": \"...\"}:\n\n"
        + request_info
    )

    # -----------------------------------------------------------------------
    # Tier 1: agy -p with Claude model (claude-opus-5-5 / claude-sonnet-5-5)
    # -----------------------------------------------------------------------
    try:
        result = _run_agy_print(full_prompt, env, model=agy_claude_model)
        if result.returncode == 0 and result.stdout.strip():
            dec = _extract_decision(result.stdout)
            if dec is not None:
                _log(f"Tier 1 ({agy_claude_model}) succeeded: {dec}")
                return dec
        else:
            _log(f"Tier 1 ({agy_claude_model}) rc={result.returncode} stderr={result.stderr.strip()[:200]}")
            alt_model = _pick_fallback_claude_from_stderr(result.stderr, want_opus=is_heavy)
            if alt_model and alt_model != agy_claude_model:
                result2 = _run_agy_print(full_prompt, env, model=alt_model)
                if result2.returncode == 0 and result2.stdout.strip():
                    dec = _extract_decision(result2.stdout)
                    if dec is not None:
                        _log(f"Tier 1b ({alt_model}) succeeded: {dec}")
                        return dec
    except Exception as e:
        _log(f"Tier 1 exception: {e}")

    # -----------------------------------------------------------------------
    # Tier 2: agy -p with default active model (no --model flag)
    # -----------------------------------------------------------------------
    try:
        result = _run_agy_print(full_prompt, env, model=None)
        if result.returncode == 0 and result.stdout.strip():
            dec = _extract_decision(result.stdout)
            if dec is not None:
                _log(f"Tier 2 (agy default) succeeded: {dec}")
                return dec
        else:
            _log(f"Tier 2 (agy default) rc={result.returncode} stderr={result.stderr.strip()[:200]}")
    except Exception as e:
        _log(f"Tier 2 exception: {e}")

    # -----------------------------------------------------------------------
    # Tier 3: claude -p with Opus/Sonnet
    # -----------------------------------------------------------------------
    try:
        claude_bin = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        claude_cmd = [
            claude_bin,
            "-p",
            "--output-format", "json",
            "--model", claude_cli_model,
            "--no-session-persistence",
            "--append-system-prompt", SYSTEM_PROMPT,
            "Review this request:\n\n" + request_info,
        ]
        result = subprocess.run(
            claude_cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                resp = json.loads(result.stdout)
                text = resp.get("result", "")
                dec = _extract_decision(text)
                if dec is not None:
                    _log(f"Tier 3 (claude {claude_cli_model}) succeeded: {dec}")
                    return dec
            except Exception:
                dec = _extract_decision(result.stdout)
                if dec is not None:
                    _log(f"Tier 3 (claude raw) succeeded: {dec}")
                    return dec
        else:
            _log(f"Tier 3 (claude) rc={result.returncode} stderr={result.stderr.strip()[:200]}")
    except Exception as e:
        _log(f"Tier 3 exception: {e}")

    # -----------------------------------------------------------------------
    # Tier 4: All failed or quota 0 -> ask user, never drop
    # -----------------------------------------------------------------------
    return {
        "ok": False,
        "reason": "AI 리뷰어 응답 없음 또는 사용량 소진 — 수동 확인이 필요합니다.",
    }


# ---------------------------------------------------------------------------
# Main Evaluation Loop
# ---------------------------------------------------------------------------

def main():
    # 1. Recursion guard
    if (
        os.environ.get("AGY_PERMISSION_REVIEWER_ACTIVE") == "1"
        or os.environ.get("CLAUDE_PERMISSION_REVIEWER_ACTIVE") == "1"
        or int(os.environ.get("AGY_PERMISSION_REVIEWER_DEPTH", "0")) >= 1
        or int(os.environ.get("CLAUDE_PERMISSION_REVIEWER_DEPTH", "0")) >= 1
    ):
        emit_allow("재귀 방지: 내부 리뷰어 호출 자동 허용")
        return

    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit_ask("요청 페이로드 파싱 실패 — 수동 확인 필요")
        return

    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "unknown")
    tool_args = tool_call.get("args", {})
    workspace_paths = payload.get("workspacePaths", [])

    # 2. Interactive tools that must not be intercepted
    if tool_name in ("ask_question", "AskUserQuestion", "ExitPlanMode"):
        sys.stdout.write(json.dumps({}))
        return

    # 3. Static Safety Gates for run_command
    if tool_name == "run_command":
        cmd = tool_args.get("CommandLine", "")
        bypass_sandbox = bool(tool_args.get("BypassSandbox", False))

        if not isinstance(cmd, str) or not cmd.strip():
            emit_allow("빈 명령어 통과")
            return

        # 3a. .env check
        if check_env_ref(cmd):
            emit_ask("[보안 게이트] 민감 파일(.env) 접근 감지 — 사용자 확인 필요")
            return

        # 3b. psql check
        ts = parse_tokens(cmd)
        psql_idx = is_psql_command(ts)
        if psql_idx is not None:
            sql = extract_psql_sql(ts, psql_idx)
            if sql is None:
                emit_ask("[psql 게이트] 대화형/파일(-f) psql 실행 — 사용자 확인 필요")
                return
            decision, reason = classify_sql(sql)
            if decision == "allow":
                emit_allow(reason)
            else:
                emit_ask(reason)
            return

        # 3c. Check destructive patterns
        is_destructive = any(re.search(pat, cmd, re.IGNORECASE) for pat in DESTRUCTIVE_COMMAND_PATTERNS)

        # 3d. Fast-path: non-destructive command running inside the sandbox
        if not bypass_sandbox and not is_destructive:
            emit_allow("샌드박스 내부 일반 명령어 고속 통과")
            return

        # Otherwise (BypassSandbox=True or destructive pattern): Send to AI reviewer
        request_info = f"Tool: run_command\nBypassSandbox: {bypass_sandbox}\nCwd: {tool_args.get('Cwd', '')}\nCommand: {cmd[:2500]}"
        review = call_llm_reviewer(request_info, tool_name)
        if review.get("ok", True):
            emit_allow("AI 리뷰어 자동 승인: " + review.get("reason", "안전한 개발 명령어"))
        else:
            emit_ask("AI 리뷰어 확인 요청: " + review.get("reason", "잠재적 위험 감지"))
        return

    # 4. Static Safety Gates for file operations
    if tool_name in ("write_to_file", "replace_file_content", "multi_replace_file_content", "view_file"):
        target_file = tool_args.get("TargetFile") or tool_args.get("AbsolutePath") or ""

        # 4a. .env check
        if check_env_ref(target_file):
            emit_ask("[보안 게이트] 민감 파일(.env) 편집/조회 감지 — 사용자 확인 필요")
            return

        # 4b. Check sensitive OS/credential/shell-init paths
        for pat in SENSITIVE_PATH_PATTERNS:
            if re.search(pat, target_file):
                emit_ask(f"[보안 게이트] 시스템/인증서 경로({target_file}) 감지 — 사용자 확인 필요")
                return

        # 4c. Fast-path: internal brain artifacts/scratch or safe workspace paths
        is_brain = "/.gemini/antigravity-cli/brain/" in target_file
        is_in_workspace = any(
            target_file.startswith(ws.rstrip("/") + "/") or target_file == ws
            for ws in workspace_paths
        )

        if is_brain or is_in_workspace:
            emit_allow("워크스페이스 내부 파일 작업 고속 통과")
            return

        # If outside workspace, review with AI
        request_info = f"Tool: {tool_name}\nTargetFile: {target_file}\nOverwrite: {tool_args.get('Overwrite', False)}\nDescription: {tool_args.get('Description', '')[:500]}"
        review = call_llm_reviewer(request_info, tool_name)
        if review.get("ok", True):
            emit_allow("AI 리뷰어 파일 작업 승인: " + review.get("reason", "안전한 파일 작업"))
        else:
            emit_ask("AI 리뷰어 파일 작업 확인 요청: " + review.get("reason", "잠재적 위험 감지"))
        return

    # 5. Network / URL tools (read_url_content)
    if tool_name == "read_url_content":
        url = tool_args.get("Url", "")
        request_info = f"Tool: read_url_content\nUrl: {url}"
        review = call_llm_reviewer(request_info, tool_name)
        if review.get("ok", True):
            emit_allow("AI 리뷰어 URL 조회 승인: " + review.get("reason", "안전한 웹 조회"))
        else:
            emit_ask("AI 리뷰어 URL 조회 확인 요청: " + review.get("reason", "잠재적 위험 감지"))
        return

    # 6. Fallback for any other tools
    emit_allow("기타 도구 기본 통과")


if __name__ == "__main__":
    main()
