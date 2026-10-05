# wombat-gate — design notes

## 1. Commit queue (implemented)

**Problem.** N agents, one working tree, one `.git/index`. Every index write
creates `.git/index.lock` with `O_EXCL`; a concurrent writer fails immediately.
Git has no waiting mode for this. Agents then retry ad hoc, delete the lock by
hand (corrupting a live operation), or fall back to `git add -A`.

**Choice: an advisory `flock` on a local file, outside the repository.**

- `flock` is released by the kernel when the holder exits or is killed, so a
  crashed agent cannot wedge the queue. (A lock *file* that has to be deleted is
  exactly the failure mode of `index.lock` we are trying to escape.)
- The lock lives on a local disk, not in `.git/`: the repository may sit on a
  network / 9p mount where `flock` is unreliable and every metadata operation
  is slow.
- One lock per repository, keyed by `realpath(git rev-parse --git-common-dir)`,
  so linked worktrees share a queue (they share refs and objects).
- Waiting is a blocking `flock` (0.1 polled with jitter; review showed a
  polling waiter can lose every race). A one-shot `SIGALRM` timer, re-armed to
  `min(note interval, time left)`, prints "waiting for <session>" notes and
  raises at the deadline; between alarms the interrupted `flock` is restarted
  automatically (PEP 475). If the deadline alarm races a successful `flock`,
  a non-blocking re-lock of our own fd tells us we hold it. On the self-test
  this cut p95 queue wait from ~0.3 s to ~0.05 s.
- A `.holder` side file (written atomically via rename after acquiring) records
  session / op / pid / start time for `wombat-gate who` and for waiters' notes. It is
  informational only; the flock is the truth.

**Commit = `git add -A -- P` → `git diff --cached --no-renames --name-status -- P` →
`git commit -F - -- P`**, all inside the lock, with `GIT_LITERAL_PATHSPECS=1`.
(`--no-renames` because rename detection was 7 of 8 s of that step on a slow
mount and we only need the file list.) Each step's wall time is logged
(`steps` in the JSON line).

- P must be literal paths strictly inside the repository: `:` magic
  (`:!mine` = "everything except mine"), globs and the root are refused.
