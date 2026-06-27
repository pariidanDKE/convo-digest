#!/usr/bin/env python3
"""transcript.py — deterministic reduction of one Claude Code conversation.

Given a transcript .jsonl, produce:
  - exchanges: vertically-stripped user->assistant exchanges (no thinking / tool
    I/O; images & documents replaced with markers), with timestamps and the idle
    gap to the next exchange.
  - facets: structured search keys mined from the FULL transcript (including the
    tool I/O we then drop) — files, dirs, languages, commands, tools, errors,
    project, cwd, git branch, timespan, counts.

Pure stdlib, no model calls. This is the substrate the sampler/summarizer slice
(see sampling-spec.md §"Navigation tools" and SPEC.md §4.1–4.2).
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

# tool_use input keys that name a file
FILE_PATH_KEYS = ("file_path", "path", "notebook_path")

LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".cs": "csharp", ".php": "php", ".sql": "sql", ".sh": "shell", ".bash": "shell",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".html": "html", ".css": "css", ".scss": "css", ".cpp": "cpp", ".cc": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".r": "r", ".lua": "lua", ".dockerfile": "docker",
}

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROG_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*$")
_SHELL_OPS = {"|", "||", "&&", ";", "|&", "&"}
_SHELL_KEYWORDS = {"for", "while", "if", "then", "else", "elif", "fi", "do", "done",
                   "case", "esac", "until", "select", "function", "time", "exec"}

# Shell builtins / coreutils — plumbing, not signal. Filtered from the `commands`
# facet so only meaningful programs (git, python, docker, pytest, …) survive.
_SHELL_BUILTINS = {
    "cd", "ls", "cat", "echo", "pwd", "printf", "head", "tail", "grep", "egrep",
    "fgrep", "rg", "find", "fd", "sed", "awk", "sort", "uniq", "wc", "cut", "tr",
    "xargs", "tee", "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "chown",
    "ln", "kill", "pkill", "pgrep", "ps", "lsof", "sleep", "true", "false", "test",
    "export", "unset", "source", "which", "type", "command", "eval", "basename",
    "dirname", "realpath", "readlink", "stat", "du", "df", "less", "more", "diff",
    "date", "env", "set", "read", "seq", "watch", "clear", "history", "wait",
    "open", "code", "man", "whoami", "hostname", "uname", "yes", "nohup", "tmux",
    "screen", "expr", "let", "shift", "trap", "exit", "return", "alias", "nl",
    "column", "paste", "join", "comm", "tac", "rev", "split", "od", "xxd",
}

# Junk "errors" — exit codes, permission rejections, empty markers. Not failures
# worth indexing; filtered from the `errors` facet.
_ERROR_JUNK = re.compile(
    r"^(exit code\b|###\s*error\s*$)|doesn't want to proceed|tool use was rejected",
    re.IGNORECASE,
)


def _clean_errors(errors: list[str]) -> list[str]:
    """Drop junk error markers + dedupe, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for e in errors:
        s = (e or "").strip()
        if not s or _ERROR_JUNK.search(s) or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _programs(cmd: str) -> list[str]:
    """Program names invoked by a bash command. Shell-aware: tokenizes with shlex
    (so quotes/heredoc bodies stay intact), skips env-var assignments, and treats a
    token as a program only when it's first or immediately after a pipe/&&/;.
    'FOO=1 python x.py | grep y' -> ['python', 'grep']."""
    try:
        toks = shlex.split(cmd, posix=True, comments=True)
    except ValueError:  # unbalanced quotes / heredocs — fall back to first token
        first = cmd.strip().splitlines()[0].split() if cmd.strip() else []
        toks = first[:1]
    progs: list[str] = []
    expect = True  # the next non-assignment token starts a command
    for tok in toks:
        if tok in _SHELL_OPS:
            expect = True
            continue
        if not expect:
            continue
        if _ENV_ASSIGN.match(tok):  # skip leading VAR=val, still expecting the program
            continue
        expect = False
        p = os.path.basename(tok) if "/" in tok else tok
        if _PROG_OK.match(p) and p not in _SHELL_KEYWORDS:
            progs.append(p)
    return progs


