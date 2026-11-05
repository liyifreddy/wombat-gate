---
name: wombat-gate
description: How to commit, check status and push when several agents share one git working tree. Use before any git add / git commit / git status / git push in a shared checkout, and whenever git reports "index.lock: File exists".
---

# Committing in a shared working tree (wombat-gate)

Other agents are working in this same checkout right now. Their uncommitted
work is in the same index and the same files as yours. Plain `git add` /
`git commit` collide on `.git/index.lock` and can sweep their work into your
commit. `wombat-gate` queues every write through one lock (one wombat in the
burrow at a time) and commits only the paths you name.

## Do

- At the start, reserve the paths your task gives you:
  `wombat-gate claim --as <your session name> -- <paths>`. Exit 7 means another
  session already holds an overlapping path (stderr says who): stop and report, do not
  work around it. When you have handed in, `wombat-gate release --as <name>`.
- Commit: `wombat-gate commit -m "<message>" -- <your files or directories>`
  - name only your own paths; a directory covers everything under it;
  - long message: write it to a file and use `wombat-gate commit -F <file> -- <paths>`;
  - add `--session <your name>` so others can see who is in the queue.
- Look before you commit: `wombat-gate commit -n -m x -- <paths>` (dry run; leaves the shared index and HEAD untouched).
- Status: `wombat-gate status -- <your paths>` (add `-uno` to skip untracked files).
- Push: `wombat-gate run -- git push`.
- Who is committing right now: `wombat-gate who`.

## Don't

- `git add -A`, `git add .`, `git commit -a`, a bare `git commit` with no paths,
  `git stash`, `git reset --hard`, `git clean -f`, `git checkout -- .`,
  `git restore .` — each one takes or throws away other agents' work.
- Repo-wide `git status` / `git diff` on a big tree — use your paths.
- Never delete `.git/index.lock` (or any `*.lock` in `.git`) by hand. If wombat-gate exits 5,
  run `wombat-gate doctor` and report what it says; a stale lock is for a human to remove.
  (wombat-gate removes its own leftover lock by itself.)
- Never use the human-only escape hatches (`--allow-broad`, `--unsafe`) and never
  try to switch off wombat-gate's agent check (do not touch `CLAUDECODE`, do not
  run commands under `env -i`). If you think you need one, stop and ask.
- To unstage or discard your own file, name it: `git reset -q -- <path>`,
  `git checkout -- <path>`. Forms without paths are blocked.

## Exit codes and what to do

| Code | Meaning | Do this |
|---|---|---|
| 0 | committed; the sha and file list printed are from your commit | carry on |
| 1 | git error (e.g. a commit hook refused the message, or HEAD could not be read) | read the message, fix, retry; nothing was committed and the index was left as it was — if stderr says HEAD could not be read or HEAD moved, check `wombat-gate status -- <paths>` first and report a disk problem |
| 2 | refused (bad path, `.`, glob, a forbidden option) or usage error | name real paths inside the repo; use only allowed commands |
| 3 | nothing to commit under your paths — or another process's commit already took your changes (the message says which) | check `git log -1 -- <paths>`; do not commit again blindly |
| 4 | waited too long in the queue (`--timeout`, default 600 s) | run `wombat-gate who`, wait, retry later |
| 5 | a git process that does not use wombat-gate kept a lock, or other commits kept landing while we built ours | report it with the message; do not delete the lock |
| 6 | committed (the sha is printed), but writing the index failed afterwards, so it still has the old entries for your paths | report the disk error; once the disk is fine run the same `wombat-gate commit` again: it repairs the index and exits 3. Never commit the "staged" old versions |
| 7 | `claim` / `isolate`: another session holds an overlapping path; nothing was claimed | stop and report who holds it (it is in stderr); do not take the path anyway |

If two agents commit the same path, the first commit takes both agents'
changes; the second gets exit 3. Keep to your own paths.

## Reviewed moves and worktree integration (0.8)

For a move, name the old and new paths even if Git already staged the rename/deletion. Run `wombat-gate commit -n --expect-files N -m MESSAGE -- PATH...`, review the one-row-per-path output, then use the same N and path list for the real commit. A rename contributes two paths but one rename pair. Directory parameters expand, so use the expanded file count. Stop on count mismatch. No reset of staged deletions is needed.

From the target worktree, integrate a reviewed committed branch with `wombat-gate integrate --rebase BRANCH --check 'python3 tests/run_all.py'`. Repeat `--check` for every task-required check. The clean source must already have its own worktree. Other sessions keep committing while the checks run; if the target moves, integrate rebases and checks again by itself (up to `--attempts`). On conflict stop and report the source path and the conflicting files it lists; the rebase remains there. On failed checks the rebased source is retained and the target is not advanced. Use the project's permission rules before integration, pushing or publishing.

## Your own worktree (0.9)

When your task says to work isolated (several sessions on the same code), run from the main checkout `wombat-gate isolate --as <name> [-- <paths>]`, `cd` into the printed directory, and commit there with `wombat-gate commit --as <name> ...`. To hand in, from the main checkout: `wombat-gate integrate --as <name> --check '<required check>' --done` — it rebases, checks, fast-forwards and then removes your worktree and branch. Never remove branches or worktrees by hand (`git worktree remove`, `git branch -D`, `rm -rf`); if `--done` refuses, report its message.

For Codex and OpenCode, put these rules in project AGENTS.md and start the CLI with `WOMBAT_NO_ESCAPES=1 WOMBAT_SESSION=<task>`. This installs instructions, not a shell guard. The Claude Code plugin supplies its own Bash guard hook. See the README for configuration examples and platform limits.
