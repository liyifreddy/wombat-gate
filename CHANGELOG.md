# Changelog

## 0.8.0 — 2026-11-04 (unreleased)

- Commit already-staged deletions and both ends of renames, including Unicode paths, in fast and classic modes while keeping unrelated staging.
- Preview one row per changed file path and report deletion / rename-pair / other-change counts. `--expect-files N` enforces a reviewed count under the queue lock before a real commit.
- Add `integrate --rebase BRANCH --check COMMAND`: rebase a clean source worktree under the common queue, run required checks there, then fast-forward the target worktree. Stop on conflict, dirty overlap, failed checks or changed HEAD; keep branches/worktrees.
- Add Linux/macOS CI, packaging checks, release-tag metadata validation and a separate OIDC publishing job. Document the PyPI Trusted Publisher setup required to fix `invalid-publisher`.
- Document platform status and Claude Code, Codex and OpenCode Go integration.

## 0.7.0 — 2026-10-05

- Record ownership of git index locks and reclaim only wombat-gate's own stale lock after its holder dies; preserve foreign locks.
- Add `doctor` for lock diagnostics and `log --stats` for timing percentiles.


## 0.6.2

- Add before/after illustrations and language navigation.

## 0.6.1

- Add illustrated README, CLI artwork, pipx packaging and PyPI workflow.

## 0.6.0

- Introduce fast commits using a private index, tree grafting and compare-and-swap ref updates.
