# wombat-gate

**One at a time through the burrow.** A commit queue for many AI agents (or
humans) sharing one git working tree.

```
             .-~~~~~~~~~~~~~~~~~~~~~~~~-.
          .-'      ~ the burrow ~         '-.
         /        .----------------.         \
        |        /                  \         |
        |       |    o          o    |        |   <- one wombat bottom, wedged in.
        |       |         ,,         |        |      a plate of cartilage and bone.
        |       |    o          o    |        |      nobody else gets past.
        |        \_______/  \_______/         |
         \          ''          ''           /
          '-._                           _.-'
              '~~~~~~~~~~~~~~~~~~~~~~~~~'
                   [] [] []   <- yes, wombat poo really is cube-shaped.
```

Wombats are lovely, stubborn, and built like a door. When something chases a
wombat, it dives into its burrow and plugs the entrance with its backside: a
hard plate of cartilage and bone that no fox can get past. One wombat in the
tunnel, everyone else waits outside. (They also leave neat little cubes of poo
on rocks to say *this is mine*. We think that is the right attitude towards your
own directory.)

`wombat-gate` does that for git. Many agents can work in one checkout; only
one at a time gets into `.git`:

```
  agent A ──┐                                   ┌──▶ git add -A -- A's paths
  agent B ──┼──▶ queue ──▶ [ wombat ] ──▶ write ─┤    git commit -- A's paths
  agent C ──┘    (wait)     one at a time        └──▶ release ──▶ next in line
                            (flock on a
                             local disk)
```

## Why

Run a dozen Claude Code sessions (or any agents) against **one** working tree
and they will all call `git add` / `git commit` / `git status` on their own
schedule. Git was not built for that:

- every index write takes `.git/index.lock`; the second writer fails with
  `fatal: Unable to create '.../.git/index.lock': File exists.`
- `git add -A`, `git commit -a` and a bare `git commit` sweep other agents'
  half-finished work into *your* commit;
- repo-wide `git status`, repeated by many agents, can saturate a slow or
  network-backed filesystem (WSL `/mnt/c`-style 9p mounts, SMB, NFS).

In the self-test (8 writers × 15 commits on a local disk), plain git landed
1–10 of 120 commits across our runs; the other 110–119 failed on `index.lock`.
With wombat-gate: 120 of 120, median wait in the queue well under 0.1 s.

| | |
|---|---|
| **Queue** | All writes to a repository go through one `flock(2)` lock on a *local* disk. Waiters wait instead of failing. If the holder dies, the kernel drops the lock — nothing stale to clean up. |
| **Your paths only** | `wombat-gate commit -m MSG -- PATH...` stages and commits *only* those paths. Whatever another agent staged elsewhere stays staged and stays out of your commit. Whole-tree pathspecs are refused. |
| **Polite retries** | If a git process that does *not* use wombat-gate holds a git lock, wombat-gate backs off and retries. It never deletes git's lock files. |
| **Cheap status** | `wombat-gate status -- PATH...` looks at your paths only and never takes `index.lock`. |
| **Honest reports** | The commit sha and file list it prints are read from the commit it actually made, not from `HEAD` (which a neighbour may already have moved). |
| **Timing log** | One JSON line per operation: queue wait, lock hold, time per git step, retries, exit code. |

Single file, Python ≥ 3.8 standard library only, Linux / macOS / WSL.

## Install

```sh
git clone https://github.com/liyifreddy/wombat-gate.git
install -m 755 wombat-gate/bin/wombat-gate ~/.local/bin/wombat-gate   # or symlink it
wombat-gate --version
```