@dataclass
class Exchange:
    """One user->assistant exchange, vertically stripped.

    The assistant side may span many agentic turns (tool calls, etc.) between two
    real user turns; we concatenate only its *visible text* and record which tools
    were used along the way.
    """
    index: int
    user_text: str
    assistant_text: str          # all assistant visible text in this exchange
    final_assistant_text: str    # last non-empty assistant text block (tail emphasis)
    tools: list[str]
    start_ts: Optional[str]
    end_ts: Optional[str]
    gap_to_next_sec: Optional[float] = None

    @property
    def chars(self) -> int:
        return len(self.user_text) + len(self.assistant_text)


@dataclass
class Facets:
    project: Optional[str] = None
    cwd: Optional[str] = None
    git_branch: Optional[str] = None                    # first branch seen (back-compat)
    git_branches: list[str] = field(default_factory=list)  # distinct branches across the convo
    languages: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)   # program (first token of a bash command)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)     # short first-line error markers
    message_count: int = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None


@dataclass
class Transcript:
    path: str
    exchanges: list[Exchange]
    facets: Facets


# --- block helpers --------------------------------------------------------

def _blocks(content: Any) -> Iterator[dict]:
    """Yield content blocks. A plain string becomes a single text block."""
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                yield b


def _visible_text(content: Any) -> list[str]:
    """Visible text of a message: text blocks kept, media -> markers, rest dropped."""
    out: list[str] = []
    for b in _blocks(content):
        bt = b.get("type")
        if bt == "text":
            txt = b.get("text") or ""
            if txt:
                out.append(txt)
        elif bt == "image":
            out.append("[image]")
        elif bt == "document":
            src = b.get("source") or {}
            name = b.get("title") or (src.get("file_id") if isinstance(src, dict) else "") or ""
            out.append(f"[document: {name}]" if name else "[document]")
    return out


def _is_real_user_turn(content: Any) -> bool:
    """True if this user message carries actual user input (not only tool_result)."""
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            for b in content
        )
    return False


# Harness-injected payloads that arrive wearing type:"user" but were NOT authored
# by the user (task completions, session reminders, slash-command output). They
# look exactly like real input to _is_real_user_turn, so they must be filtered
# explicitly — otherwise a background task notifying back into a dormant convo
# advances last_ts and re-triggers a summary (issue #2 follow-up), and the payload
# pollutes the exchange stream fed to the summarizer.
_HARNESS_TAGS = (
    "<task-notification>", "<system-reminder>", "<local-command-stdout>",
    "<command-message>", "<command-name>", "<command-args>",
)


def _is_synthetic_user(content: Any) -> bool:
    """True for a harness-injected user message (see _HARNESS_TAGS).

    A genuine turn that merely *carries* an appended reminder alongside real text
    is NOT synthetic — only messages whose entire visible text is harness payload.
    """
    def _starts(s: str) -> bool:
        return s.lstrip().startswith(_HARNESS_TAGS)

    if isinstance(content, str):
        return _starts(content)
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return bool(texts) and all(_starts(t) for t in texts)
    return False


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --- main parse -----------------------------------------------------------

