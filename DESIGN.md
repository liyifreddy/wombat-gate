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

**Fast commit (0.6, the default).** The classic commit below spends almost all
of its time in `git commit` itself: before writing anything it refreshes the
whole shared index, one `lstat()` per tracked file. Measured on a 22k-file
repository on a 9p-mounted Windows drive: 25–85 s per commit, tens of thousands
of 9p requests. With a dozen agents committing in a burst, queue waits reached
6 minutes. A full `read-tree HEAD` into a private index is no way out either:
19.5 s on that mount (42k loose objects, one open per tree). The fast path:

1. **Take git's index lock** the way git does: `open(index.lock, O_CREAT|O_EXCL)`,
   backing off on `EEXIST` like `git_retry`. It is held until the end, so no
   other git process can write the index meanwhile. A first draft wrote the
   index only after the ref update, without the lock; review pointed out that a
   bare `git commit` in that gap would build its tree from an index still
   holding our paths' old entries and silently revert us.
2. **Copy the index** to a private file (local disk) and `add -A -- P` on the
   copy. Copying instead of seeding from `HEAD` (the first draft) keeps stat data
   (unchanged files are not re-hashed), keeps force-added ignored files and
   intent-to-add entries under P (the draft dropped them), keeps staged mode
   changes, and keeps everyone else's staging for the final index write.
3. **Graft.** `write-tree` the copy, `ls-tree` it for each path in P (one call),
   rebuild `HEAD^{tree}` bottom-up along those paths only: per directory on the
   way, `ls-tree` one level, replace / delete the entry, `mktree -z --missing`.
   Directories that end up empty are dropped. Cost ≈ depth × number of paths.
   Others' staged entries elsewhere are in the copy's tree but are never
   grafted. A path that exists nowhere (not on disk, not in HEAD) fails the
   commit, as `git commit -- P` does — but only when there is something to
   commit, since the classic path returns "nothing to commit" first.
4. Graft equals `HEAD^{tree}` ⇒ exit 3 (the copy is still written back: it is
   what `add -A -- P` leaves). Otherwise `prepare-commit-msg` / `commit-msg`
   (`--git-path hooks/…` honours `core.hooksPath`; `GIT_INDEX_FILE` = the copy),
   message cleanup as `git commit -F` (`stripspace`; `--strip-comments` only for
   `commit.cleanup=strip`), `commit-tree`, `update-ref -m "commit: <subject>"
   HEAD NEW OLD`. With the index lock held a normal `git commit` cannot move
   HEAD, but plumbing or another worktree on the same branch can; then the CAS
   fails (`is at X but expected Y`) and P is grafted onto the new HEAD (their
   change kept; if it already contains ours, exit 3). Six failures in a row ⇒
   exit 5.
5. **Write the copy into `index.lock` and rename it over the index**, as git
   does; then `post-commit`. If that write fails (disk error) the commit has
   landed but the index still has the old entries (staged changes that would
   revert it): exit 6, and running the same commit again repairs it (exit 3).
   On every other failure the lock file is removed (only if its inode is still
   ours) and nothing has changed. SIGTERM / SIGHUP / SIGQUIT are turned into an
   exception for the duration, so a tool timeout does not leave `index.lock`
   behind for every other agent (an independent verifier found 0.6-draft did).
   The write loops until every byte is written (a short write on a full disk
   would otherwise leave a truncated index) and checks the lock file is still
   ours before the rename.

**Timestamps (racy git).** Git re-reads an entry whose file mtime is not older
than the index file's own mtime ("racily clean"), because a rewrite within the
same clock tick does not change size or mtime. Copying the index with a fresh
mtime, or writing it back with one, hides such rewrites for good: the verifier
reproduced a commit of stale content with a clean `git status`, also across
agents. So the copy keeps the original mtime (`copy2`), and the lock file gets
the private index's mtime (the one `git add` judged its entries against) before
the rename.