Or as a Claude Code plugin, which brings the CLI (on the Bash tool's `PATH`),
the guard hook and a skill that teaches the agent the rules — see
[Claude Code plugin](#claude-code-plugin). Install the CLI as above too if you
want to use it from your own terminal.

## Usage

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
lock never cleared.

### What exactly happens on `commit`

Inside the lock, with `GIT_LITERAL_PATHSPECS=1`:

1. snapshot the index entries under your paths (`git ls-files -s`);
2. `git add -A -- PATHS` (additions, modifications, deletions under them);
3. `git diff --cached -- PATHS` — nothing there ⇒ exit 3;
4. `git commit -F - -- PATHS` — only these paths go into the commit;
5. read the sha from git's output and the file list from that commit.

If the commit fails (say, a hook rejects it), the index entries under your paths
are put back exactly as they were — a neighbour's partial `git add -p` there
survives. If a non-wombat-gate process committed in the meantime, wombat-gate
does not restore (that would stage a revert of their commit) and looks at the
working tree instead: if your changes are already in `HEAD`, someone's bare
`git commit` swept them up — exit 3, and it tells you; otherwise it stages and
commits once more.

Two agents committing the *same* path: the first takes both agents' changes,
the second gets exit 3. Give agents disjoint paths.

### Paths

`commit` and `status` take literal files or directories strictly inside the
repository. Refused: the repository root (`.`, `./`, its absolute path),
anything outside it, `*` and `?` (globs would silently match nothing in literal
mode), and any `:` pathspec magic — `:!mine` means "everything *except* mine",
the opposite of what you want here. A file named `[x].txt` is fine. A symlink is
judged by where the link lives, not where it points. Note: your git hooks inherit
`GIT_LITERAL_PATHSPECS=1`; a hook that globs (`git diff --cached -- '*.py'`) will
match nothing during a wombat-gate commit.

### `wombat-gate run`

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

### Escape hatches are for humans

`--allow-broad` (whole-repo paths) and `run --unsafe` (anything outside the
allow-list) exist for a human cleaning up. They are refused when the process
looks like an agent — `CLAUDECODE` is set (Claude Code sets it in its Bash
tool) or `WOMBAT_NO_ESCAPES=1` (set this for other agent frameworks) — unless
`WOMBAT_HUMAN_OVERRIDE=1` is set too. Doing this inside the tool covers every
shell spelling of the flags (quotes, line continuations, variables), which a
text-matching hook cannot. Option abbreviations are disabled, so `--allow` is an
error, not `--allow-broad`.

### Retries

Only git's lock-collision message (`Unable to create '….lock': File exists`)
is retried; git prints it before writing anything, so a retry is safe.
Disk-full, permission, ref-conflict and remote-rejection errors fail at once.

### Configuration

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
| `WOMBAT_NO_ESCAPES` | unset | `1` = treat this process as an agent |
| `WOMBAT_HUMAN_OVERRIDE` | unset | `1` = a human allows the escape hatches |

The lock is per repository (keyed by the real path of its git common dir), so
linked worktrees of one repository share one queue.

## Using it with Claude Code

1. Install the CLI (above) and put the rules where every session reads them,
   e.g. in `CLAUDE.md`:

   > Commit with `wombat-gate commit -m "..." -- <your paths>`; check with
   > `wombat-gate status -- <your paths>`; push with `wombat-gate run -- git push`.
   > Never `git add -A`, `git add .`, `git commit -a`, `git stash`, or a
   > repo-wide `git status`. Never use the human-only escape hatches.

2. Add a `PreToolUse` hook on the Bash tool as a second layer — either the
   plugin below, or [`hooks/guard.py`](hooks/guard.py) in your own
   `.claude/settings.json`. It splits each command the way a shell would
   (`;`, `&&`, pipes, redirections, `bash -c`, `$(...)`, heredocs) and reads
   git's real arguments, then blocks:
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

   Path-limited forms (`git add -A -- src/mine`, `git reset -q -- src/x.py`,
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

## Claude Code plugin

This repository is also a Claude Code plugin and a one-plugin marketplace:

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

What it brings:

| Part | File | What it does |
|---|---|---|
| CLI | `bin/wombat-gate` | plugin `bin/` directories are put on the Bash tool's `PATH`, so agents can run `wombat-gate` directly |
| Guard hook | `hooks/hooks.json` → `hooks/guard.py` | `PreToolUse` on Bash: blocks whole-tree git commands and the ways of switching off the agent check (self-test: `hooks/selftest_guard.sh`, 181 cases) |
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

## Limits — read before relying on it

- **Only wombat-gate users queue.** Plain `git` does not take the lock;
  wombat-gate survives it by retrying, but plain git can still fail against
  everyone else. Migrate every agent.
- **The lock must be on a local filesystem.** `flock` on 9p / SMB / NFS / FUSE
  may silently not exclude; wombat-gate warns when it detects one. On WSL, keep
  the lock on the Linux disk even when the repository is on `/mnt/<drive>`.
  Git running on the Windows side (Windows-native git, IDEs) does not see a WSL
  lock at all.
- **Slow disks stay slow.** wombat-gate stops collisions; it does not make git
  faster. On a 9p-mounted Windows drive one commit took us 30–90 s, and the
  others wait that long. The per-step timings in the log show where it goes.
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

## Self-test

```sh
python3 selftest_wombat_gate.py                # throw-away repos under $TMPDIR (~30 s)
python3 selftest_wombat_gate.py --mutants      # re-introduce 17 past bugs; each must turn its check red (~4 min)
bash hooks/selftest_guard.sh                   # 181 commands the guard must block or let through
```

The self-test refuses to run on a network / 9p mount. It includes a **negative
control** (8 concurrent plain-git writers must hit `index.lock`, otherwise the
positive test proves nothing), a **mutation control** (the queue switched off
must show overlapping lock holds), the **queue** test (8 writers × 15 commits:
zero failures, every commit touches only its writer's directory, every reported
sha is the writer's own), a **mixed** run with plain-git writers, and races
with foreign commits staged through a fake `git` on `PATH` (a neighbour commits
during our retry, sweeps our staged files, drops our staging, takes one of our
files). `SELFTEST_WOMBAT_BIN=/path/to/copy` runs the suite against a modified
copy of the tool.

## Status

0.5.0 — small and deliberately boring. Next: a session registry (who holds which
machine or directory), sketched in [DESIGN.md](DESIGN.md).

## License

MIT — see [LICENSE](LICENSE).