def parse_transcript(path: str, include_sidechains: bool = False) -> Transcript:
    facets = Facets(project=os.path.basename(os.path.dirname(path)))
    files: set[str] = set()
    dirs: set[str] = set()
    langs: set[str] = set()
    commands: set[str] = set()
    tools: set[str] = set()
    errors: list[str] = []
    branches: set[str] = set()

    exchanges: list[Exchange] = []
    current: Optional[Exchange] = None

    def mine_tool_use(b: dict) -> None:
        name = (b.get("name") or "").strip()
        if name:
            tools.add(name)
        inp = b.get("input")
        if not isinstance(inp, dict):
            return
        for k in FILE_PATH_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                files.add(v)
                d = os.path.dirname(v)
                if d:
                    dirs.add(d)
                ext = os.path.splitext(v)[1].lower()
                if ext in LANG_BY_EXT:
                    langs.add(LANG_BY_EXT[ext])
        cmd = inp.get("command")
        if name.lower().startswith("bash") and isinstance(cmd, str) and cmd.strip():
            commands.update(_programs(cmd))

    def mine_tool_result(b: dict) -> None:
        content = b.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(s.get("text", "") for s in content if isinstance(s, dict))
        if b.get("is_error") and text.strip():
            errors.append(text.strip().splitlines()[0][:200])

    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue

            # top-level facets (first non-null wins for cwd/branch; ts brackets the span)
            if facets.cwd is None and o.get("cwd"):
                facets.cwd = o["cwd"]
            if o.get("gitBranch"):
                if facets.git_branch is None:
                    facets.git_branch = o["gitBranch"]
                branches.add(o["gitBranch"])
            if o.get("type") not in ("user", "assistant"):
                continue
            if o.get("isSidechain") and not include_sidechains:
                continue
            msg = o.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            # Drop harness-injected synthetic user messages before they can affect
            # anything: they wear type:"user" but were not authored by the user, so
            # they must neither advance the watermark nor become exchanges (issue #2
            # follow-up — a non-content type filter alone misses these).
            if role == "user" and _is_synthetic_user(content):
                continue
            # Watermark (first_ts/last_ts) advances ONLY on genuine content turns —
            # AFTER the type, sidechain, and synthetic filters. Otherwise trailing
            # non-content events (queue-operation, task-notification, attachments)
            # move last_ts and trip the change-detector into re-summarizing a
            # dormant convo (issue #2).
            ts = o.get("timestamp")
            if ts:
                if facets.first_ts is None:
                    facets.first_ts = ts
                facets.last_ts = ts

            # mine tool I/O before the strip discards it
            for b in _blocks(content):
                bt = b.get("type")
                if bt == "tool_use":
                    mine_tool_use(b)
                elif bt == "tool_result":
                    mine_tool_result(b)

            if role == "user" and _is_real_user_turn(content):
                if current is not None:
                    exchanges.append(current)
                current = Exchange(
                    index=len(exchanges),
                    user_text="\n".join(_visible_text(content)).strip(),
                    assistant_text="", final_assistant_text="", tools=[],
                    start_ts=ts, end_ts=ts,
                )
            elif role == "assistant":
                if current is None:  # assistant before any user turn (rare)
                    current = Exchange(index=0, user_text="", assistant_text="",
                                       final_assistant_text="", tools=[], start_ts=ts, end_ts=ts)
                texts = _visible_text(content)
                if texts:
                    joined = "\n".join(texts)
                    current.assistant_text += ("\n" if current.assistant_text else "") + joined
                    if joined.strip():
                        current.final_assistant_text = joined
                current.tools.extend(
                    (b.get("name") or "") for b in _blocks(content) if b.get("type") == "tool_use"
                )
                current.end_ts = ts
            # user-with-only-tool_result -> part of the middle; content already mined, now dropped

    if current is not None:
        exchanges.append(current)

    # idle gap to the next exchange
    for i, ex in enumerate(exchanges[:-1]):
        a = _parse_ts(ex.end_ts)
        b = _parse_ts(exchanges[i + 1].start_ts)
        if a and b:
            ex.gap_to_next_sec = (b - a).total_seconds()

    facets.message_count = len(exchanges)
    facets.git_branches = sorted(branches)
    facets.files = sorted(files)[:50]
    facets.dirs = sorted(dirs)[:30]
    facets.languages = sorted(langs)
    facets.commands = sorted(c for c in commands if c.lower() not in _SHELL_BUILTINS)[:30]
    facets.tools = sorted(tools)
    facets.errors = _clean_errors(errors)[:20]
    return Transcript(path=path, exchanges=exchanges, facets=facets)


# --- CLI (structure only — never prints conversation content) -------------

def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: transcript.py <transcript.jsonl>")
        return 2
    tr = parse_transcript(argv[0])
    f = tr.facets
    print(f"file       : {os.path.basename(tr.path)}")
    print(f"project    : {f.project}")
    print(f"exchanges  : {len(tr.exchanges)}")
    print(f"timespan   : {f.first_ts} -> {f.last_ts}")
    print(f"languages  : {f.languages}")
    print(f"tools      : {f.tools}")
    print(f"commands   : {f.commands}")
    print(f"dirs ({len(f.dirs)}) : {f.dirs[:8]}")
    print(f"files ({len(f.files)}): {[os.path.basename(x) for x in f.files[:8]]}")
    print(f"errors     : {len(f.errors)}")
    print("--- exchange structure (char counts only, no content) ---")
    for ex in tr.exchanges[:15]:
        gap = f"{ex.gap_to_next_sec/60:.0f}m" if ex.gap_to_next_sec else "-"
        print(f"  #{ex.index:>3}  user={len(ex.user_text):>6}c  "
              f"asst={len(ex.assistant_text):>7}c  tools={len(ex.tools):>3}  gap_next={gap}")
    if len(tr.exchanges) > 15:
        print(f"  ... (+{len(tr.exchanges) - 15} more)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
