#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""wombat-gate guard: a Claude Code PreToolUse hook for the Bash tool.

Many agents share one working tree. This blocks the shell commands that sweep
up or throw away other agents' uncommitted work, and the ways of switching off
wombat-gate's "this is an agent" check. Exit 2 = block (stderr goes back to the
agent); exit 0 = allow.

It splits the command the way a shell would (;, &&, ||, |, &, newlines,
redirections, `bash -c '...'`, `$(...)`, heredoc bodies dropped) and then looks
at git's real argument structure (global options, short-option clusters,
pathspecs), instead of pattern-matching the raw text. It is still a guard rail
for cooperating agents, not a sandbox: it cannot see inside scripts, aliases or
`python -c`, and an agent determined to get round it will.

Usage: python3 guard.py [--agent-only] < hook-input.json
Self-test: hooks/selftest_guard.sh
"""
import json
import os
import re
import shlex
import sys

OVERRIDE = "WOMBAT_HUMAN_OVERRIDE"
MARKER = "CLAUDECODE"

# words that just run the rest of the line as a command, with their options that take a value
PREFIX_CMDS = {"sudo": "ugCDhprtUT", "command": "", "nohup": "", "time": "fo", "exec": "a", "nice": "n",
               "stdbuf": "ioe", "timeout": "sk", "xargs": "IiLlnPsdEa", "builtin": "", "setsid": ""}
# shell words that can stand in front of a command (`if git ...`, `then git ...`)
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "time"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "{", "}", "!"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=")
BROAD = re.compile(r"^(\.|\./|\.\.?(/\.\.)*(/\.?)?|:/?.*|:[!^].*|:\(.*|\*|\*\*)$")

# git global options that take a value as the next argument
GIT_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                         "--super-prefix", "--config-env", "--attr-source"}


def fail(why):
    print("wombat-gate guard: blocked %s. Several agents share this working tree; commit only your "
          "own paths with: wombat-gate commit -m \"...\" -- <your paths>. If you really need this, "
          "stop and ask the human." % why, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- shell splitting

def strip_heredocs(text):
    """Drop heredoc bodies (they are data, not commands)."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = re.findall(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        i += 1
        for _, tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1  # the terminator line
    return "\n".join(out)


def strip_comments(text):
    """Remove `# ...` comments: a # that starts a word, outside quotes (not `a#b`, not "#12")."""
    out, i, q, n = [], 0, None, len(text)
    while i < n:
        c = text[i]
        if q:
            out.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c == "\\" and i + 1 < n:
            out.append(c + text[i + 1])
            i += 1
        elif c in "'\"":
            q = c
            out.append(c)
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def inner_commands(text):
    """Contents of $(...) and `...` (one level; nested ones are found recursively)."""
    found, i = [], 0
    while i < len(text):
        if text.startswith("$(", i) and not text.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            found.append(text[i + 2:j - 1])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            if j < 0:
                break
            found.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return found


def simple_commands(text, depth=0):
    """Yield argv lists of the simple commands in a shell command string."""
    if depth > 4:
        return
    text = strip_comments(strip_heredocs(text.replace("\\\n", " ")))
    for sub in inner_commands(text):
        yield from simple_commands(sub, depth + 1)
    lex = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""  # comments were removed above, quote-aware
    try:
        tokens = list(lex)
    except ValueError:
        # unbalanced quotes: bash would refuse the command too; look at raw words
        tokens = text.replace("\n", " ; ").split()
    cur, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur:
                yield cur
            cur = []
        elif t and set(t) <= set("<>&|"):
            # a redirection operator: drop its target, and a file-descriptor
            # number written right before it (`2>&1`, `2> err.log`)
            if cur and cur[-1].isdigit():
                cur.pop()
            i += 1
        else:
            cur.append(t)
        i += 1
    if cur:
        yield cur


