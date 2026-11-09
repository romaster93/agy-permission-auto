#!/usr/bin/env python3
import json
import os
import sys

# claude-usage-monitor reads agy's quota from a verbatim copy of this stdin.
STATUS_COPY = os.path.expanduser(
    "~/.gemini/antigravity-cli/hud/cache/last_stdin.json")
SESSIONS_CACHE_DIR = os.path.expanduser(
    "~/.gemini/antigravity-cli/hud/cache/sessions")


def fmt_tokens(n):
    if not isinstance(n, (int, float)):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def fmt_duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def make_bar(pct, width=10):
    pct = max(0.0, min(100.0, float(pct or 0.0)))
    filled = int(round((pct / 100.0) * width))
    return "█" * filled + "░" * (width - filled)


def color_by_pct(pct, invert=False):
    val = (100.0 - float(pct)) if invert else float(pct)
    if val >= 80:
        return "\033[38;5;203m"  # Red
    if val >= 50:
        return "\033[38;5;221m"  # Yellow
    return "\033[38;5;114m"      # Green


RESET = "\033[0m"
DIM = "\033[38;5;245m"
CYAN = "\033[38;5;117m"
MAGENTA = "\033[38;5;183m"


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return
        data = json.loads(raw)
    except Exception:
        return

    try:
        os.makedirs(os.path.dirname(STATUS_COPY), exist_ok=True)
        tmp = f"{STATUS_COPY}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(tmp, STATUS_COPY)

        conv_id = str(data.get("conversation_id") or data.get("session_id") or "").strip()
        if conv_id and all(c.isalnum() or c in "-_" for c in conv_id):
            os.makedirs(SESSIONS_CACHE_DIR, exist_ok=True)
            conv_path = os.path.join(SESSIONS_CACHE_DIR, f"{conv_id}.json")
            conv_tmp = f"{conv_path}.{os.getpid()}.tmp"
            with open(conv_tmp, "w", encoding="utf-8") as f:
                f.write(raw)
            os.replace(conv_tmp, conv_path)
    except OSError:
        pass

    parts = []

    # 1. Model info
    model_info = data.get("model") or {}
    model_name = (
        model_info.get("display_name")
        or model_info.get("id")
        or ""
    )
    if model_name:
        parts.append(f"{MAGENTA}◆ {model_name}{RESET}")

    # 2. Context Window usage
    ctx = data.get("context_window") or {}
    if ctx:
        used_pct = ctx.get("used_percentage")
        in_tok = ctx.get("total_input_tokens", 0)
        out_tok = ctx.get("total_output_tokens", 0)
        win_size = ctx.get("context_window_size", 0)

        if used_pct is None and win_size:
            used_pct = ((in_tok + out_tok) / win_size) * 100.0
        used_pct = float(used_pct or 0.0)

        c = color_by_pct(used_pct, invert=False)
        bar = make_bar(used_pct, width=8)
        tok_str = f"In:{fmt_tokens(in_tok)} Out:{fmt_tokens(out_tok)}"
        if win_size:
            tok_str += f"/{fmt_tokens(win_size)}"
        parts.append(f"{CYAN}Ctx{RESET} {c}[{bar}] {used_pct:.1f}%{RESET} {DIM}({tok_str}){RESET}")

    # 3. Quota info
    quota = data.get("quota") or {}
    if isinstance(quota, dict) and quota:
        q_items = []
        for bucket, qinfo in quota.items():
            if not isinstance(qinfo, dict):
                continue
            rem_frac = qinfo.get("remaining_fraction")
            if rem_frac is None:
                continue
            rem_pct = float(rem_frac) * 100.0
            c = color_by_pct(rem_pct, invert=True)
            reset_sec = qinfo.get("reset_in_seconds")
            reset_str = fmt_duration(reset_sec)
            label = bucket.replace("gemini-", "")
            item = f"{label}: {c}{rem_pct:.1f}%{RESET}"
            if reset_str:
                item += f" {DIM}({reset_str}){RESET}"
            q_items.append(item)
        if q_items:
            parts.append(f"{CYAN}Quota{RESET} " + " · ".join(q_items))

    if parts:
        print(f" {DIM}│{RESET} ".join(parts))


if __name__ == "__main__":
    main()
