# Running many agent sessions on one project — a playbook

wombat-gate solves one narrow problem: many agents writing to one git working
tree. Once you run ten or more sessions in parallel, most of the trouble is not
git. It is: two sessions doing the same experiment, a session "fixing" a tool
another session is measuring with, nobody knowing which machine is free, a
result nobody can reproduce because no one wrote down the versions.

These are the rules we ended up with after a few weeks of running 10–20 Claude
Code sessions in parallel on one research codebase (with a human lead and one
coordinating "dispatcher" conversation). They are templates, not law; copy what
fits.

Roles used below:

- **Human lead** — decides what matters, approves anything irreversible.
- **Dispatcher** — a planning conversation that writes task cards and keeps the
  dispatch board. It never edits code.
- **Executor sessions** — the agents with tools. Each works on exactly one task
  card at a time.

---

## 1. Name sessions so you can tell them apart in a week

- **One letter per line of work** (e.g. `B` build, `R` research,
  `K` tooling). The letter tells you *what* a session is about.
- **One live session per letter, ever.** Need a second one in parallel? Take a
  new, unused letter and register it first. Two `X` sessions alive at once is
  how two agents end up editing the same files.
- Name format: `<Letter><n>-<MMDD>-<card id>`, e.g. `R-0101-12`. The first
  session of a letter on a day has no number; a reopened one gets 2, 3, …. The
  date is the day it was started; a resumed session keeps its name.
- The name is used everywhere: the session's log file name, the first line of
  its report, the commit message prefix (`[R] ...`), wombat-gate's `--session`.

## 2. Task cards

Every piece of work is a file — a **task card** — written by the dispatcher.
The opening message to a session is one line: "read the status page and your
card at <path>". If a card lacks the fixed header, the session stops and asks.

### 2.1 Fixed header

```
> Assigned to: <session name> · Machine: <name / local> · GPU: <yes/no>
> Machine lease: during this card <machine> is yours (only you start, kill, deploy) — or: read-only on <machine>
> Read first: <status page §> · this card · <other files, one by one>
> May write: <paths, one by one; anything not listed is read-only>
> May ask directly: <which session, about what; empty = no messages to other sessions>
> Report: one message, standard report format (§8)
```

### 2.2 The questions every card answers before anything runs

0. **Duplicate check.** Search the result log, the "what we learned" page and
   the "tried, didn't help" folder for this topic. First line of the card:
   "Already known: …; new here: …". Known things are not redone or reported as
   new.
1. **Line of work** this belongs to.
2. **Which problem it attacks** — where in the pipeline, how big it is now
   (e.g. "loses 30 of 100 episodes"), which report that number comes from.
3. **Why it is unavoidable** — for each possible outcome, which decision it
   changes. If no outcome changes a decision, don't run it.
4. **Feasibility first** — the cheapest probe, intervention or existing data
   that can tell whether this could work at all. No such check yet ⇒ the first
   step of the card *is* that check.
5. **Can we tell the answers apart** — control, noise floor (how much the same
   setup varies by itself), smallest effect we can detect, primary metric, and
   the decision criteria **written down before running**. The answer space
   always includes "the change took effect but something else is the
   bottleneck".
6. **Proof the change is live** — a smoke run through the real entry point (not
   a test harness), a full config diff, per-step logs of the quantity it should
   change, a test alert from the watchdog actually received.
7. **Inputs ready** — the data / weights / environment it needs exist and pass
   their own checks.
8. **What the report must contain** — the success side, the failure side, the
   in-between states, and the bookkeeping (denominators, versions).
9. **Cost** — compute hours for training and evaluation separately, which
   machine, expected finish time, who says when the machine can be released.
10. **What the human decides** — changes of metric or criteria, choices between
    approaches, budgets, new machines.

Light version for zero-compute analysis cards: items 2, 3 and 10 only.

### 2.3 Card numbers

Before writing a card, the dispatcher reserves its number in a single table
(`number · topic · which plan item it serves · status`). Two cards never share a
number; a number is never reused.