def unwrap(argv, depth=0, fed=False):
    """Strip env assignments / wrappers; recurse into `bash -c`, `eval`.

    Yields ("assign", [...]), ("env", argv) and ("cmd", argv, fed); fed=True when
    the command's arguments come from xargs / find -exec rather than the text."""
    if depth > 8:
        fail("a command nested too deeply to check")
    while argv and argv[0] in KEYWORDS:
        argv = argv[1:]
    assigns = []
    while argv and ASSIGN.match(argv[0]):
        assigns.append(argv[0])
        argv = argv[1:]
    yield ("assign", assigns)
    if not argv:
        return
    name = os.path.basename(argv[0])
    if name == "env":
        yield ("env", argv)
        rest, i = argv[1:], 0
        while i < len(rest) and (rest[i].startswith("-") or ASSIGN.match(rest[i])):
            if rest[i] in ("-u", "-C", "-S", "--unset", "--chdir", "--split-string"):
                i += 1
            if ASSIGN.match(rest[i]) if i < len(rest) else False:
                assigns.append(rest[i])
            i += 1
        yield ("assign", assigns)
        yield from unwrap(rest[i:], depth + 1, fed)
        return
    if name == "exec" and any(a.startswith("-") and not a.startswith("--") and "c" in a[1:]
                              for a in argv[1:] if a != "--"):
        fail("exec -c (an empty environment hides %s)" % MARKER)
    if name in PREFIX_CMDS:
        rest, takes = argv[1:], PREFIX_CMDS[name]
        while rest and rest[0].startswith("-") and rest[0] != "-":
            opt = rest[0]
            rest = rest[1:]
            if opt == "--":
                break
            if not opt.startswith("--") and opt[-1] in takes and rest:
                rest = rest[1:]  # its value is the next word
        if name == "timeout" and rest:
            rest = rest[1:]  # the duration
        yield from unwrap(rest, depth + 1, fed or name == "xargs")
        return
    if name == "find":
        for k, a in enumerate(argv):
            if a in ("-exec", "-execdir", "-ok", "-okdir") and k + 1 < len(argv):
                yield from unwrap([t for t in argv[k + 1:] if t not in (";", "+", "\\;")], depth + 1, True)
        return
    if name in SHELLS or name == "eval":
        script = None
        if name == "eval":
            script = " ".join(argv[1:])
        elif "-c" in argv[1:] or any(a.startswith("-") and "c" in a[1:] and not a.startswith("--") for a in argv[1:]):
            for j, a in enumerate(argv[1:], 1):
                if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:]):
                    if j + 1 < len(argv):
                        script = argv[j + 1]
                    break
        if script is not None and depth < 4:
            for sub in simple_commands(script, depth + 1):
                yield from unwrap(sub, depth + 1, fed)
            return
    yield ("cmd", argv, fed)


# ---------------------------------------------------------------- git rules

