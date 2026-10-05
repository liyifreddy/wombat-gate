# 🕳️ wombat-gate

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-banner.jpg" width="100%"
       alt="wombat-gate banner illustration">
</p>

**One at a time through the burrow.** A commit queue for AI agents sharing one git
repository: they wait their turn, and each commits only its own files.

🌐 **English** · [中文](https://github.com/liyifreddy/wombat-gate/blob/main/README.zh-CN.md)

## 🚀 Quick start

```sh
pipx install git+https://github.com/liyifreddy/wombat-gate

wombat-gate commit -m "fix parser" -- src/parser   # commit only these paths, in turn
wombat-gate run -- git push                        # push
wombat-gate who                                    # who is in the burrow
```

One file, Python ≥ 3.8, standard library only. Linux, macOS and WSL.

In Claude Code, install the plugin (CLI + guard hook + rules for the agent):

```
/plugin marketplace add liyifreddy/wombat-gate
/plugin install wombat-gate@wombat-gate
```

## 😩 The problem

Put a few Claude Code sessions (or any agents) in one checkout and this happens fast:

- 🧺 **Commits take other people's files.** `git add -A`, `git commit -a` or a bare
  `git commit` sweeps in whatever a neighbour has staged. It bit us twice in two days.
- 🥊 **Lock fights.** Every index write needs `.git/index.lock`; the second agent gets
  `index.lock: File exists`. In our self-test, 8 writers × 15 commits landed 1–10 of 120.
- 🐢 **Every commit checks the whole tree.** On our 22k-file repo on a WSL-mounted Windows
  drive, one commit took 30–70 s and a busy queue waited 6 minutes. When memory runs low,
  those floods of disk requests fail first and take the drive down with them.
- 💣 **Whole-tree commands.** Agents reach for `git stash`, `git reset --hard`,
  `git checkout -- .` and throw away each other's work.

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-before-after.jpg" width="100%"
       alt="Before: three robots crash into each other in the burrow and their crates break. After: the wombat at the entrance lets one robot in at a time, and it puts its crate on its own shelf while the others wait outside">
</p>
<p align="center"><sub>Before: everyone in the burrow at once. After: one at a time, each crate on its own shelf.</sub></p>

## ✨ What you get

- ✅ **Your paths, nothing else.** `wombat-gate commit -m MSG -- PATH...` commits exactly
  those paths. Whatever others staged stays put.
- ✅ **A queue, not errors.** Everyone waits for one lock and goes in turn. Self-test:
  120 of 120 commits land; median wait 0.14 s on an idle machine.
- ✅ **No whole-tree scan.** Only your paths are touched. That 22k-file repo went from
  30–70 s to 3 s on its first real commit.
- ✅ **A guard hook** that stops whole-tree git commands in Claude Code (tested on 197 commands).
- ✅ **Hands off git's lock files.** If another git process holds one, it waits and retries.
- ✅ **A timing log**: queue wait, lock hold and every git step, one JSON line per commit.
- ✅ **One-line install** as a Claude Code plugin, with the hook and a skill that teaches
  the agent the rules.

## 🕳️ Why a wombat

When a wombat is chased, it dives into its burrow and plugs the hole with its bottom —
a hard plate of bone that no fox can bite through. One wombat per burrow; everyone else
waits outside. (Wombats also leave cube-shaped poo on rocks to say *mine*. We feel the
same about our directories.)

```
  agent A ──┐                                      ┌──▶ commit A's paths only
  agent B ──┼──▶ queue ──▶ take lock ──▶ write ────┤
  agent C ──┘    (wait)    (local disk)            └──▶ release ──▶ next in line
```

<p align="center">
  <img src="https://raw.githubusercontent.com/liyifreddy/wombat-gate/main/docs/img/wombat-gate-how-it-works-captioned.jpg" width="100%"
       alt="Four panels: robots queue at the burrow; one goes in with the wombat; inside it puts its crate on its own shelf; it comes out and the next one goes in">
</p>

1. **Wait in queue** — agents line up outside.
2. **Take the lock** — one goes in. The lock lives under `~/.local/state`, outside the
   repo, on a local disk (wombat-gate warns if it isn't).
3. **Commit only your own paths** — it puts down its own crate and nothing else.
4. **Release, next in line** — out it comes; the next one goes in.

<p align="center"><sub>Illustrations generated with Google Gemini and edited by the author.</sub></p>

## 🧭 Under the hood

- **No stuck locks.** If the holder dies, the kernel drops the lock. Nothing to clean up.
- **Path checks.** `.`, the repo root, globs and `:` pathspecs are refused, so nobody
  commits the whole tree by accident.
- **Why it's fast.** Your paths are staged in a private copy of the index and grafted
  onto `HEAD`. The branch moves only if nobody else moved it, and git's own index lock is
  held the whole time. Every self-test run checks the result against plain `git commit`.
- **Status for your paths only**: `wombat-gate status -- PATH...`.
- **Human-only flags** (`--allow-broad`, `run --unsafe`) are refused inside agents.

The step-by-step version is in the [reference](REFERENCE.md).

## 🛠️ Usage

```sh
wombat-gate commit -m "parser: handle empty input" -- src/parser tests/test_parser.py
wombat-gate commit -n -m "..." -- src/parser   # dry run
wombat-gate status -- src/parser               # status of your paths only
wombat-gate run -- git push                    # push, fetch, tag, log, ... (an allow-list)
wombat-gate who                                # who is in the burrow
wombat-gate log -n 50                          # recent timings
```

Set `WOMBAT_SESSION=agent-7` (or `--session`) so `who` can tell you who is in there.
Exit codes, settings and every refused command are in the [reference](REFERENCE.md).

## 🤖 With Claude Code

1. Install the plugin (see [Quick start](#-quick-start)). It brings the CLI, the guard hook
   and a skill with the rules.

2. Tell your sessions the rules, e.g. in `CLAUDE.md`:

   > Commit with `wombat-gate commit -m "..." -- <your paths>`; push with
   > `wombat-gate run -- git push`. No `git add -A`, `git commit -a` or `git stash`.

3. Mark Claude Code as an agent in `.claude/settings.json`, so the human-only flags
   (`--allow-broad`, `run --unsafe`) stay off: `{ "env": { "WOMBAT_NO_ESCAPES": "1" } }`.

Running many sessions? The [playbook](PLAYBOOK.md) covers naming, task cards, review
chains and unattended runs.

## ⚠️ Limits

- Only processes that use wombat-gate queue. Switch every agent over.
- The lock must be on a local disk. Windows-side git can't see a WSL lock.
- Commits get fast; `git status` and `git log` on a slow disk don't.
- Two agents changing the same file: the first commit takes both changes. Give agents
  separate paths.
- One machine only.

The full list is in the [reference](REFERENCE.md#limits-in-full).

## ✅ Self-test

```sh
python3 selftest_wombat_gate.py               # ~1 min, throw-away repos on a local disk
python3 selftest_wombat_gate.py --mutants     # 36 past bugs put back; each must turn a check red
bash hooks/selftest_guard.sh                  # 197 commands the guard must block or let through
```

Every fast commit is compared with what plain `git commit` does on a copy of the repo.

## 📍 Status

- 0.6.2 — before / after picture, language switch.
- 0.6.1 — illustrated README, a tiny wombat in `--help`, pipx packaging, PyPI workflow.
- 0.6.0 — fast commits (private index + tree graft + compare-and-swap).

How it's built and why: [DESIGN.md](DESIGN.md).

## 📄 License

MIT — see [LICENSE](LICENSE).
