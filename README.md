# 🕳️ wombat-gate

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-banner.jpg" width="100%"
       alt="wombat-gate banner illustration">
</p>

**One at a time through the burrow.** A commit queue for many AI agents (or
humans) sharing one git working tree. ([中文简介](https://github.com/liyifreddy/wombat-gate/blob/main/README.zh-CN.md))

## 😩 The problem

You run several Claude Code sessions (or any agents) in **one** checkout. Then:

- 🧺 **Commits sweep up other agents' files.** `git add -A`, `git commit -a` or a bare
  `git commit` takes whatever is staged, including a neighbour's half-finished work. We
  had two such commits in two days before writing this tool.
- 🥊 **Agents fight over git.** Every index write takes `.git/index.lock`; the second
  writer fails with `index.lock: File exists`, retries on its own schedule, and the
  retries trample each other's index. In our self-test, 8 plain-git writers × 15 commits
  landed only 1–10 of 120 commits; the rest died on the lock.
- 🐢 **Every commit scans the whole tree.** `git commit` first refreshes the index: one
  `lstat()` per tracked file. On a 22k-file repository on a 9p-mounted Windows drive (WSL)
  that is tens of thousands of 9p requests and 30–70 s per commit; in a burst the queue of
  agents waited up to 6 minutes. Each 9p request needs a contiguous chunk of kernel
  memory, so when memory runs low those whole-tree scans are what fail first — and the
  mount drops for every session (that was the root cause of our drive dropping out).
- 💣 **Agents reach for whole-tree commands** — `git add -A`, `git stash`,
  `git reset --hard`, `git checkout -- .` — and throw away or take other sessions' work.

## ✨ What you get

- ✅ **Only your own paths** — `wombat-gate commit -m MSG -- PATH...` commits exactly
  those paths; what others staged stays staged and out of your commit.
- ✅ **No more fights over git** — writers wait in line for one local `flock` instead of
  failing and retrying into each other's index. Self-test: 120 of 120 commits land;
  median queue wait 0.14 s and lock hold 0.026 s on an idle machine (0.9 s / 0.16 s with
  ten other agent sessions running).
- ✅ **No whole-tree scan** — the commit is built in a private copy of the index and only
  your paths are grafted onto `HEAD`, so git never stats the rest of the tree: on that
  22k-file 9p repository the lock hold went from 30–70 s to 3.07 s in the first real
  commit after the switch (one measurement; "a few seconds" in general).
- ✅ **A guard hook** for Claude Code that blocks whole-tree git commands (staging,
  committing, stash, reset, checkout, clean, …) — 197 test commands it must block or pass.
- ✅ **Never deletes git's lock files** — if a git process outside the queue holds one,
  wombat-gate backs off and retries.
- ✅ **Honest reports** — the sha and file list come from the commit it actually made,
  not from `HEAD`.
- ✅ **A timing log** — one JSON line per operation: queue wait, lock hold, each git step.
- ✅ **One-step install for Claude Code** — a plugin with the CLI, the guard hook and a
  skill that teaches the agent the rules. Checked against real `git commit` on every
  run of the self-test, plus 36 re-introduced past bugs that must each turn a check red.

## 🕳️ How it works

Wombats are lovely, stubborn, and built like a door. When something chases a
wombat, it dives into its burrow and plugs the entrance with its backside: a
hard plate of cartilage and bone that no fox can get past. One wombat in the
tunnel, everyone else waits outside. (They also leave neat little cubes of poo
on rocks to say *this is mine*. We think that is the right attitude towards your
own directory.)

**One wombat in the burrow at a time = one session writing to git at a time.**
Many agents can work in one checkout; they queue, and only one at a time gets
into `.git`:

```
  agent A ──┐                                      ┌──▶ commit A's paths only
  agent B ──┼──▶ queue ──▶ take lock ──▶ write ────┤
  agent C ──┘    (wait)    (flock on a             └──▶ release lock ──▶ next in line
                            local disk)
```

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-how-it-works-captioned.jpg" width="100%"
       alt="Four panels: robots queue at the burrow; one goes in with the wombat; inside it puts its crate on its own shelf; it comes out and the next one goes in">
</p>

1. **Wait in queue** — every agent that wants to write lines up outside the burrow.
2. **Take the lock (flock, local disk)** — one at a time goes in; the lock is a file under `~/.local/state`, outside the repository, and that directory must be on a local disk (wombat-gate warns when it is not).
3. **Commit only your own paths** — inside, it puts down only its own crate: the paths it named.
4. **Release, next in line** — it comes out, the lock is released, and the next agent goes in.

<p align="center"><sub>Illustrations generated with Google Gemini and edited by the author.</sub></p>

## 🧭 Features in detail

| | |
|---|---|
| **Queue** | All writes to a repository go through one `flock(2)` lock on a *local* disk. Waiters wait instead of failing. If the holder dies, the kernel drops the lock — nothing stale to clean up. |
| **Your paths only** | `wombat-gate commit -m MSG -- PATH...` stages and commits *only* those paths. Whatever another agent staged elsewhere stays staged and stays out of your commit. Whole-tree pathspecs are refused. |
| **Fast on big or slow checkouts** | The commit is built in a private copy of the index and grafted onto `HEAD`'s tree, so git never stats the rest of the working tree; `HEAD` is moved with a compare-and-swap while git's own index lock is held. On a 22k-file repository on a 9p-mounted Windows drive the queue hold went from 30–70 s to a few seconds. |
| **Polite retries** | If a git process that does *not* use wombat-gate holds a git lock, wombat-gate backs off and retries. It never deletes git's lock files. |
| **Cheap status** | `wombat-gate status -- PATH...` looks at your paths only and never takes `index.lock`. |
| **Honest reports** | The commit sha and file list it prints are read from the commit it actually made, not from `HEAD` (which a neighbour may already have moved). |
| **Timing log** | One JSON line per operation: queue wait, lock hold, time per git step, retries, exit code. |

Single file, Python ≥ 3.8 standard library only, Linux / macOS / WSL.

## 📦 Install

```sh
git clone https://github.com/liyifreddy/wombat-gate.git
install -m 755 wombat-gate/bin/wombat-gate ~/.local/bin/wombat-gate   # or symlink it
wombat-gate --version
```

Or as a Claude Code plugin, which brings the CLI (on the Bash tool's `PATH`),
the guard hook and a skill that teaches the agent the rules — see
[Claude Code plugin](#-claude-code-plugin). Install the CLI as above too if you
want to use it from your own terminal.

## 🛠️ Usage

```sh
export WOMBAT_SESSION=agent-7                       # optional: your name in the log / queue

wombat-gate commit -m "parser: handle empty input" -- src/parser tests/test_parser.py
wombat-gate commit -n -m "..." -- src/parser        # dry run: list what would be committed
wombat-gate status -- src/parser                    # status of your paths only
wombat-gate status -uno -- src/parser               # ... without untracked files
wombat-gate run -- git push                         # allow-listed git commands (below)
wombat-gate who                                     # who is in the burrow right now
wombat-gate log -n 50                               # recent timing lines
```

Exit codes: `0` ok · `1` git error · `2` usage / refused · `3` nothing to commit
under the given paths · `4` timed out waiting in the queue · `5` a foreign git
lock never cleared (or `HEAD` kept moving) · `6` committed, but writing the
index failed afterwards (a disk error) — run the same command again once the disk
is fine; it repairs the index and exits 3.

### 🔍 What exactly happens on `commit`

Inside the queue, with `GIT_LITERAL_PATHSPECS=1`, wombat-gate makes the commit
`git add -A -- PATHS && git commit -- PATHS` would make, without looking at the
working tree outside your paths:

1. it takes git's own lock on the index (creates `.git/index.lock` exactly as
   git does; if a foreign git process has it, backs off and retries). From here
   to the end no other git process can write the index — so no bare
   `git commit` can slip in between;
2. copies the index to a private file on the local disk and runs
   `git add -A -- PATHS` on the copy. Stat data comes along, so unchanged files
   are not re-read; whatever else is staged (by other agents, force-added
   files, intent-to-add entries) stays as it is;
3. `git write-tree` on the copy; only your paths are taken from that tree and
   grafted onto `HEAD`'s tree with `ls-tree` + `mktree`, one directory level at a
   time — the cost is the depth of your paths, not the size of the repository.
   Same tree as `HEAD` ⇒ exit 3. Another agent's staged files never reach your
   commit;
4. `prepare-commit-msg` and `commit-msg` hooks run (with `GIT_INDEX_FILE`
   pointing at the copy), the message gets the same cleanup as with
   `git commit -F`, `git commit-tree` writes the commit, and
   `git update-ref HEAD NEW OLD` moves the branch **only if `HEAD` is still
   `OLD`**. If something moved it anyway (plumbing, another worktree), your
   paths are grafted onto the new `HEAD` and tried again — their change stays;
5. the copy is written into `index.lock` and renamed over the index, as git
   does; then `post-commit` runs.

If anything fails before step 5 — a hook rejects the message, the message is
empty, a path you named exists nowhere, git errors, SIGTERM / SIGHUP / SIGQUIT
arrive — the lock file is removed and nothing has changed: not the branch, not
the index. (SIGKILL cannot be caught; then `.git/index.lock` stays behind, as it
does when git itself is killed.) The private copy keeps the index's timestamp,
so git's check for files rewritten within the same clock tick ("racily clean"
entries) works as usual. An unmerged entry anywhere in the index (a half-resolved
conflict) makes the whole-index `write-tree` fail; the commit then falls back to
the classic path.

Known differences from plain `git commit -- PATHS`: mode changes staged with
`git update-index --chmod` on a `core.fileMode=false` repository are committed as
staged (git re-reads the mode from `HEAD` and drops them); a path reached through
a symlinked directory is resolved and committed (git refuses it); hooks see the
private copy as `GIT_INDEX_FILE` (see Limits).

The classic path — `git add -A -- PATHS` then `git commit -- PATHS` on the
shared index, with the index restored on failure — is still there and is used
for `--allow-broad`, while a merge / cherry-pick / revert is in progress, with a
`pre-commit` hook (`--no-verify` skips it), with `commit.gpgSign` (commit-tree
would not sign), in a sparse checkout or split index, for a path spelled in a
different case than on disk in a case-insensitive checkout, and with
`WOMBAT_COMMIT_MODE=classic`. The timing log records which path ran and why.

Two agents committing the *same* path: the first takes both agents' changes,
the second gets exit 3. Give agents disjoint paths.

### 📁 Paths

`commit` and `status` take literal files or directories strictly inside the
repository. Refused: the repository root (`.`, `./`, its absolute path),
anything outside it, `*` and `?` (globs would silently match nothing in literal
mode), and any `:` pathspec magic — `:!mine` means "everything *except* mine",
the opposite of what you want here. A file named `[x].txt` is fine. A symlink is
judged by where the link lives, not where it points. Note: your git hooks inherit
`GIT_LITERAL_PATHSPECS=1`; a hook that globs (`git diff --cached -- '*.py'`) will
match nothing during a wombat-gate commit.

### ▶️ `wombat-gate run`

An allow-list: `push`, `fetch`, `tag`, `branch`, `notes`, and read-only `log`,
`show`, `diff`, `rev-parse`, `ls-files`, `blame`. `push`, `fetch` and the
read-only commands skip the queue (a slow push should not block everyone's
commits); `tag`, `branch`, `notes` take it. Options that rewrite shared state are
refused, including git's prefix abbreviations (`--forc`, `--d`) and short-option
clusters (`-fd`): forced / deleting / mirror pushes and `+ref` / `:ref`
refspecs; `fetch --update-head-ok`, `--upload-pack`, `--refmap`, `--prune-tags`
and refspecs with `:` or `+` (plain `fetch --prune` is fine); `branch`
delete / force / move / copy / upstream changes; `tag -d` / `-f`; `--output` on
any command (it truncates the named file). Everything else — `add`, `commit`,
`stash`, `reset`, `checkout`, `switch`, `restore`, `clean`, `rm`, `mv`,
`merge`, `pull`, `gc`, … — and global options before the subcommand (`-C`,
`-c`, `--git-dir`) is refused.

### 🚪 Escape hatches are for humans

`--allow-broad` (whole-repo paths) and `run --unsafe` (anything outside the
allow-list) exist for a human cleaning up. They are refused when the process
looks like an agent — `CLAUDECODE` is set (Claude Code sets it in its Bash
tool) or `WOMBAT_NO_ESCAPES=1` (set this for other agent frameworks) — unless
`WOMBAT_HUMAN_OVERRIDE=1` is set too. Doing this inside the tool covers every
shell spelling of the flags (quotes, line continuations, variables), which a
text-matching hook cannot. Option abbreviations are disabled, so `--allow` is an
error, not `--allow-broad`.

### 🔁 Retries

Only git's lock-collision message (`Unable to create '….lock': File exists`)
is retried; git prints it before writing anything, so a retry is safe.
Disk-full, permission, ref-conflict and remote-rejection errors fail at once.
If `HEAD` cannot be read (an I/O error on a failing mount), wombat-gate says so
and exits 1; it does not guess that someone else moved it.

### ⚙️ Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WOMBAT_SESSION` | `pid<parent pid>` | your name in the log and in `who` |
| `WOMBAT_STATE_DIR` | `$XDG_STATE_HOME/wombat-gate` or `~/.local/state/wombat-gate` | log + locks |
| `WOMBAT_LOCK_DIR` | `$WOMBAT_STATE_DIR/locks` | lock files — **must be on a local disk** |
| `WOMBAT_LOG` | `$WOMBAT_STATE_DIR/wombat-gate.log` | timing log (JSON lines) |
| `WOMBAT_TIMEOUT` | `600` | seconds to wait in the queue (also `--timeout`, must be > 0) |
| `WOMBAT_GIT_RETRIES` | `8` | retries on a foreign git lock |
| `WOMBAT_GIT_BACKOFF` / `WOMBAT_GIT_BACKOFF_MAX` | `0.5` / `20` | first back-off and cap, seconds |
| `WOMBAT_NOTE_EVERY` | `15` | seconds between "still waiting" notes |
| `WOMBAT_COMMIT_MODE` | `fast` | `classic` = always use `git add` + `git commit` on the shared index |
| `WOMBAT_NO_ESCAPES` | unset | `1` = treat this process as an agent |
| `WOMBAT_HUMAN_OVERRIDE` | unset | `1` = a human allows the escape hatches |

The lock is per repository (keyed by the real path of its git common dir), so
linked worktrees of one repository share one queue.

## 🤖 Using it with Claude Code

1. Install the CLI (above) and put the rules where every session reads them,
   e.g. in `CLAUDE.md`:

   > ✅ Commit with `wombat-gate commit -m "..." -- <your paths>`; check with
   > `wombat-gate status -- <your paths>`; push with `wombat-gate run -- git push`.
   > ⛔ Never `git add -A`, `git add .`, `git commit -a`, `git stash`, or a
   > repo-wide `git status`. ⛔ Never use the human-only escape hatches.

2. Add a `PreToolUse` hook on the Bash tool as a second layer — either the
   plugin below, or [`hooks/guard.py`](hooks/guard.py) in your own
   `.claude/settings.json`. It splits each command the way a shell would
   (`;`, `&&`, pipes, redirections, `bash -c`, `$(...)`, heredocs) and reads
   git's real arguments, then ⛔ blocks:
   - whole-tree staging and committing: `git add -A` / `-u` / `.` without your
     own paths, `git commit -a`, `git commit` without paths;
   - throwing work away or moving the tree: `git stash` (except `list` / `show`),
     `git reset` without `-- <paths>` or with `--hard`, whole-tree or forced
     `checkout` / `restore`, `checkout <branch>`, `switch`, whole-tree
     `clean -f` and `rm`, `pull` / `merge` / `rebase`, `commit -i` / `--amend`;
   - git write commands whose paths the shell computes (`$(...)`, `$PWD`) or
     that are fed by `xargs` / `find -exec` — name your paths explicitly;
   - switching off wombat-gate's agent check: setting `WOMBAT_HUMAN_OVERRIDE`,
     unsetting / un-exporting / overwriting `CLAUDECODE`, `env -i`.

   ✅ Path-limited forms (`git add -A -- src/mine`, `git reset -q -- src/x.py`,
   `git checkout -- src/x.py`) pass. It is a guard rail for cooperating
   agents, not a sandbox: it cannot see inside scripts, git aliases or
   `python -c`, and it does not look at other git commands (`cherry-pick`,
   `revert`, `am`, `worktree`, …). If the guard cannot parse a command it
   blocks it. `python3 hooks/guard.py --agent-only` runs only the agent-check
   rules, for projects that keep their own git rules.

3. Give each session a name (`WOMBAT_SESSION=...` in the command, or
   `--session NAME`) so `wombat-gate who` can tell you who is in the burrow.

For the bigger picture — naming sessions, task cards, a dispatch board, review
chains, unattended runs — see the [multi-session playbook](PLAYBOOK.md).

## 🧩 Claude Code plugin

This repository is also a Claude Code plugin and a one-plugin marketplace:

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

What it brings:

| Part | File | What it does |
|---|---|---|
| CLI | `bin/wombat-gate` | plugin `bin/` directories are put on the Bash tool's `PATH`, so agents can run `wombat-gate` directly |
| Guard hook | `hooks/hooks.json` → `hooks/guard.py` | `PreToolUse` on Bash: blocks whole-tree git commands and the ways of switching off the agent check (self-test: `hooks/selftest_guard.sh`, 197 cases) |
| Skill | `skills/wombat-gate/SKILL.md` | the rules and the exit codes, in the agent's words |

**Recommended:** also mark every Claude Code process as an agent explicitly, in
your project's `.claude/settings.json`:

```json
{ "env": { "WOMBAT_NO_ESCAPES": "1" } }
```

wombat-gate also recognises `CLAUDECODE`, which Claude Code currently sets in
its Bash tool — but that variable is not part of Claude Code's documented
interface and could change. `WOMBAT_NO_ESCAPES=1` does not depend on it.

Without the plugin, use the hook directly: add to `.claude/settings.json`

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "python3 /path/to/wombat-gate/hooks/guard.py" } ] }
    ]
  }
}
```

## ⚠️ Limits — read before relying on it

- **Only wombat-gate users queue.** Plain `git` does not take the lock;
  wombat-gate survives it by retrying, but plain git can still fail against
  everyone else. Migrate every agent.
- **The lock must be on a local filesystem.** `flock` on 9p / SMB / NFS / FUSE
  may silently not exclude; wombat-gate warns when it detects one. On WSL, keep
  the lock on the Linux disk even when the repository is on `/mnt/<drive>`.
  Git running on the Windows side (Windows-native git, IDEs) does not see a WSL
  lock at all.
- **Slow disks: commits get fast, the rest of git does not.** The fast commit
  path avoids stat'ing the whole tree (30–70 s per commit → a few seconds on our
  9p-mounted Windows drive), but your own `git status`, `git log`, etc. are as
  slow as before. Copying the index in and out costs one sequential read and
  write of `.git/index` per commit.
- **Git hooks run, but not exactly as git runs them.** On the fast path
  `prepare-commit-msg` and `commit-msg` see `GIT_INDEX_FILE` pointing at the
  private copy: it holds your paths as committed *plus* whatever else is staged
  in the shared index (git's own partial commit shows hooks `HEAD` + your paths
  only). `post-commit` runs after the index is written. A `pre-commit` hook
  sends the commit down the classic path. `git commit`'s automatic
  `gc --auto` is not run.
- **WSL + 9p: watch memory.** In our setup the `/mnt/<drive>` mount dropped
  (I/O errors until remounted) whenever WSL ran out of memory: 9p requests need
  64 KB of contiguous kernel memory and fail first. That is not wombat-gate's
  doing, but long git commands are the first to hit it.
- **One machine.** The lock does not span hosts.
- **The agent check follows the environment.** A command an agent starts in a
  terminal multiplexer whose server was started outside the agent (e.g.
  `tmux new -d "..."` on a server a human launched) runs without `CLAUDECODE`.
  Set `WOMBAT_NO_ESCAPES=1` in that server's environment too
  (`tmux set-environment -g WOMBAT_NO_ESCAPES 1`).
- **Same path, two agents:** the first commit takes both agents' changes.
- **Fairness:** waiters block in `flock(2)`; the kernel does not promise strict
  first-come-first-served, but at millisecond hold times nobody starves.
- Uses `SIGALRM` while waiting; only matters if you import it as a module.

## ✅ Self-test

```sh
python3 selftest_wombat_gate.py                # throw-away repos under $TMPDIR (~1 min)
python3 selftest_wombat_gate.py --mutants      # re-introduce 36 past bugs; each must turn its check red
bash hooks/selftest_guard.sh                   # 197 commands the guard must block or let through
```

The self-test refuses to run on a network / 9p mount. It includes a **negative
control** (8 concurrent plain-git writers must hit `index.lock`, otherwise the
positive test proves nothing), a **mutation control** (the queue switched off
must show overlapping lock holds), the **queue** test (8 writers × 15 commits:
zero failures, every commit touches only its writer's directory, every reported
sha is the writer's own), a **mixed** run with plain-git writers, and races
with foreign commits staged through a fake `git` on `PATH` (a neighbour commits
during our retry, sweeps our staged files, drops our staging, takes one of our
files). The single-process checks run once on each commit path. The fast path
is also checked against **real git**: 30 rounds of random edits (new, changed,
deleted files, whole directories removed, symlinks, mode changes, force-added
ignored files, intent-to-add and `rm --cached` entries, names with spaces,
brackets and non-ASCII) under several directories. Before each wombat-gate
commit the repository is copied and the copy runs `git add -A -- PATHS && git
commit -- PATHS`; commit tree, index entries under the paths and exit status
must match. Message cleanup is compared byte-for-byte with `git commit`, and a
bare `git commit` fired from inside a wombat-gate commit must be locked out.
`SELFTEST_WOMBAT_BIN=/path/to/copy` runs the suite against a modified copy of
the tool.

## 📍 Status

0.6.1 — illustrated README, a small wombat in `--help`, packaging for `pipx`
packaging and a PyPI release workflow. 0.6.0 — fast commits (private index + tree graft +
compare-and-swap). Next: a
session registry (who holds which machine or directory), sketched in
[DESIGN.md](DESIGN.md).

## 📄 License

MIT — see [LICENSE](LICENSE).