**Unmerged entries elsewhere.** `write-tree` on the whole copy fails if any entry
in the index is unmerged, even outside P, while `git commit -- P` copes. Nothing
has changed at that point, so the fast path hands over to the classic one.

**Bytes, not text.** File names are bytes; one non-UTF-8 name anywhere on the
graft path crashed the first draft. git output is decoded with
`surrogateescape`, so names round-trip unchanged.

Fallbacks to the classic path: `--allow-broad` (pathspec magic cannot be
grafted); merge / cherry-pick / revert in progress; a `pre-commit` hook (git
shows it `HEAD` + P, our copy also holds others' staging); `commit.gpgSign`
(`commit-tree` ignores it); sparse checkout; split index (the copy would point
at shared-index files next to the original); and, with `core.ignorecase`, a
path whose spelling differs from the file on disk (the graft is case-sensitive
and would add a case-duplicate entry). `WOMBAT_COMMIT_MODE=classic` forces it.
The log line records `mode` and `classic_reason`.

How we know it is the same commit: the self-test copies the repository before
every fast commit and runs real `git add -A -- P && git commit -- P` on the
copy, over 30 rounds of random edits including the special staging states
above; commit tree, index entries under P and exit status must match.

**Reading `HEAD`.** 0.5 used "`rev-parse HEAD` returned nothing" both for an
unborn branch and as "HEAD moved"; on a failing 9p mount an I/O error made it
report a foreign commit that never happened (twice on one day). Now a failure
with stderr output raises, and a branch that had a commit but reads as missing
raises too; both exit 1 with "cannot read HEAD".

**Classic commit = `git add -A -- P` → `git diff --cached --no-renames --name-status -- P` →
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
  commit. A dry run stages in a private index copy; the shared index is untouched, although Git may write content objects.
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

## 2. Path claims (0.9, implemented) and host leases (design only)

**Problem.** Agents in one checkout collide on *paths* long before git does: two
agents edit the same directory, and the clash only shows when the second commit
sweeps up both changes or a merge conflicts. Agents sharing a machine pool also
collide on *resources* (two agents start jobs on the same remote box). Both were
coordinated by a human-maintained table in a document, which goes stale.

### Path claims as built (0.9)

- `claim --as NAME -- PATH...` registers repository-relative paths for a session;
  overlap = equal paths or one above the other. All or nothing: one overlapping
  path refuses the whole request (exit 7) and names the holder. Advisory, like the
  queue: `commit` only prints notes; enforcement stays with task cards and humans.
- **Lifetime = the agent process, not a TTL.** Each claim records the owner process
  (`$WOMBAT_OWNER_PID`, `$CLAUDE_PID`, else the nearest non-shell ancestor) with its
  start time; the claim is dropped as soon as anyone reads the registry after that
  process is gone or its pid was reused. Agents that wait for hours keep their claims
  without renewing; a crashed agent frees them at once. This is the same "is the
  owner alive" test dingo-pack's roster uses. The price: claims made without an
  owner process (human shell under tmux/ssh, macOS) live until `release`.
- Storage: one small JSON document per repository next to the queue lock
  (`<repo>-<hash>.claims.json`), keyed by the git common dir so all worktrees share
  it, rewritten atomically under its own short `flock`. Current state only; the
  timing log is not used as an audit trail for claims.
- Identity is self-declared (`--as` / `$WOMBAT_SESSION`); claim / release / isolate
  refuse the `pid…` fallback name. Not a security boundary.

### Host leases (design only — not built)

The rest of this section is the original lease design for remote machines; path
claims above replaced its `path` kind.

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

### Interface (proposed — not implemented yet)

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
- Exit codes (in addition to the existing 0–6): `7` held by another session
  (stderr names the holder, since, expires); `8` not yours (`renew` / `release`
  of someone else's lease). `check` exits `0` if free or yours, `7` otherwise.
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

## 0.8 commit and integration details

`stage_named` checks selected paths against HEAD and the chosen index. A path absent from disk and index but known to HEAD is already a staged deletion, so it is omitted from `git add` and remains absent in the index. Unknown paths still fail. In classic mode, deleted HEAD entries are seeded into the shared index immediately before `git commit --only`, so Git can match those path arguments and stage their disk deletion into its own commit index; the original selected-path snapshot is restored on failure. The fast path keeps using its private index and grafts the absent entries as deletions. All tree lookup paths are normalized against the repository root, including calls from a subdirectory.

Preview stages in an index copy, reads NUL-separated diff records, identifies rename pairs with Git's rename detection and prints one JSON-quoted row per path. `--expect-files` verifies the reviewed count inside the queue and again at the final selected diff; it does not turn a directory argument into a single file. Unrelated staged entries are neither displayed nor committed. Counts are not a content-approval signature: review changes immediately before committing and coordinate other processes touching the same paths.

`integrate` deliberately requires two existing worktrees rather than checking out a branch in the target's shared working tree. It verifies source cleanliness, operation state, branch identity, dirty target overlap (including ignored files) and a common merge base. Linear rebase runs in the source with auto-stash, auto-squash and updateRefs disabled. Required argv-based checks run there with individual timeouts; checks must leave the source clean. The target is checked again, then Git performs an unforced fast-forward. Conflicts remain in the source for review; failed checks keep the rebased branch without advancing the target. There is no rollback that could overwrite somebody else's work. Foreign writers remain outside the cooperative queue. Lock scope and clean-up are in section 3.

Lock interval validation uses monotonic acquisition/release timestamps in addition to wall-clock log fields. WSL wall time was observed stepping backwards between sequential commits by the same writer; it cannot prove queue overlap.

## 3. Isolated worktrees and integrate without the queue (0.9)

**Why worktrees alone are not enough.** One worktree per agent (the OpenCode / Codex
model) keeps agents apart while they type, but two agents that edit the same code
still meet at merge time, and someone has to create, merge and delete the worktrees
by hand. 0.9 keeps that isolation and adds the three things around it: overlap is
reported when work starts (`isolate -- PATH...` claims first), hand-in is a checked
rebase + fast-forward, and clean-up is a command that refuses to lose work.

**`isolate`.** Branch `wombat/<name>` at the target's HEAD, worktree at
`<main worktree>.wombat/<name>` (beside the repository, so tools that scan the
repository do not walk into it). The entry (session, branch, path, target branch,
base, owner) goes into `<repo>-<hash>.isolates.json` under the registry lock; the
`git worktree add` itself runs under the queue lock because it writes refs. Isolated
worktrees are never pruned automatically, unlike claims: the directory may hold
commits nobody integrated yet.

**`--done`.** Order: refuse from inside the worktree; refuse a rebase/merge in
progress, uncommitted or untracked files, or a branch that is not an ancestor of the
target branch; then `git worktree remove` without `--force` (git re-checks
cleanliness), then `git update-ref -d refs/heads/<branch> <sha>` so a commit that
lands on the branch after the check makes the delete fail instead of losing it.
Ignored files go with the worktree (caches from the checks would otherwise block
every clean-up); they are listed.

**W1: the queue is not held during rebase and checks.** 0.8 held the repository's
queue for the whole integration; with a 10-minute check every other session's
commit waited, and timed out at the default 600 s (found in an independent review).
The queue protects the *target* worktree's index and HEAD; rebase and checks touch
only the source worktree (its own index, its own branch ref, shared objects that
git writes atomically). So 0.9 holds the queue only for:

1. a snapshot: validate both worktrees, note target branch + sha;
2. the hand-over: target branch, sha and operation state unchanged, no dirty target
   path overlapping the rebased changes → `merge --ff-only`.

If the target moved in between, the checks validated a tree that is no longer what
would land, so the round is discarded: rebase onto the new target, check again, up
to `--attempts` (default 3). A busy target with long checks can exhaust the attempts;
that ends with exit 1, the target untouched and the original source sha printed. The
first snapshot keeps 0.8's behaviour that a queue timeout leaves the source branch
untouched.