def parse_opts(args, value_longs=(), value_shorts=""):
    """Split git subcommand args into (short letters, long options, paths).

    Values of options that take one are skipped (`-m "-a"` is a message, not -a);
    everything after `--` is a path."""
    shorts, longs, paths, i = set(), [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            paths += args[i + 1:]
            break
        if a.startswith("--"):
            longs.append(a)
            if "=" not in a and any(is_long(a, v) for v in value_longs):
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                shorts.add(ch)
                if ch in value_shorts:
                    if k == len(a) - 2:
                        i += 1  # value is the next argument
                    break      # otherwise the value is the rest of this word
        else:
            paths.append(a)
        i += 1
    return shorts, longs, paths


def is_long(arg, full, minimum=2):
    """`arg` is `full` or a unique-looking prefix git would accept (at least `minimum` chars after --)."""
    a = arg.split("=", 1)[0]
    return a == full or (a.startswith("--") and len(a) >= 2 + minimum and full.startswith(a))


def split_git(argv):
    """-> (subcommand, args) after git's global options; (None, []) if no subcommand."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in GIT_GLOBAL_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return None, []


def broad(paths):
    return any(BROAD.match(p) for p in paths)


def no_generated(sub, paths):
    if generated(paths):
        fail("git %s with paths generated by the shell (name your paths explicitly)" % sub)


def generated(paths):
    """Paths the shell computes (`$(...)`, backticks, $PWD): we cannot see what they are."""
    return any(p == "$" or p.startswith("$(") or "`" in p or p in ("$PWD", "${PWD}") for p in paths)


WRITES = {"add", "commit", "rm", "checkout", "restore", "clean", "reset", "stash", "mv"}


def check_git(argv, fed=False):
    sub, args = split_git(argv)
    if sub is None:
        return
    if fed and sub in WRITES:
        fail("git %s with paths fed by xargs / find -exec (name your paths explicitly)" % sub)
    if sub in ("pull", "merge", "rebase"):
        fail("git %s (it rewrites the tree under everyone)" % sub)
    if sub == "checkout-index" or (sub == "read-tree" and any(a in ("-u", "--reset") for a in args)):
        fail("git %s (it overwrites working-tree files)" % sub)

    if sub == "add":
        shorts, longs, paths = parse_opts(args, ("--chmod", "--pathspec-from-file"))
        no_generated(sub, paths)
        if any(is_long(a, "--pathspec-from-file", 2) for a in longs):
            fail("git add --pathspec-from-file (name your paths explicitly)")
        whole = {"A", "u"} & shorts or any(is_long(a, "--all", 1) or is_long(a, "--update", 1) for a in longs)
        if broad(paths) or (whole and not paths):
            fail("a whole-tree git add")
    elif sub == "commit":
        shorts, longs, paths = parse_opts(args, ("--message", "--file", "--author", "--date", "--reuse-message",
                                                 "--reedit-message", "--fixup", "--squash", "--template",
                                                 "--cleanup", "--trailer", "--pathspec-from-file"), "mFcCt")
        no_generated(sub, paths)
        if "a" in shorts or any(a == "--all" for a in longs):
            fail("git commit -a (it takes every tracked change, including other agents')")
        if "i" in shorts or any(is_long(a, "--include", 3) for a in longs):
            fail("git commit -i (it takes everything already staged, including other agents' work)")
        if any(is_long(a, "--amend", 2) for a in longs):
            fail("git commit --amend (it rewrites the last commit, which may be someone else's)")
        if not paths and not any(is_long(a, "--dry-run") for a in longs):
            fail("git commit without paths (it takes everything anyone has staged)")
        if broad(paths):
            fail("a whole-tree git commit")
    elif sub == "stash":
        rest = [a for a in args if not a.startswith("-")]
        if not rest or rest[0] not in ("list", "show"):
            fail("git stash (it takes or drops everyone's uncommitted changes)")
    elif sub == "reset":
        shorts, longs, paths = parse_opts(args, ("--pathspec-from-file",))
        no_generated(sub, paths)
        if any(is_long(a, f) for a in longs for f in ("--hard", "--merge", "--keep")):
            fail("git reset --hard / --merge / --keep")
        if "--" not in args:
            fail("git reset without `--` and paths (it unstages everyone's work or moves the branch)")
        if broad(args[args.index("--") + 1:]) or not args[args.index("--") + 1:]:
            fail("a whole-tree git reset")
    elif sub in ("checkout", "restore"):
        shorts, longs, paths = parse_opts(args, ("--source", "--conflict", "--pathspec-from-file"),
                                          "sbB" if sub == "checkout" else "s")
        no_generated(sub, paths)
        if "f" in shorts or any(is_long(a, "--force", 1) for a in longs):
            fail("git %s --force" % sub)
        if broad(paths):
            fail("a whole-tree git %s" % sub)
        if sub == "checkout" and "--" not in args:
            fail("git checkout without `--` (switching branches changes the tree under everyone)")
        if not paths:
            fail("git %s without paths" % sub)
    elif sub == "switch":
        fail("git switch (switching branches changes the tree under everyone)")
    elif sub == "clean":
        shorts, longs, paths = parse_opts(args, ("--exclude",), "e")
        no_generated(sub, paths)
        if ("f" in shorts or any(is_long(a, "--force", 1) for a in longs)) and (not paths or broad(paths)):
            fail("a whole-tree git clean")
    elif sub == "rm":
        shorts, longs, paths = parse_opts(args, ("--pathspec-from-file",))
        no_generated(sub, paths)
        if any(is_long(a, "--pathspec-from-file", 2) for a in longs):
            fail("git rm --pathspec-from-file (name your paths explicitly)")
        if broad(paths):
            fail("a whole-tree git rm")


# ---------------------------------------------------------------- agent-check rules

def check_assigns(assigns):
    for a in assigns:
        name = a.split("=", 1)[0].split("[", 1)[0].rstrip("+")
        if name == OVERRIDE:
            fail("%s (the human-only switch for wombat-gate's escape hatches)" % OVERRIDE)
        if name == MARKER:
            fail("overwriting %s (it tells wombat-gate this is an agent)" % MARKER)


def check_env(argv):
    rest, i = argv[1:], 0
    while i < len(rest):
        a = rest[i]
        nxt = rest[i + 1] if i + 1 < len(rest) else ""
        if a == "--" or (not a.startswith("-") and not ASSIGN.match(a)):
            break  # the command env runs starts here
        if a == "-" or is_long(a, "--ignore-environment"):
            fail("env -i (an empty environment hides %s)" % MARKER)
        if is_long(a, "--unset"):
            target = a.split("=", 1)[1] if "=" in a else nxt
            if target == MARKER:
                fail("env --unset %s" % MARKER)
            i += 0 if "=" in a else 1
        elif a.startswith("-") and not a.startswith("--"):
            for k, ch in enumerate(a[1:]):
                if ch == "i":
                    fail("env -i (an empty environment hides %s)" % MARKER)
                if ch in "uCS":  # these take a value: rest of the word, or the next one
                    value = a[k + 2:] or nxt
                    if ch == "u" and value == MARKER:
                        fail("env -u %s" % MARKER)
                    if not a[k + 2:]:
                        i += 1
                    break
        i += 1


def check_builtin(argv):
    name = argv[0]
    args = argv[1:]
    if name == "printf" and MARKER in args and any(a.startswith("-") and "v" in a for a in args):
        fail("printf -v %s" % MARKER)
    if name in ("read", "readarray", "mapfile") and MARKER in args:
        fail("%s into %s" % (name, MARKER))
    if name == "unset" and MARKER in args:
        fail("unset %s" % MARKER)
    if name in ("export", "declare", "typeset", "local", "readonly"):
        flags = [a for a in args if a.startswith(("-", "+"))]
        names = [a.split("=", 1)[0] for a in args if not a.startswith(("-", "+"))]
        if OVERRIDE in names:
            fail("%s (the human-only switch for wombat-gate's escape hatches)" % OVERRIDE)
        if MARKER in names and (any("n" in f[1:] for f in flags if f.startswith("-")) or any("x" in f[1:] for f in flags if f.startswith("+"))
                                or any(a.startswith(MARKER + "=") for a in args)):
            fail("un-exporting or overwriting %s" % MARKER)


def main():
    # --agent-only: only the checks that protect wombat-gate's agent gate (for
    # projects that keep their own git rules and add just these)
    agent_only = "--agent-only" in sys.argv[1:]
    raw = sys.stdin.read()
    try:
        cmd = json.loads(raw)["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        cmd = raw
    if not isinstance(cmd, str):
        return 0
    for argv in simple_commands(cmd):
        for kind, item, *fed in unwrap(argv):
            if kind == "assign":
                check_assigns(item)
            elif kind == "env":
                check_env(item)
            elif kind == "cmd" and item:
                check_builtin(item)
                if not agent_only and os.path.basename(item[0]).lstrip("\\") == "git":
                    check_git(item, fed[0])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        if "--agent-only" in sys.argv[1:]:
            # embedded in another project's hook: a broken guard must not block every
            # Bash call of every session; warn and let the host hook's own rules decide
            print("wombat-gate guard (--agent-only): internal error %s; check skipped" % type(e).__name__,
                  file=sys.stderr)
            sys.exit(0)
        # standalone: an exit code other than 0/2 would let the command through, so block
        print("wombat-gate guard: could not parse this command (%s); blocked to be safe. "
              "Simplify the command or ask the human." % type(e).__name__, file=sys.stderr)
        sys.exit(2)