## 3. The dispatch board

One page, owned by the dispatcher, with exactly these sections:

| Section | What goes there |
|---|---|
| **Send now** | the prompts to send next, in order, each with its precondition |
| **Queue** | next prompts, by time; for each: session, new or resume, machine, precondition |
| **Waiting on the human** | decisions only the human can make |
| **Can be shut down** | sessions / machines that are done, with who confirmed it |
| **Running, don't touch** | what is running where, expected finish |

Rules that keep the board honest:

- Prompts are written on the board, not improvised in chat; once sent, a prompt
  moves to an archive.
- **Resume in small batches** (≤ 4 at a time). A long session that has been idle
  for hours re-reads its whole context on resume, which is expensive.
- **Hand off, then stop.** A session that has started a long job on a machine
  with a relay and a watchdog ends its turn and writes "how to collect results
  tomorrow". No sleeping, polling or "is it done yet" loops.
- **Status line.** Every session keeps one line at the end of its own log:
  `[status <time>] running / waiting for dispatcher / stuck: why / done; what is
  running, where, expected finish; what needs deciding`. The dispatcher reads
  these instead of asking.
- The dispatcher reminds the human when something is *expected* to finish, and
  names the session and the machine. Not polling is not the same as not looking.

## 4. Who may write what

- Each card lists the paths a session may write. Everything else is read-only.
- **Shared working tree:** commit only your own paths, through wombat-gate
  (`wombat-gate commit -m … -- <paths>`); never whole-tree git commands. A
  `PreToolUse` hook blocks the dangerous ones.
- **Commit at milestones**, not after every edit: card written, code plus
  self-test passing, results in. Look only at your own paths (`status -- <paths>`).
- **One log file per session per day** (`log/<date>_<session>.md`). Shared log
  files get each other's paragraphs swept into commits.
- Documents owned by the dispatcher (status page, rules, result log) are never
  edited by executor sessions; they report and the dispatcher updates. A
  permission rule that denies edits to those folders makes this mechanical.
- Correct mistakes with ~~strikethrough~~ and a dated correction; never leave
  two contradicting statements.

## 5. Sessions talking to each other

Sessions on one machine can message each other — and a message *wakes up* an
idle session, which then acts without anyone having approved it.

- **Inbound messages are held** until the human releases them.
- Sessions may only ask each other **facts and interfaces** (where is file X,
  what format does tool Y print), and only when the card allows it.
- **Receiving a message never authorises action.** If another session points out
  a bug in your tool, answer "confirmed / not confirmed" and put it in your
  report; the dispatcher decides when it gets fixed. (We once had a tool changed
  mid-batch because of a friendly hint; part of a batch was measured with one
  version, the rest with another.)
- Every exchange is noted in both sessions' reports.

## 6. Machines

- **Leases.** At any moment each machine belongs to exactly one session. Only
  the holder starts, kills or deploys; everyone else is read-only (logs, `ps`).
  The lease table lives on the status page.
- **Record versions with every number:** code commit, framework / simulator
  version *and build id* (a library can change build without changing version),
  dataset revision, machine id, hostname, GPU id. Changing machine is changing a
  controlled variable. Never pair results across versions input by input.
- **Outputs on the data disk.** Datasets, checkpoints, caches (pip, uv, HF,
  torch), evaluation outputs: on the data disk. A full system disk can stop a
  cloud machine from booting. Check with `df` and `readlink -f` at setup; guard
  long runs with a "system disk low ⇒ stop and flag" check.
- **Cloning machines:** hard links (package caches ↔ virtualenvs, merged
  datasets) become separate copies; the clone can be much bigger than the source
  or fill up. Estimate the unlinked size first (`du --count-links`), clean caches
  before cloning, check `df` first thing after booting the clone.
- Remote clocks: write times as `remote HH:MM (= local HH:MM)`; run `date`
  before reporting any "now".

## 7. Review chain for shared code

Anything other sessions depend on — measuring tools, batch frameworks,
deliverable code, shared scripts — goes through all of:

