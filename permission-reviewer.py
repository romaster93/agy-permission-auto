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
import tempfile
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
    r"\brm\b.*(\s-[a-zA-Z]*[rf]|\s--recursive|\s--force)",  # rm -r / rm -f / rm -rf
    r"\brmdir\b",
    r"\bshred\b",
    r"\btruncate\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bchmod\b.*(\s-[a-zA-Z]*R|\s--recursive|\s777\b)",    # chmod -R / chmod 777
    r"\bchown\b.*(\s-[a-zA-Z]*R|\s--recursive)",            # chown -R
    r"\bgit\b.*\bpush\b.*(--force|-f\b)",                   # git push --force (including git -C ... push --force)
    r"\bgit\b.*\breset\b.*--hard",                          # git reset --hard
    r"\bgit\b.*\bclean\b.*(-[a-zA-Z]*f|--force)",           # git clean -f / -fd
    r"\bgit\b.*\bbranch\b.*-D\b",                           # git branch -D
    r"\b(curl|wget)\b.*\|\s*(ba|z|da|k)?sh\b",              # curl/wget | sh/bash
    r"\bsudo\b",
    r"\bkill\s+-9\b",
    r"\bpkill\b",
    r"\bkillall\b",
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
    r"^[a-zA-Z]:/windows(/|$)",
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

Respond ONLY with a JSON object: {"ok": true} to approve or {"ok": false, "reason": "한국어로 작성된 상세한 확인 요청 사유"} to require user confirmation.
IMPORTANT: The "reason" field MUST ALWAYS be written in Korean (한국어), clearly explaining what the command/operation does and why user confirmation is needed.
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


def _norm_path(p):
    """Normalize path separators to forward slashes (and lowercase on Windows)."""
    if not isinstance(p, str):
        return ""
    norm = p.replace("\\", "/")
    if os.name == "nt":
        norm = norm.lower()
    return norm


def _has_symlink_component(base_dir, target_path):
    """Return True if any path component from user home / `base_dir` to `target_path` is a symlink."""
    home_abs = os.path.abspath(os.path.expanduser("~"))
    target_abs = os.path.abspath(target_path)
    root_abs = home_abs if target_abs.startswith(home_abs + os.sep) else os.path.abspath(base_dir)
    cur = root_abs
    if os.path.islink(cur):
        return True
    rel = os.path.relpath(target_abs, root_abs)
    if rel in (".", ""):
        return False
    for part in rel.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return True
    return False


def _sync_managed_tree(src_dir, dst_dir):
    """
    Selectively and atomically sync regular files from `src_dir` into `dst_dir`
    without touching unmanaged files or following symlinks. Sets 0o755 on .py entrypoints.
    Returns count of updated files.
    """
    updated = 0
    if not os.path.isdir(src_dir) or os.path.islink(src_dir) or os.path.islink(dst_dir):
        return updated
    for root, dirs, files in os.walk(src_dir, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d != "__pycache__"
            and not d.startswith(".")
            and not os.path.islink(os.path.join(root, d))
        ]
        rel_root = os.path.relpath(root, src_dir)
        target_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        if _has_symlink_component(dst_dir, target_root):
            continue
        for fname in files:
            if fname.endswith((".pyc", ".pyo", ".tmp")) or fname.startswith("."):
                continue
            src_file = os.path.join(root, fname)
            if os.path.islink(src_file) or not os.path.isfile(src_file):
                continue
            dst_file = os.path.join(target_root, fname)
            if os.path.islink(dst_file):
                continue
            try:
                with open(src_file, "rb") as sf:
                    src_bytes = sf.read()
                need_write = True
                if os.path.isfile(dst_file):
                    with open(dst_file, "rb") as df:
                        if df.read() == src_bytes:
                            need_write = False
                if need_write:
                    os.makedirs(target_root, exist_ok=True)
                    if _has_symlink_component(dst_dir, target_root) or os.path.islink(dst_file):
                        continue
                    fd, tmp_file = tempfile.mkstemp(
                        prefix=f".{fname}.",
                        suffix=".tmp",
                        dir=target_root,
                    )
                    try:
                        with os.fdopen(fd, "wb") as tf:
                            tf.write(src_bytes)
                        if fname.endswith((".py", ".sh")):
                            os.chmod(tmp_file, 0o755)
                        else:
                            os.chmod(tmp_file, 0o644)
                        os.replace(tmp_file, dst_file)
                        updated += 1
                    except Exception:
                        try:
                            if os.path.exists(tmp_file):
                                os.remove(tmp_file)
                        except Exception:
                            pass
                        raise
                elif fname.endswith((".py", ".sh")) and os.name != "nt":
                    st_mode = os.stat(dst_file).st_mode
                    if not (st_mode & 0o111):
                        os.chmod(dst_file, st_mode | 0o755)
            except Exception as e:
                _log(f"_sync_managed_tree error ({src_file} -> {dst_file}): {e}")
    return updated


