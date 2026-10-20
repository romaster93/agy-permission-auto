#!/usr/bin/env python3
# ruff: noqa: S110, S603, S607, TRY300
"""
Antigravity (agy) PreToolUse hook: AI-assisted permission reviewer & safety gate.

- Ported from Claude Code permission-reviewer.py and permission-gates.py.
- Bootstraps ~/.gemini/antigravity-cli/settings.json (`permissions.allow` wildcards)
  so agy's built-in Auto mode classifier defers to this hook (`auto_approve` vs `force_ask`).
- Fast-paths harmless sandboxed operations, read-only file lookups, and safe workspace edits.
- Enforces static safety gates (.env file access, credential paths, psql query classification).
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
    "write_file",
    "write_to_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
}

WRITE_FILE_TOOLS = {
    "write_file",
    "write_to_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
}

READ_FILE_TOOLS = {
    "read_file",
    "view_file",
    "grep_search",
    "list_dir",
    "find_by_name",
    "view_file_outline",
    "view_code_item",
}

REQUIRED_ALLOW_RULES = [
    "command(*)",
    "read_file(*)",
    "write_file(*)",
    "read_url(*)",
    "execute_url(*)",
    "mcp(*)",
]

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

SENSITIVE_READ_PATTERNS = [
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.aws(/|$)",
    r"(^|/)\.gnupg(/|$)",
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
- Normal development commands (git operations including git push/pull/fetch/clone without --force, package managers, builds, tests, running scripts)
- Deleting or editing workspace/build artifacts, caches, temp files, and normal project source code
- Safe network access to well-known developer resources (GitHub, package registries, documentation)
- When in doubt, approve.

Flag for confirmation ONLY when the risk is real and irreversible:
- Destroying the user's home directory root, root filesystem, or OS system files
- Force-pushing (`git push --force`) or hard-resetting shared history without explicit intent
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


def ensure_cli_permissions_allow():
    """
    Ensure ~/.gemini/antigravity-cli/settings.json contains wildcard allow rules
    so that agy's built-in Auto mode classifier (which emits priority-3 'ask' signals)
    defers to this PreToolUse hook (which emits priority-2 'auto_approve' or
    priority-5 'force_ask').
    """
    settings_path = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
    try:
        data = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            data["permissions"] = perms
        allow_list = perms.get("allow")
        if not isinstance(allow_list, list):
            allow_list = []
            perms["allow"] = allow_list

        missing = [r for r in REQUIRED_ALLOW_RULES if r not in allow_list]
        if missing:
            allow_list.extend(missing)
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            tmp_path = settings_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, settings_path)
            _log(f"Bootstrapped permissions.allow in {settings_path}: added {missing}")
    except Exception as e:
        _log(f"ensure_cli_permissions_allow error: {e}")


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
    sys.stdout.flush()


def emit_ask(reason="User confirmation required by AI safety reviewer"):
    """
    NEVER return 'deny'. Always return 'force_ask' so the user gets an interactive
    confirmation prompt with the exact reason displayed.
    """
    output = {
        "decision": "force_ask",
        "reason": reason,
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Static Safety Gates (ported from permission-gates.py)
# ---------------------------------------------------------------------------

def parse_tokens(cmd):
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def check_env_ref(text):
    """Detect references to .env files (excluding .env.example / .env.sample / .env.template)."""
    if not text:
        return False
    cleaned = re.sub(r"\.env\.(example|sample|template|defaults|dist)\b", "", text, flags=re.IGNORECASE)
    return bool(re.search(r"(^|[\s/\"'=])\.env(\.[\w-]+)?([\s\"']|$)", cleaned))


def is_psql_command(tokens):
    for i, t in enumerate(tokens):
        base = t.rsplit("/", 1)[-1]
        if base == "psql":
            return i
    return None


def extract_psql_sql(tokens, psql_idx):
    args = tokens[psql_idx + 1 :]
    sqls = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--command") and i + 1 < len(args):
            sqls.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--command="):
            sqls.append(a.split("=", 1)[1])
            i += 1
            continue
        if a.startswith("-c") and len(a) > 2:
            sqls.append(a[2:])
            i += 1
            continue
        if a in ("-f", "--file") or a.startswith("-f") or a.startswith("--file="):
            return None
        i += 1
    return "\n".join(sqls) if sqls else None


def strip_sql_comments_and_strings(sql):
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:''|[^'])*'", "''", sql)
    return sql


def classify_sql(sql):
    cleaned = strip_sql_comments_and_strings(sql).strip()
    if not cleaned:
        return ("ask", "빈 SQL 문 — 수동 확인 필요")

    stmts = [s.strip() for s in cleaned.split(";") if s.strip()]
    writey_re = re.compile(r"\b(" + "|".join(WRITEY_SQL) + r")\b", re.IGNORECASE)

    for s in stmts:
        m = writey_re.search(s)
        if m:
            return ("ask", f"SQL 변경/잠금 키워드 감지 ({m.group(1).upper()}) — 수동 확인 필요")
        first = s.split(None, 1)[0].lower() if s.split() else ""
        if first not in ("select", "with", "explain", "show", "table", "values"):
            return ("ask", f"비조회 SQL 문장 ({first}) — 수동 확인 필요")

    return ("allow", "읽기 전용 SQL 조회 승인")


# ---------------------------------------------------------------------------
# AI Model Reviewer (3-Tier Fallback: agy Opus/Sonnet -> agy Default -> claude)
# ---------------------------------------------------------------------------

def _parse_llm_json(raw_output):
    """Extract {"ok": ...} JSON object from model stdout."""
    if not raw_output:
        return None
    text = raw_output.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    except Exception:
        pass
    return None


def _build_clean_env():
    env = os.environ.copy()
    env["AGY_PERMISSION_REVIEWER_ACTIVE"] = "1"
    env["CLAUDE_PERMISSION_REVIEWER_ACTIVE"] = "1"
    depth = int(env.get("AGY_PERMISSION_REVIEWER_DEPTH", "0"))
    env["AGY_PERMISSION_REVIEWER_DEPTH"] = str(depth + 1)
    env["CLAUDE_PERMISSION_REVIEWER_DEPTH"] = str(depth + 1)
    return env


def _try_agy_cli(prompt_text, model=None, timeout=18):
    agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    if not os.path.exists(agy_bin):
        return None

    cmd = [agy_bin, "-p", prompt_text, "--output-format", "text"]
    if model:
        cmd.extend(["--model", model])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_build_clean_env(),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd="/tmp",
        )
        if res.returncode == 0 and res.stdout:
            return _parse_llm_json(res.stdout)
        _log(f"agy (model={model}) non-zero or empty: rc={res.returncode} stderr={res.stderr[:200]}")
    except Exception as e:
        _log(f"agy (model={model}) exception: {e}")
    return None


def _try_claude_cli(prompt_text, model="sonnet", timeout=18):
    claude_bin = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(claude_bin):
        return None

    cmd = [
        claude_bin,
        "-p",
        "--model", model,
        "--output-format", "text",
        "--no-session-persistence",
        "--permission-mode", "ReadOnly",
        "--system-prompt", SYSTEM_PROMPT,
        prompt_text,
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_build_clean_env(),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd="/tmp",
        )
        if res.returncode == 0 and res.stdout:
            return _parse_llm_json(res.stdout)
        _log(f"claude (model={model}) non-zero or empty: rc={res.returncode} stderr={res.stderr[:200]}")
    except Exception as e:
        _log(f"claude (model={model}) exception: {e}")
    return None


def call_llm_reviewer(request_summary, tool_name):
    """
    Tier 1: `agy -p` with Claude Opus 4.6 (for heavy tools) or Claude Sonnet 4.6 (for lighter tools)
    Tier 2: `agy -p` with default model (if Claude quota/rate limit is reached)
    Tier 3: `claude -p` (if agy CLI fails or is unavailable)
    Fallback: Returns {"ok": False, "reason": ...} -> triggers `force_ask` (never auto-deny).
    """
    full_prompt = f"{SYSTEM_PROMPT}\n\n[Request under review]\n{request_summary}"
    primary_agy_model = "claude-opus-4-6" if tool_name in HEAVY_TOOLS else "claude-sonnet-4-6"

    # Tier 1: agy with target Claude model
    result = _try_agy_cli(full_prompt, model=primary_agy_model)
    if result is not None:
        _log(f"Tier 1 (agy {primary_agy_model}) decision for {tool_name}: {result}")
        return result

    # Tier 2: agy with default model (e.g. Gemini 3.1 Pro when Claude quota is exhausted)
    result = _try_agy_cli(full_prompt, model=None)
    if result is not None:
        _log(f"Tier 2 (agy default) decision for {tool_name}: {result}")
        return result

    # Tier 3: claude CLI fallback
    claude_model = "opus" if tool_name in HEAVY_TOOLS else "sonnet"
    result = _try_claude_cli(request_summary, model=claude_model)
    if result is not None:
        _log(f"Tier 3 (claude {claude_model}) decision for {tool_name}: {result}")
        return result

    _log(f"All AI reviewer tiers failed for {tool_name}; falling back to force_ask.")
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

    # Ensure ~/.gemini/antigravity-cli/settings.json has wildcard grants so
    # agy's built-in Auto mode classifier defers to this hook's decisions.
    ensure_cli_permissions_allow()

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

    # 4. Static Safety Gates for file operations (both read and write/edit tools)
    if tool_name in WRITE_FILE_TOOLS or tool_name in READ_FILE_TOOLS:
        target_file = (
            tool_args.get("Path")
            or tool_args.get("TargetFile")
            or tool_args.get("AbsolutePath")
            or tool_args.get("File")
            or tool_args.get("SearchPath")
            or tool_args.get("DirectoryPath")
            or tool_args.get("SearchDirectory")
            or ""
        )

        # 4a. .env check (applies to both read and write)
        if check_env_ref(target_file):
            emit_ask("[보안 게이트] 민감 파일(.env) 편집/조회 감지 — 사용자 확인 필요")
            return

        # 4b. Read-only file/directory tools: block sensitive credential dirs, fast-path everything else
        if tool_name in READ_FILE_TOOLS:
            for pat in SENSITIVE_READ_PATTERNS:
                if re.search(pat, target_file):
                    emit_ask(f"[보안 게이트] 민감 인증서/키 경로({target_file}) 조회 감지 — 사용자 확인 필요")
                    return
            emit_allow("안전한 파일/디렉토리 조회 고속 통과")
            return

        # 4c. Write/Edit tools: check sensitive OS/credential/shell-init paths
        for pat in SENSITIVE_PATH_PATTERNS:
            if re.search(pat, target_file):
                emit_ask(f"[보안 게이트] 시스템/인증서 경로({target_file}) 편집 감지 — 사용자 확인 필요")
                return

        # 4d. Fast-path for Write/Edit: internal brain artifacts/scratch or safe workspace paths
        is_brain = "/.gemini/antigravity-cli/brain/" in target_file
        is_in_workspace = any(
            target_file.startswith(ws.rstrip("/") + "/") or target_file == ws
            for ws in workspace_paths
        )

        if is_brain or is_in_workspace:
            emit_allow("워크스페이스 내부 파일 작업 고속 통과")
            return

        # If writing/editing outside workspace, review with AI
        request_info = f"Tool: {tool_name}\nTargetFile: {target_file}\nOverwrite: {tool_args.get('Overwrite', False)}\nDescription: {tool_args.get('Description', '')[:500]}"
        review = call_llm_reviewer(request_info, tool_name)
        if review.get("ok", True):
            emit_allow("AI 리뷰어 파일 작업 승인: " + review.get("reason", "안전한 파일 작업"))
        else:
            emit_ask("AI 리뷰어 파일 작업 확인 요청: " + review.get("reason", "잠재적 위험 감지"))
        return

    # 5. Network / URL tools (read_url_content, search_web)
    if tool_name == "search_web":
        query = tool_args.get("query", "")
        if check_env_ref(query):
            emit_ask("[보안 게이트] 웹 검색 쿼리에 민감 정보(.env) 포함 의심 — 사용자 확인 필요")
            return
        emit_allow("웹 검색 고속 통과")
        return

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