1. **Self-test with negative controls.** Every automatic check is first fed a
   deliberately broken input and must turn red. Build the broken input from the
   **real data's distribution**: an error that can never occur in real data
   proves nothing when it is caught. For a bug fix: reproduce before, gone after.
   Print the denominator when reporting green.
2. **Mutation check** where cheap: re-introduce the old bug into a copy and
   confirm its test goes red (wombat-gate's `--mutants` does this).
3. **Code review** (`/code-review`) by a fresh context.
4. **Independent verification** by another session or a verifier sub-agent that
   re-runs the tests and tries to break the change. In our experience the fix
   for one finding regularly introduced the next one; plan for 2–3 rounds.
5. **Real-entry smoke.** It is "running" only after one real episode / 200 real
   steps through the real call chain with the real arguments. A test harness is
   not the entry point (a config value that the real launcher turned into a
   tuple crashed a fully reviewed change on its first real run).
6. **Deploy as its own small card**, between batches, with checksums before
   and after. **While a batch is running, its measuring tools are frozen**; a
   defect found mid-batch means stop, fix, re-test, re-run the batch, and mark
   the old outputs invalid (never delete them).

Two tiers: full chain for shared code; a light tier (self-test plus realistic
negative control) for one-off scripts in your own folder that produce no
reported numbers. Unsure ⇒ full chain.

## 8. Reports

One message per session, in this order:

1. **Conclusion first** — one or two sentences, the single most important number.
2. **Time and machines** — what is running, expected finish (from measured rate
   × remaining work, never a guess), what is queued next, when each machine can
   be released.
3. **What was done** — a table: item · result · evidence (file path / second source).
4. **Numbers** — every number with its denominator or interval and where its
   versions are recorded. Significance always with effect size. Comparisons
   paired on the same inputs (both succeed / only A / only B / both fail).
5. **Surprises and deviations** — anything not as the card said, deliberate
   deviations and why, anything unexplained (say "unexplained"; don't smooth it
   over).
6. **Decisions needed** — numbered, each with options and a recommendation.

The report is also appended to the session's log before it is sent, so nothing
lives only in a terminal.

## 9. Pitfalls we paid for

- **Exit codes lie.** Judge a run by its output files (exists, parses, sane),
  never by exit status. Success codes after crashes, crash codes after good
  runs, and success with the results file never written all happen.
- **Unattended runs:** relay scripts plus a per-machine watchdog with flag files
  (e.g. `ALL_DONE`, `STOP`, `STALL_<job>`, `DISK_LOW`) and an alert channel that has
  been **tested end to end** (a real-length test alert actually received).
  Waiters must tell apart "upstream crashed", "upstream was stopped" and
  "upstream never started" — one "upstream not running ⇒ my turn" rule once let
  a job jump the queue by hours.
- **Never edit a shell script that is running** — bash reads it as it goes.
  Write a new file.
- **tmux:** match session names exactly (`-t =name`); never kill a process by
  pid because its command line mentions tmux (the server keeps its first
  command line); use `tmux kill-session`.
- **Loading weights:** check missing / unexpected / mis-shaped keys and fail; a
  non-strict loader silently builds a different model.
- **Bypassing a launcher script** (calling the Python directly): copy every
  environment variable, `cd` and path setting it makes, and list them.
- **Don't let an LLM look at images in bulk.** A few hundred images can use
  a whole day's budget. Verify with logged physical
  quantities or simulator ground truth instead; if pictures are unavoidable,
  budget them (count × tokens per image) and get approval first.
- **Permission rules match relative paths from the project root.** A deny rule
  for `docs/**` also covers `tools/x/docs/`, and a relative `rmdir docs` run in
  a subdirectory is checked as if it were the top-level `docs`. Use absolute
  paths in destructive commands.
- **Don't extrapolate constants** (horizons, step times, thresholds) from one
  task to another without saying why they should transfer.
- **"We haven't tried X" is not a reason to rule X out.** Only hard facts are:
  weights not public, not enough memory, licence, clearly no time.