- Before staging, wombat-gate snapshots the index entries under P
  (`ls-files -s -z --full-name`: without `--full-name` the names are relative
  to the cwd, while `update-index --index-info` reads them as relative to the
  repository root — 0.3 corrupted the index when run from a subdirectory) and
  `HEAD`. On failure it restores exactly those entries with
  `update-index --index-info` (0.2 used `reset -- P`, which also dropped other
  people's partial stagings under P). If `HEAD` moved in between, it does not
  restore: old entries on a new `HEAD` would stage a revert of the foreign
  commit. A dry run uses `git status` and never stages.
- The message is read once and fed on stdin, so `-F -` survives a retry.
- Lock collisions make git fail before it writes anything, so a commit is
  simply retried. 0.2 instead treated "HEAD moved after a lock error" as "our
  commit landed"; but HEAD moves precisely because the *other* process is
  committing, so 0.2 reported foreign shas as its own (found by an independent
  verifier; the self-test now checks sha ownership in every concurrent
  scenario). The sha is parsed from `git commit`'s output, not from `HEAD`.
- If our retries fail and `HEAD` moved, a foreign process committed meanwhile.
  Two cases look the same in the index (nothing under P staged against
  `HEAD`): its bare `git commit` took our staged changes, or its index write
  dropped our staging. 0.3 checked only the index and reported "swept" in both
  (3/3 false in a verifier run). 0.4 checks the working tree with
  `git status --porcelain --untracked-files=all -- P`: empty ⇒ our changes
  are in `HEAD`, exit 3 with an explicit message; otherwise stage and commit
  once more.

- `git commit -- P` (the `--only` mode) builds a temporary index from `HEAD`
  plus P, so entries other agents staged for other paths are neither committed
  nor unstaged.
- `add -A -- P` picks up additions, modifications and deletions under P only.
- The `diff --cached` step gives an explicit "nothing to commit" exit code (3)
  instead of git's generic failure, and the file list for the log.

**Foreign git processes** (agents or humans not using wombat-gate) still race. Inside
the lock, wombat-gate recognises git's lock-collision message (`Unable to create
'….lock': File exists` — only that; `unable to write new index file` can be a
full disk and `cannot lock ref` can be a ref conflict or a remote rejection,
neither of which clears by waiting) and retries with jittered
exponential back-off. It never removes git's lock files: a lock file may belong
to a live process, and deleting it is how indexes get corrupted. After the last
retry it reports the lock file's age and the live `git` processes so a human
can decide.

**Status without locking.** `git status` opportunistically rewrites the index
to refresh stat information, which takes `index.lock`. `GIT_OPTIONAL_LOCKS=0`
turns that off, so `wombat-gate status` never collides with writers and needs no
queue. It also requires a pathspec, which keeps the scan to the agent's own
directories.

**Guard rails.** Broad pathspecs are refused for `commit` and `status`.
`wombat-gate run` is an allow-list, not a deny-list: 0.1's deny-list was bypassed by
`-C . stash`, `reset --har` (git accepts option prefixes), `checkout -f`,
`switch --discard-changes`, `rm -r --cached .` and a plain `reset`. 0.2's
allow-list still let through state-rewriting *arguments* (`fetch
--update-head-ok origin +main:main` moved HEAD, `branch -M`, `diff --output=F`
truncated F, `push --force`), so 0.3 adds per-subcommand option denials that
also match long-option prefixes and short clusters, and drops `gc` /
`maintenance`. `push` / `fetch` / read-only commands run without the queue;
`tag` / `branch` / `notes` take it. `--allow-broad` / `--unsafe` override,
but only for humans: when the environment marks the process as an agent
(`CLAUDECODE`, `WOMBAT_NO_ESCAPES`), both are refused unless
`WOMBAT_HUMAN_OVERRIDE=1`, and refusal messages shown to agents do not name
the escape hatch.

**The guard hook** (`hooks/guard.py`) was first a list of regular expressions
over the command text. Review showed that cannot work: `git checkout .`,
`git stash 2>&1`, `git commit -qam`, `git -C "dir with space" add -A` all
slipped through, while safe, path-limited forms (`git add -A -- src/mine`) and
heredoc bodies were blocked. It now splits the command like a shell (shlex with
punctuation, redirections and their fd numbers dropped, `bash -c` / `eval` /
`$(...)` recursed into, heredoc bodies removed) and checks git's actual argument
structure (global options, short-option clusters, option values, pathspecs).

## 2. Session registry (design only — not built)

**Problem.** Besides git, agents sharing a machine pool collide on *resources*:
two agents start jobs on the same remote box, or edit the same directory.
Today this is coordinated by a human-maintained table in a document, which goes
stale.

**Goal.** A small, local, append-only lease registry that agents query before
acting, and that a human can read at a glance. Advisory, like the git queue:
it informs and warns; enforcement stays with hooks and humans.

### Data model

One JSON-lines file per registry (local disk, next to the wombat-gate state), each line
an event:

```json
{"ts": "2026-01-01T12:00:00+0100", "event": "claim", "session": "agent-7",
 "kind": "host", "name": "gpu-box-2", "mode": "exclusive",
 "ttl_s": 14400, "note": "training run X", "pid": 12345}
```

- `kind`: `host` (a remote machine), `path` (a directory in the working tree),
  or free-form.
- `mode`: `exclusive` (only the holder may start / kill / deploy) or `shared`
  (read-only access, recorded for visibility).
- `event`: `claim`, `renew`, `release`. Current state = fold over the log;
  a lease whose `ts + ttl_s` has passed without `renew` is shown as *expired*,
  never silently dropped.
- Appends happen under the same `flock` mechanism (a separate lock file), so
  concurrent claims are serialised and the second exclusive claim on a resource
  is rejected with the current holder's name.

### Interface (proposed for 0.6 — not implemented yet)

```
wombat-gate lease claim   KIND NAME [--ttl 4h] [--shared] [--note TEXT]
wombat-gate lease renew   KIND NAME [--ttl 4h]
wombat-gate lease release KIND NAME
wombat-gate lease check   KIND NAME            # read-only
wombat-gate lease show    [--kind KIND] [--json]
```

- `KIND`: `host` (a machine), `path` (a directory or file in this repository),
  or any other lowercase word. `NAME` for `path` is normalised to a
  repository-relative path (same rules as `commit` paths: literal, inside the
  repo, not the root); a `path` lease covers everything below it, and two
  `path` leases conflict when one contains the other.
- `--ttl`: `30m`, `4h`, `1d`; default `4h`; maximum `7d`. Any wombat-gate
  command run by the same session renews that session's leases.
- Identity: `claim`, `renew`, `release` require a session name
  (`WOMBAT_SESSION` or `--session`); the `pid…` fallback is refused, because a
  lease must outlive the process that took it.
- Exit codes (in addition to the existing 0–5): `6` held by another session
  (stderr names the holder, since, expires); `7` not yours (`renew` / `release`
  of someone else's lease). `check` exits `0` if free or yours, `6` otherwise.
- Taking over someone else's lease is `claim --steal`, a human-only escape
  hatch under the same agent check as `--allow-broad` / `--unsafe`.
- `show` prints one row per live resource: kind, name, holder, mode, since,
  expires, note; expired leases are listed separately, never dropped silently.
  `--json` prints `[{"kind", "name", "holder", "mode", "since", "expires",
  "expired", "note"}]` for tooling and status pages.
- Storage: append-only `leases.jsonl` in `WOMBAT_STATE_DIR` (one event per
  line, as above), written under its own `flock`; the state is the fold over
  the log, so the file doubles as the audit trail.

### Integration points

- `wombat-gate commit` could warn (not refuse) when a committed path lies under a
  `path` lease held by another session.
- A `PreToolUse` hook can call `wombat-gate lease check host <name>` before commands
  that start or kill jobs on a remote host (e.g. matching `ssh <host> ... tmux
  new` / `kill`), turning the human table into a mechanical check.
- `wombat-gate lease show` output can be pasted into a status document; the JSONL log
  is the audit trail of who held what, when.

### Open questions

1. TTL default and renewal (proposed above: 4 h default, renewed by any command of the same session). Agents that sleep for hours (waiting on a job) need
   long leases; a crashed agent should not hold a box for a day. Proposal:
   default 4 h, `renew` on every wombat-gate invocation by the same session.
2. Identity: `WOMBAT_SESSION` is self-declared. Good enough for cooperating
   agents; not a security boundary.
3. Multiple machines: the registry is local. Agents on different machines would
   need a shared store (a tiny HTTP service, or a git branch used as a log);
   out of scope until needed.
4. Reconciliation with reality: a lease says "agent-7 owns gpu-box-2", not
   that agent-7's job is still running there. A `probe` hook per kind (e.g.
   `ssh host pgrep ...`) could mark leases as *stale*.