def ensure_cli_permissions_allow():
    """
    Ensure ~/.gemini/antigravity-cli/settings.json contains:
      1. wildcard allow rules (`permissions.allow`) so that agy's built-in Auto mode
         classifier defers to this PreToolUse hook (`auto_approve` vs `force_ask`).
      2. `"contextWindow": "1M"` so `pre-compact-wrap.py` can suggest `session-wrap`
         at 60% and 70%, force-run `session-wrap` at 90%, and inject a compaction
         checkpoint right after wrap finishes, while `AGY_CLI_AUTO_COMPACT_OVERRIDE="95%"`
         acts as the hard safety ceiling.
      3. `"statusLine"` HUD configuration pointing to the bundled `agy-statusline.sh`.
    Also ensures ~/.bashrc exports `AGY_CLI_AUTO_COMPACT_OVERRIDE="95%"` on POSIX systems,
    and syncs bundled HUD, skills, and companion plugins.
    """
    plugin_root_dir = os.path.dirname(os.path.abspath(__file__))

    if os.name != "nt":
        for entry in ("permission-reviewer.py", "pre-compact-wrap.py"):
            ep = os.path.join(plugin_root_dir, entry)
            try:
                if os.path.isfile(ep) and not (os.stat(ep).st_mode & 0o111):
                    os.chmod(ep, 0o755)
            except Exception:
                pass

    # Sync bundled HUD script (hud/agy-statusline.sh) before bootstrapping settings.json
    try:
        plugin_hud_dir = os.path.join(plugin_root_dir, "hud")
        global_hud_dir = os.path.expanduser("~/.gemini/antigravity-cli/hud")
        if os.path.isdir(plugin_hud_dir):
            cnt = _sync_managed_tree(plugin_hud_dir, global_hud_dir)
            if cnt > 0:
                _log(f"Synced HUD to {global_hud_dir} ({cnt} files updated)")
            if os.name != "nt":
                local_bin_dir = os.path.expanduser("~/.local/bin")
                cnt_bin = _sync_managed_tree(plugin_hud_dir, local_bin_dir)
                if cnt_bin > 0:
                    _log(f"Synced HUD to {local_bin_dir} ({cnt_bin} files updated)")
    except Exception as e:
        _log(f"ensure_cli_permissions_allow HUD sync error: {e}")

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

        changed = False
        missing = [r for r in REQUIRED_ALLOW_RULES if r not in allow_list]
        if missing:
            allow_list.extend(missing)
            changed = True

        if data.get("contextWindow") in (None, "", "256K", "512K"):
            data["contextWindow"] = "1M"
            changed = True

        status_line = data.get("statusLine")
        if not isinstance(status_line, dict) or not status_line.get("command"):
            if os.name == "nt":
                win_hud_script = os.path.expanduser("~/.gemini/antigravity-cli/hud/agy-statusline.sh")
                default_hud_cmd = f'python "{win_hud_script}"'
            else:
                default_hud_cmd = os.path.expanduser("~/.local/bin/agy-statusline.sh")
            data["statusLine"] = {
                "type": "command",
                "command": default_hud_cmd,
                "stack_with_default": True,
            }
            changed = True

        if changed:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            tmp_path = settings_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, settings_path)
            _log(
                f"Bootstrapped {settings_path}: added_allow={missing}, "
                f"contextWindow={data.get('contextWindow')}, "
                f"statusLine={data.get('statusLine')}"
            )
    except Exception as e:
        _log(f"ensure_cli_permissions_allow settings error: {e}")

    if os.name != "nt":
        bashrc_path = os.path.expanduser("~/.bashrc")
        try:
            if os.path.exists(bashrc_path):
                with open(bashrc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "AGY_CLI_AUTO_COMPACT_OVERRIDE" not in content:
                    with open(bashrc_path, "a", encoding="utf-8") as f:
                        if content and not content.endswith("\n"):
                            f.write("\n")
                        f.write('export AGY_CLI_AUTO_COMPACT_OVERRIDE="95%"\n')
                    _log(f"Bootstrapped AGY_CLI_AUTO_COMPACT_OVERRIDE=95% in {bashrc_path}")
                else:
                    updated_content = re.sub(
                        r'export\s+AGY_CLI_AUTO_COMPACT_OVERRIDE="(?:50|60|70|80|85|90)%"',
                        'export AGY_CLI_AUTO_COMPACT_OVERRIDE="95%"',
                        content,
                    )
                    if updated_content != content:
                        with open(bashrc_path, "w", encoding="utf-8") as f:
                            f.write(updated_content)
                        _log(f"Updated AGY_CLI_AUTO_COMPACT_OVERRIDE to 95% in {bashrc_path}")
        except Exception as e:
            _log(f"ensure_cli_permissions_allow bashrc error: {e}")

    # Sync bundled plugin skills into ~/.gemini/config/skills/
    try:
        plugin_skills_dir = os.path.join(plugin_root_dir, "skills")
        global_skills_dir = os.path.expanduser("~/.gemini/config/skills")
        if os.path.isdir(plugin_skills_dir):
            for skill_name in os.listdir(plugin_skills_dir):
                src_skill = os.path.join(plugin_skills_dir, skill_name)
                dst_skill = os.path.join(global_skills_dir, skill_name)
                if os.path.isdir(src_skill):
                    cnt = _sync_managed_tree(src_skill, dst_skill)
                    if cnt > 0:
                        _log(f"Synced skill {skill_name} to {dst_skill} ({cnt} files updated)")

        # Sync companion plugins (codex-review-gate, opus-review-gate) into ~/.gemini/config/plugins/
        companion_dir = os.path.join(plugin_root_dir, "companion-plugins")
        global_plugins_dir = os.path.expanduser("~/.gemini/config/plugins")
        if os.path.isdir(companion_dir):
            for comp_name in os.listdir(companion_dir):
                src_comp = os.path.join(companion_dir, comp_name)
                dst_comp = os.path.join(global_plugins_dir, comp_name)
                if os.path.isdir(src_comp):
                    cnt = _sync_managed_tree(src_comp, dst_comp)
                    if cnt > 0:
                        _log(f"Synced companion plugin {comp_name} to {dst_comp} ({cnt} files updated)")
                comp_skills_dir = os.path.join(src_comp, "skills")
                if os.path.isdir(comp_skills_dir):
                    for skill_name in os.listdir(comp_skills_dir):
                        src_skill = os.path.join(comp_skills_dir, skill_name)
                        dst_skill = os.path.join(global_skills_dir, skill_name)
                        if os.path.isdir(src_skill):
                            cnt = _sync_managed_tree(src_skill, dst_skill)
                            if cnt > 0:
                                _log(f"Synced companion skill {skill_name} to {dst_skill} ({cnt} files updated)")
    except Exception as e:
        _log(f"ensure_cli_permissions_allow skills/companion sync error: {e}")


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
        return shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return cmd.split()


def check_env_ref(text):
    """Detect references to .env files (excluding .env.example / .env.sample / .env.template)."""
    if not text:
        return False
    cleaned = re.sub(r"\.env\.(example|sample|template|defaults|dist)\b", "", text, flags=re.IGNORECASE)
    return bool(re.search(r"(^|[\s/\\\"'=])\.env(\.[\w-]+)?([\s\"']|$)", cleaned, flags=re.IGNORECASE))


def is_psql_command(tokens):
    for i, t in enumerate(tokens):
        base = re.split(r"[/\\]", t.strip("\"'"))[-1].lower()
        if base in ("psql", "psql.exe"):
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


def _find_claude_bin():
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.local/bin/claude.exe"),
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return "claude"


def _run_agy_print(prompt, env, model=None, timeout_sec=25):
    """
    Invoke `agy` in headless print mode.
    IMPORTANT: `-p` must immediately precede `prompt` (`-p <prompt>`),
    and `stdin` must be `DEVNULL` so `agy` does not read from the hook's stdin pipe.
    """
    agy_bin = _find_agy_bin()
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
        encoding="utf-8",
        errors="replace",
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
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    full_prompt = (
        SYSTEM_PROMPT
        + "\n\nReview this request and respond ONLY with JSON {\"ok\": true} or {\"ok\": false, \"reason\": \"한국어로 작성된 사유\"}:\n\n"
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
        claude_bin = _find_claude_bin()
        claude_cmd = [
            claude_bin,
            "-p",
            "--output-format", "json",
            "--model", claude_cli_model,
            "--no-session-persistence",
            "--append-system-prompt", SYSTEM_PROMPT,
            "Review this request (reason MUST be in Korean):\n\n" + request_info,
        ]
        result = subprocess.run(
            claude_cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
        norm_target = target_file.replace("\\", "/") if isinstance(target_file, str) else ""

        # 4a. .env check (applies to both read and write)
        if check_env_ref(target_file):
            emit_ask("[보안 게이트] 민감 파일(.env) 편집/조회 감지 — 사용자 확인 필요")
            return

        # 4b. Read-only file/directory tools: block sensitive credential dirs, fast-path everything else
        if tool_name in READ_FILE_TOOLS:
            for pat in SENSITIVE_READ_PATTERNS:
                if re.search(pat, norm_target, re.IGNORECASE):
                    emit_ask(f"[보안 게이트] 민감 인증서/키 경로({target_file}) 조회 감지 — 사용자 확인 필요")
                    return
            emit_allow("안전한 파일/디렉토리 조회 고속 통과")
            return

        # 4c. Write/Edit tools: check sensitive OS/credential/shell-init paths
        for pat in SENSITIVE_PATH_PATTERNS:
            if re.search(pat, norm_target, re.IGNORECASE):
                emit_ask(f"[보안 게이트] 시스템/인증서 경로({target_file}) 편집 감지 — 사용자 확인 필요")
                return

        # 4d. Fast-path for Write/Edit: internal brain artifacts/scratch or safe workspace paths
        norm_cmp = _norm_path(target_file)
        is_brain = (
            "/.gemini/antigravity-cli/brain/" in norm_cmp
            or "/.gemini/antigravity/brain/" in norm_cmp
            or "/.gemini/antigravity-ide/brain/" in norm_cmp
        )
        is_in_workspace = any(
            norm_cmp.startswith(_norm_path(ws).rstrip("/") + "/") or norm_cmp == _norm_path(ws).rstrip("/")
            for ws in workspace_paths
            if isinstance(ws, str) and ws.strip()
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
