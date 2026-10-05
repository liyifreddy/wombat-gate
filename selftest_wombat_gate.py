#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Self-test for wombat-gate. Builds throw-away repositories on a LOCAL disk and runs:

  negctl  8 concurrent writers using plain `git add` + `git commit` must hit
          index.lock collisions (proves the problem reproduces here; if it
          does not, the positive test proves nothing and we fail loudly)
  queue   8 concurrent writers using `wombat-gate commit` must all succeed, every
          commit must touch only its writer's directory, and the lock-hold
          intervals recorded in the log must never overlap
  mutation  the same 8 wombat-gate writers with the queue switched off (a private lock
          dir per writer): the overlap check above must go red
  mixed   4 plain-git writers + 4 wombat-gate writers at once: every wombat-gate commit succeeds
  plus single-process checks: foreign staged files are left alone, deletions are
  committed, whole-repo pathspecs are refused, "nothing to commit" exits 3,
  a foreign index.lock is retried then reported, the queue lock times out (4),
  a SIGKILLed holder releases the lock, `status` only shows the given paths.

Usage: python3 selftest_wombat_gate.py [--base DIR] [--writers 8] [--commits 15] [--keep] [--mutants]
Refuses to run when --base is on a network / 9p / drvfs mount.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.environ.get("SELFTEST_WOMBAT_BIN") or os.path.join(HERE, "bin", "wombat-gate")  # override = mutation testing
sys.path.insert(0, HERE)

RESULTS = []
PREFIX = [""]  # "[classic] " / "[fast] " while scen_single runs in that commit mode


def check(name, ok, detail=""):
    name = PREFIX[0] + name
    RESULTS.append((name, bool(ok), detail))
    print("%s  %-58s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def base_env(state):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("WOMBAT_") or k.startswith("GIT_") or k == "CLAUDECODE":
            del env[k]
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "WOMBAT_STATE_DIR": state, "WOMBAT_NOTE_EVERY": "3600",
    })
    return env


def sh(argv, cwd, env, check_rc=True):
    r = subprocess.run(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check_rc and r.returncode != 0:
        raise RuntimeError("%s failed: %s" % (argv, r.stderr))
    return r


def make_repo(root, env, n_files):
    os.makedirs(root)
    sh(["git", "init", "-q", "-b", "main"], root, env)
    bulk = os.path.join(root, "bulk")
    os.makedirs(bulk)
    for i in range(n_files):  # a non-trivial index makes each index write slower, like a real repo
        with open(os.path.join(bulk, "f%05d.txt" % i), "w") as f:
            f.write("x%d\n" % i)
    sh(["git", "add", "bulk"], root, env)
    sh(["git", "commit", "-q", "-m", "init"], root, env)
    return root


def n_commits(repo, env):
    return int(sh(["git", "rev-list", "--count", "HEAD"], repo, env).stdout)


# ---------------------------------------------------------------- worker mode

def worker(repo, mode, wid, k, start_at):
    """One concurrent writer: k commits, each touching only w<wid>/."""
    os.chdir(repo)
    d = "w%02d" % wid
    os.makedirs(d, exist_ok=True)
    while time.time() < start_at:
        time.sleep(0.005)
    fails, lockfails = 0, 0
    for i in range(k):
        path = os.path.join(d, "f%03d.txt" % i)
        with open(path, "w") as f:
            f.write("%s %d %d\n" % (mode, wid, i))
        msg = "%s-%s-%03d" % (mode, d, i)
        if mode == "raw":
            r1 = subprocess.run(["git", "add", path], text=True, capture_output=True)
            r = r1 if r1.returncode else subprocess.run(["git", "commit", "-q", "-m", msg, "--", path],
                                                        text=True, capture_output=True)
        else:
            env = None
            if mode == "nolock":  # mutation: private lock dir per writer = queue switched off
                env = dict(os.environ, WOMBAT_LOCK_DIR=os.path.join(os.environ["WOMBAT_STATE_DIR"], "lk-" + d))
            r = subprocess.run([sys.executable, TOOL, "commit", "-q", "--session", d, "-m", msg, "--", d],
                               text=True, capture_output=True, env=env)
        if r.returncode != 0:
            fails += 1
            if ".lock" in r.stderr:
                lockfails += 1
    print(json.dumps({"wid": wid, "mode": mode, "fails": fails, "lockfails": lockfails}))


def run_writers(repo, env, modes, k):
    start_at = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, __file__, "--worker", repo, m, str(i), str(k), repr(start_at)],
                              env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i, m in enumerate(modes)]
    out = []
    for p in procs:
        o, e = p.communicate(timeout=900)
        try:
            out.append(json.loads(o.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            out.append({"mode": "?", "fails": k, "lockfails": 0, "err": e[-300:]})
    return out


def read_log(state):
    try:
        with open(os.path.join(state, "wombat-gate.log")) as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def commits_touch_only_own_dir(repo, env, prefix):
    """For every commit whose subject starts with prefix-wNN-, all changed files are under wNN/."""
    r = sh(["git", "log", "--format=@@%s", "--name-only"], repo, env).stdout
    bad, seen, subj = [], 0, None
    for line in r.splitlines():
        if line.startswith("@@"):
            subj = line[2:]
            if subj.startswith(prefix + "-"):
                seen += 1
            continue
        if line.strip() and subj and subj.startswith(prefix + "-"):
            own = subj.split("-")[1] + "/"
            if not line.startswith(own):
                bad.append((subj, line))
    return seen, bad


# ---------------------------------------------------------------- scenarios

def scen_negctl(tmp, env, w, k, n_files):
    best = None
    for rnd in range(3):  # collisions are probabilistic; give the negative control a few chances
        repo = make_repo(os.path.join(tmp, "neg%d" % rnd), env, n_files)
        out = run_writers(repo, env, ["raw"] * w, k)
        lockfails = sum(o["lockfails"] for o in out)
        fails = sum(o["fails"] for o in out)
        best = (rnd, fails, lockfails, n_commits(repo, env) - 1)
        if lockfails > 0:
            break
    rnd, fails, lockfails, made = best
    check("negctl: plain git, %d writers x %d commits hits index.lock" % (w, k), lockfails > 0,
          "round %d: %d/%d attempts failed (%d on a .lock), %d commits landed" % (rnd + 1, fails, w * k, lockfails, made))
    return lockfails > 0


def overlaps_of(recs):
    iv = sorted((r["t_acq"], r["t_rel"]) for r in recs if "t_acq" in r and "t_rel" in r)
    return len(iv), sum(1 for a, b in zip(iv, iv[1:]) if b[0] < a[1])


def sha_owner_check(name, repo, env, recs, prefix, expect_landed=None):
    """Every rc=0 commit record must name a commit whose subject is the writer's own
    (verifier 0.2 N1: a foreign sha was reported as ours)."""
    ok = [r for r in recs if r.get("op") == "commit" and r.get("rc") == 0]
    wrong = []
    for r in ok:
        subj = sh(["git", "log", "-1", "--format=%s", str(r.get("commit"))], repo, env, check_rc=False).stdout.strip()
        if not subj.startswith("%s-%s-" % (prefix, r.get("session"))):
            wrong.append((r.get("session"), r.get("commit"), subj))
    # exit 3 "swept_by_foreign" must be true: some commit not made by this writer
    # touched the writer's directory (verifier 0.3 M1: 3/3 such claims were false)
    for r in recs:
        if r.get("result") != "swept_by_foreign":
            continue
        d = str(r.get("session"))
        subs = sh(["git", "log", "--format=%s", "--", d + "/"], repo, env).stdout.split("\n")
        if not any(s and not s.startswith("%s-%s-" % (prefix, d)) for s in subs):
            wrong.append((d, "swept_by_foreign", "no foreign commit touched %s/" % d))
    landed = n_commits(repo, env) - 1
    good = not wrong and (expect_landed is None or len(ok) == landed)
    check(name, good, "%d rc=0 records, %d wrong sha, %d commits landed%s"
          % (len(ok), len(wrong), landed, (": %s" % wrong[:2]) if wrong else ""))


def scen_mutation(tmp, env, w, k, n_files):
    state = env["WOMBAT_STATE_DIR"]
    before = len(read_log(state))
    repo = make_repo(os.path.join(tmp, "mutation"), env, n_files)
    run_writers(repo, env, ["nolock"] * w, k)
    recs = read_log(state)[before:]
    n, ov = overlaps_of(recs)
    check("mutation negctl: queue switched off -> overlap check goes red", ov > 0,
          "%d intervals, %d overlaps" % (n, ov))
    sha_owner_check("mutation: even unqueued, rc=0 means own commit landed", repo, env, recs,
                    "nolock", expect_landed=True)


def scen_queue(tmp, env, w, k, n_files):
    state = env["WOMBAT_STATE_DIR"]
    before = len(read_log(state))
    repo = make_repo(os.path.join(tmp, "queue"), env, n_files)
    t = time.time()
    out = run_writers(repo, env, ["queue"] * w, k)
    wall = time.time() - t
    fails = sum(o["fails"] for o in out)
    made = n_commits(repo, env) - 1
    check("queue: wombat-gate, %d writers x %d commits, zero failures" % (w, k), fails == 0 and made == w * k,
          "%d failed, %d/%d commits landed, wall %.1fs" % (fails, made, w * k, wall))
    seen, bad = commits_touch_only_own_dir(repo, env, "queue")
    check("queue: every commit touches only its writer's dir", seen == w * k and not bad,
          "%d commits checked, %d foreign files%s" % (seen, len(bad), (": %s" % bad[:3]) if bad else ""))
    recs = [r for r in read_log(state)[before:] if r.get("op") == "commit"]
    ok = [r for r in recs if r.get("rc") == 0 and "t_acq" in r]
    check("queue: one log line per commit", len(recs) == w * k and len(ok) == w * k,
          "%d lines, %d with rc=0" % (len(recs), len(ok)))
    n_iv, overlaps = overlaps_of(ok)
    check("queue: lock-hold intervals never overlap (serialised)", overlaps == 0 and n_iv == w * k,
          "%d intervals, %d overlaps" % (n_iv, overlaps))
    sha_owner_check("queue: every reported sha is the writer's own", repo, env, recs, "queue", expect_landed=True)
    waits = sorted(r["wait_s"] for r in ok)
    holds = sorted(r["hold_s"] for r in ok)
    if waits:
        print("      wait_s median %.2f p95 %.2f max %.2f | hold_s median %.3f max %.3f" % (
            waits[len(waits) // 2], waits[int(len(waits) * .95)], waits[-1], holds[len(holds) // 2], holds[-1]))


def scen_mixed(tmp, env, w, k, n_files):
    before = len(read_log(env["WOMBAT_STATE_DIR"]))
    repo = make_repo(os.path.join(tmp, "mixed"), env, n_files)
    half = max(1, w // 2)
    out = run_writers(repo, env, ["raw"] * half + ["queue"] * (w - half), k)
    gq = [o for o in out if o["mode"] == "queue"]
    raw = [o for o in out if o["mode"] == "raw"]
    check("mixed: wombat-gate writers succeed alongside plain-git writers", sum(o["fails"] for o in gq) == 0,
          "wombat-gate fails %d/%d; plain-git fails %d/%d (lock %d)" % (
              sum(o["fails"] for o in gq), len(gq) * k, sum(o["fails"] for o in raw), len(raw) * k,
              sum(o["lockfails"] for o in raw)))
    seen, bad = commits_touch_only_own_dir(repo, env, "queue")
    check("mixed: wombat-gate commits touch only own dir", not bad and seen == len(gq) * k, "%d checked" % seen)
    sha_owner_check("mixed: every reported sha is the writer's own", repo, env,
                    read_log(env["WOMBAT_STATE_DIR"])[before:], "queue")


def gq(repo, env, *args, **kw):
    return subprocess.run([sys.executable, TOOL] + list(args), cwd=repo, env=env, text=True,
                          capture_output=True, **kw)


def scen_single(tmp, env, mode):
    """Single-process checks, run once per commit mode (fast = default, classic = 0.5 path)."""
    PREFIX[0] = "[%s] " % mode
    try:
        _scen_single(tmp, dict(env, WOMBAT_COMMIT_MODE=mode), mode)
    finally:
        PREFIX[0] = ""


def _scen_single(tmp, env, mode):
    repo = make_repo(os.path.join(tmp, "single-" + mode), env, 50)
    os.makedirs(os.path.join(repo, "mine"))
    os.makedirs(os.path.join(repo, "theirs"))
    for p in ("mine/a.txt", "theirs/b.txt", "mine/gone.txt"):
        open(os.path.join(repo, p), "w").write("1\n")
    sh(["git", "add", "mine/gone.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "gone", "--", "mine/gone.txt"], repo, env)

    # foreign staged file must stay staged and stay out of our commit
    sh(["git", "add", "theirs/b.txt"], repo, env)
    os.remove(os.path.join(repo, "mine/gone.txt"))
    open(os.path.join(repo, "mine/a.txt"), "w").write("a-distinct\n")
    r = gq(repo, env, "commit", "-m", "mine", "--", "mine")
    files = sh(["git", "show", "--no-renames", "--name-status", "--format=", "HEAD"], repo, env).stdout.split("\n")
    files = [f for f in files if f.strip()]
    staged = sh(["git", "diff", "--cached", "--name-only"], repo, env).stdout.split()
    check("isolation: commit has only mine/ (incl. deletion)",
          r.returncode == 0 and sorted(files) == ["A\tmine/a.txt", "D\tmine/gone.txt"], repr(files))
    check("isolation: someone else's staged file is still staged", staged == ["theirs/b.txt"], repr(staged))
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    steps = last.get("steps", {})
    want = ({"snapshot", "add", "diff", "commit", "diff_tree"} if mode == "classic" else
            {"index_lock", "copy_index", "add", "write_tree", "graft", "head", "diff_tree", "commit_tree",
             "update_ref", "write_index"})
    check("log: per-step timings recorded for a commit",
          set(steps) >= want and all(v >= 0 for v in steps.values()) and last.get("mode") == mode,
          repr((last.get("mode"), sorted(steps))))

    # status shows only the given path
    open(os.path.join(repo, "mine/new.txt"), "w").write("n\n")
    r = gq(repo, env, "status", "--", "mine")
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    check("status: only the given path", r.returncode == 0 and lines and all("mine/" in l for l in lines),
          repr(lines))

    # broad pathspecs refused, nothing committed
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout
    rcs = [gq(repo, env, "commit", "-m", "x", "--", p).returncode for p in (".", ":/", repo, "./")]
    rcs.append(gq(repo, env, "commit", "-m", "x").returncode)
    rcs.append(gq(repo, env, "status").returncode)
    same = sh(["git", "rev-parse", "HEAD"], repo, env).stdout == head
    check("guard: '.', ':/', repo root, './', no paths -> exit 2", rcs == [2] * 6 and same, repr(rcs))
    r = gq(repo, env, "commit", "--allow-broad", "-n", "-m", "x", "--", ".")
    check("guard: --allow-broad lifts the refusal (dry run)", r.returncode == 0, "rc %d" % r.returncode)
    # 0.4.2: argparse abbreviations would turn `--allow` into --allow-broad and
    # `--uns` into --unsafe, slipping past hooks that match the full flag
    # (legal path `mine`, and the error must come from argparse, not the path guard)
    res = [gq(repo, env, "commit", "-n", flag, "-m", "x", "--", "mine") for flag in ("--allow", "--allow-b", "--al")]
    res.append(gq(repo, env, "status", "--allow", "--", "mine"))
    res += [gq(repo, env, "run", flag, "--", "stash") for flag in ("--uns", "--unsaf")]
    res.append(gq(repo, env, "--ver"))
    bad = [(r.returncode, r.stderr.strip()[-60:]) for r in res
           if r.returncode != 2 or "wombat-gate 0." in r.stdout
           or not any(m in r.stderr for m in ("unrecognized arguments", "invalid choice", "are required"))]
    stash = sh(["git", "stash", "list"], repo, env).stdout.strip()
    check("guard: abbreviated flags rejected by argparse on commit / status / run / top level",
          not bad and not stash, repr(bad))

    # 0.4.2: escape hatches are human-only when an agent marker is set
    agent = dict(env, CLAUDECODE="1")
    probes = [("commit", "-n", "--allow-broad", "-m", "x", "--", "."), ("status", "--allow-broad", "--", "."),
              ("run", "--unsafe", "--", "stash", "list")]
    refused = [gq(repo, agent, *a) for a in probes]
    refused.append(gq(repo, dict(env, WOMBAT_NO_ESCAPES="1"), *probes[2]))
    refused.append(gq(repo, dict(agent, WOMBAT_HUMAN_OVERRIDE="yes"), *probes[2]))  # only "1" counts
    allowed = [gq(repo, dict(agent, WOMBAT_HUMAN_OVERRIDE="1"), *a) for a in probes]
    allowed.append(gq(repo, dict(env, CLAUDECODE="0"), *probes[2]))  # marker explicitly off
    check("escape hatches: refused under CLAUDECODE / WOMBAT_NO_ESCAPES, allowed with WOMBAT_HUMAN_OVERRIDE=1",
          all(r.returncode == 2 and "human-only" in r.stderr and "WOMBAT_HUMAN_OVERRIDE" not in r.stderr for r in refused[:4])
          and refused[4].returncode == 2
          and all(r.returncode == 0 for r in allowed),
          repr(([r.returncode for r in refused], [r.returncode for r in allowed])))
    ha = gq(repo, agent, "run", "--", "stash")
    hh = gq(repo, env, "run", "--", "stash")
    pa = gq(repo, agent, "commit", "-m", "x", "--", ".")
    ph = gq(repo, env, "commit", "-m", "x", "--", ".")
    check("path refusal hint: agents are not pointed at --allow-broad, humans are",
          pa.returncode == 2 and "--allow-broad" not in pa.stderr and "ask a human" in pa.stderr
          and ph.returncode == 2 and "pass --allow-broad" in ph.stderr, repr((pa.stderr[-70:], ph.stderr[-50:])))
    ow = gq(repo, dict(env, GITQ_LOCK_DIR="/nonexistent"), "who")
    check("old GITQ_* settings are reported as ignored",
          ow.returncode == 0 and "ignoring GITQ_LOCK_DIR" in ow.stderr, repr(ow.stderr[-90:]))
    check("refusal hint: agents are not pointed at --unsafe, humans are",
          ha.returncode == 2 and "--unsafe" not in ha.stderr and "ask a human" in ha.stderr
          and hh.returncode == 2 and "pass --unsafe" in hh.stderr, repr((ha.stderr[-80:], hh.stderr[-60:])))

    # nothing to commit
    sh(["git", "add", "mine/new.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "n", "--", "mine/new.txt"], repo, env)
    r = gq(repo, env, "commit", "-m", "x", "--", "mine")
    check("nothing to commit -> exit 3", r.returncode == 3, "rc %d %s" % (r.returncode, r.stderr.strip()))

    # foreign index.lock: retried, succeeds once released
    lock = os.path.join(repo, ".git", "index.lock")
    open(os.path.join(repo, "mine/c.txt"), "w").write("c\n")
    open(lock, "w").close()
    e2 = dict(env, WOMBAT_GIT_BACKOFF="0.2", WOMBAT_GIT_RETRIES="8")
    p = subprocess.Popen([sys.executable, TOOL, "commit", "-m", "c", "--", "mine"], cwd=repo, env=e2,
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1.5)
    os.remove(lock)
    o, e = p.communicate(timeout=120)
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    check("foreign index.lock: retried then committed", p.returncode == 0 and last.get("retries", 0) >= 1,
          "rc %d, retries %s" % (p.returncode, last.get("retries")))

    # foreign index.lock never released: exit 5 with a diagnosis, lock not deleted
    open(os.path.join(repo, "mine/d.txt"), "w").write("d\n")
    open(lock, "w").close()
    e3 = dict(env, WOMBAT_GIT_BACKOFF="0.05", WOMBAT_GIT_RETRIES="2")
    head_before = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    r = gq(repo, e3, "commit", "-m", "d", "--", "mine")
    same_head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip() == head_before
    check("stuck index.lock: exit 5, says how old, leaves it alone, nothing committed",
          r.returncode == 5 and "age" in r.stderr and os.path.exists(lock) and same_head,
          "rc %d" % r.returncode)
    os.remove(lock)

    # queue-lock timeout while someone else holds the wombat-gate lock
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time,importlib.util as u,importlib.machinery as m;"
        "l=m.SourceFileLoader('g',%r);g=u.module_from_spec(u.spec_from_loader('g',l));l.exec_module(g);"
        "_,c=g.repo_info();fd=os.open(g.lock_path_for(c),os.O_RDWR|os.O_CREAT);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)") % TOOL],
        cwd=repo, env=env, text=True, stdout=subprocess.PIPE)
    holder.stdout.readline()
    t = time.time()
    r = gq(repo, env, "commit", "--timeout", "1", "-m", "d", "--", "mine")
    check("queue lock held elsewhere: --timeout 1 -> exit 4", r.returncode == 4 and time.time() - t < 10,
          "rc %d after %.1fs" % (r.returncode, time.time() - t))
    # holder SIGKILLed: kernel drops the flock, next wombat-gate goes straight through
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    r = gq(repo, env, "commit", "--timeout", "5", "-m", "d", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    check("killed holder releases the lock", r.returncode == 0 and last.get("wait_s", 99) < 1,
          "rc %d wait %.2fs" % (r.returncode, last.get("wait_s", -1)))

    # run: arbitrary git command under the lock
    r = gq(repo, env, "run", "--", "git", "tag", "t1")
    tags = sh(["git", "tag"], repo, env).stdout.split()
    check("run: executes the git command under the lock", r.returncode == 0 and "t1" in tags, repr(tags))
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout
    # dirty tree so that any destructive command that slipped through would be visible
    open(os.path.join(repo, "theirs/dirty.txt"), "w").write("wip\n")
    open(os.path.join(repo, "theirs/b.txt"), "a").write("more wip\n")
    bad = [["add", "-A"], ["git", "add", "."], ["commit", "-am", "x"], ["stash"], ["clean", "-fd"],
           ["reset", "--" + "hard"], ["checkout", "--", "."], ["restore", ":/"],
           # review 0.1 bypasses: global options first, option prefixes, other spellings
           ["-C", ".", "stash"], ["-c", "x.y=z", "reset", "--" + "hard"], ["reset", "--" + "har"],
           ["reset"], ["checkout", "-f"], ["switch", "--discard-changes", "main"],
           ["checkout", "--", repo], ["rm", "-r", "--cached", "."], ["pull"], ["merge", "t1"]]
    rcs = [gq(repo, env, "run", "--", *b).returncode for b in bad]
    staged = sh(["git", "diff", "--cached", "--name-only"], repo, env).stdout.split()
    dirty = sh(["git", "status", "--porcelain", "--", "theirs"], repo, env).stdout
    check("run: refuses %d destructive / non-allow-listed forms" % len(bad),
          rcs == [2] * len(bad) and staged == ["theirs/b.txt"] and "dirty.txt" in dirty
          and sh(["git", "rev-parse", "HEAD"], repo, env).stdout == head, repr(rcs))

    # run push: not queued, works against a local bare remote, even while the queue is held
    bare = os.path.join(tmp, "remote-%s.git" % mode)
    sh(["git", "init", "-q", "--bare", bare], tmp, env)
    sh(["git", "remote", "add", "origin", bare], repo, env)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time,importlib.util as u,importlib.machinery as m;"
        "l=m.SourceFileLoader('g',%r);g=u.module_from_spec(u.spec_from_loader('g',l));l.exec_module(g);"
        "_,c=g.repo_info();fd=os.open(g.lock_path_for(c),os.O_RDWR|os.O_CREAT);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)") % TOOL],
        cwd=repo, env=env, text=True, stdout=subprocess.PIPE)
    holder.stdout.readline()
    r = gq(repo, env, "run", "--timeout", "2", "--", "push", "-q", "origin", "main")
    rb = sh(["git", "--git-dir", bare, "rev-parse", "main"], tmp, env, check_rc=False).stdout.strip()
    check("run push: bypasses the queue, lands on the remote", r.returncode == 0 and rb == head.strip(),
          "rc %d" % r.returncode)
    holder.kill()
    holder.wait()

    # path guard: pathspec magic, globs, outside the repo (review 0.1 #1)
    open(os.path.join(repo, "theirs/c.md"), "w").write("c\n")
    open(os.path.join(repo, "mine/d.md"), "w").write("d\n")
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout
    rcs = [gq(repo, env, "commit", "-m", "x", "--", p).returncode
           for p in (":!mine", ":^mine", ":/.", "*.md", "mine/*", "**", "..", "/", ":(glob)**")]
    rcs.append(gq(repo, env, "status", "--", ":!mine").returncode)
    staged = sh(["git", "diff", "--cached", "--name-only"], repo, env).stdout.split()
    check("guard: ':!x', ':^x', ':/.', globs, outside repo -> exit 2, nothing staged",
          rcs == [2] * 10 and staged == ["theirs/b.txt"]
          and sh(["git", "rev-parse", "HEAD"], repo, env).stdout == head, repr(rcs))

    # dry run leaves the index alone (review 0.1 #4)
    r = gq(repo, env, "commit", "-n", "-m", "x", "--", "mine")
    staged = sh(["git", "diff", "--cached", "--name-only"], repo, env).stdout.split()
    check("dry run lists files and stages nothing", r.returncode == 0 and "mine/d.md" in r.stdout
          and staged == ["theirs/b.txt"], repr((r.returncode, staged)))

    # a rejecting commit-msg hook: exit 1 and our paths are unstaged again (review 0.1 #4)
    hook = os.path.join(repo, ".git", "hooks", "commit-msg")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho rejected-by-hook >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    r = gq(repo, env, "commit", "-m", "x", "--", "mine")
    staged = sh(["git", "diff", "--cached", "--name-only"], repo, env).stdout.split()
    check("hook rejects: exit 1, own paths unstaged, others' staging kept",
          r.returncode == 1 and "rejected-by-hook" in r.stderr and staged == ["theirs/b.txt"],
          repr((r.returncode, staged)))
    os.remove(hook)

    # -F - : message read once from stdin, survives a lock retry (review 0.1 #8)
    open(lock, "w").close()
    e2 = dict(env, WOMBAT_GIT_BACKOFF="0.2", WOMBAT_GIT_RETRIES="8")
    p = subprocess.Popen([sys.executable, TOOL, "commit", "-F", "-", "--", "mine"], cwd=repo, env=e2,
                         text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write("from stdin\n\nbody line\n")
    p.stdin.close()
    time.sleep(1.0)
    os.remove(lock)
    p.wait(timeout=120)
    subj = sh(["git", "log", "-1", "--format=%s|%b"], repo, env).stdout.strip()
    check("-F - with a lock retry keeps the message", p.returncode == 0 and subj == "from stdin|body line",
          repr((p.returncode, subj)))

    # only real lock collisions are retried (review 0.1 #5)
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("wg_mod2", TOOL)
    g = importlib.util.module_from_spec(importlib.util.spec_from_loader("wg_mod2", loader))
    loader.exec_module(g)
    yes = ["fatal: Unable to create '/r/.git/index.lock': File exists.",
           "error: cannot lock ref 'HEAD': Unable to create '/r/.git/HEAD.lock': File exists."]
    no = ["fatal: unable to write new index file", "error: cannot lock ref 'refs/heads/a/b': 'refs/heads/a' exists",
          "error: cannot lock ref 'refs/heads/x': is at 1234 but expected 5678",
          " ! [remote rejected] main -> main (cannot lock ref 'refs/heads/main')",
          "error: could not lock config file .git/config: Permission denied"]
    check("retry regex: lock collisions yes, disk/ref-conflict/remote errors no",
          all(g.RETRYABLE.search(s) for s in yes) and not any(g.RETRYABLE.search(s) for s in no))

    if mode == "classic":
        _classic_shim_checks(tmp, env, repo)
    _scen_single_tail(tmp, env, repo)


def _classic_shim_checks(tmp, env, repo):
    """Foreign interference around `git commit` (classic path only: the fast path makes
    no `git commit` call; scen_fast has its own interference checks)."""
    # --- verifier 0.2 findings ---------------------------------------------------
    # N1: a foreign commit lands while our commit hits index.lock. 0.2 reported the
    # foreign sha as ours (rc 0, our change left staged). Shim git: on the first
    # `commit`, make a foreign commit, then fail like a lock collision.
    real_git = shutil.which("git", path=env.get("PATH"))

    def shim_env(tag, foreign_cmd, always_fail=False):
        """git shim: on the first `commit`, run foreign_cmd then fail like a lock
        collision; later commits pass through (or keep failing if always_fail)."""
        shim = os.path.join(tmp, "shim-" + tag)
        os.makedirs(shim, exist_ok=True)
        marker = os.path.join(tmp, "shim-%s.fired" % tag)
        lockerr = "echo \"fatal: Unable to create '$PWD/.git/index.lock': File exists.\" >&2; exit 128"
        with open(os.path.join(shim, "git"), "w") as f:
            f.write("#!/bin/sh\nG=%s\n"
                    "case \" $* \" in *\" commit \"*)\n"
                    "  if [ ! -e %s ]; then touch %s\n    %s\n    %s; fi\n"
                    "  %s;;\n"
                    "esac\nexec $G \"$@\"\n" % (real_git, marker, marker, foreign_cmd, lockerr,
                                                lockerr if always_fail else ":"))
        os.chmod(os.path.join(shim, "git"), 0o755)
        return dict(env, PATH=shim + os.pathsep + env["PATH"], WOMBAT_GIT_BACKOFF="0.05"), marker

    # (a) foreign commit that does not take our staged file (built from HEAD's tree)
    es, marker = shim_env("a", "c=$($G commit-tree \"$($G rev-parse 'HEAD^{tree}')\" -p HEAD "
                               "-m FOREIGN-a </dev/null) && $G update-ref HEAD $c")
    open(os.path.join(repo, "mine/n1.txt"), "w").write("n1\n")
    r = gq(repo, es, "commit", "-m", "own-n1", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    rep_subj = sh(["git", "log", "-1", "--format=%s", str(last.get("commit"))], repo, env, check_rc=False).stdout.strip()
    staged = sh(["git", "diff", "--cached", "--name-only", "--", "mine"], repo, env).stdout.split()
    subjects = sh(["git", "log", "-3", "--format=%s"], repo, env).stdout.split("\n")
    check("foreign commit during our lock retry -> we retry, reported sha is ours",
          os.path.exists(marker) and r.returncode == 0 and rep_subj == "own-n1" and not staged
          and "FOREIGN-a" in subjects, repr((r.returncode, rep_subj, staged)))

    # (b) foreign bare `git commit` that sweeps our staged file into its own commit
    es, marker = shim_env("b", "$G commit -q -m FOREIGN-b </dev/null")
    open(os.path.join(repo, "mine/n1.txt"), "w").write("n1-b\n")
    r = gq(repo, es, "commit", "-m", "own-n1b", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    staged = sh(["git", "diff", "--cached", "--name-only", "--", "mine"], repo, env).stdout.split()
    head_subj = sh(["git", "log", "-1", "--format=%s"], repo, env).stdout.strip()
    check("our change swept into a foreign commit -> exit 3, says so, no revert staged",
          os.path.exists(marker) and r.returncode == 3 and last.get("result") == "swept_by_foreign"
          and "another process" in r.stderr and not staged and head_subj == "FOREIGN-b",
          repr((r.returncode, last.get("result"), staged, head_subj)))

    # (c) a foreign path-limited commit takes one of our two files, and our commit
    # keeps failing: the failure path must not restore the old index entry on top of
    # the new HEAD (that would stage a revert of the foreign commit)
    es, marker = shim_env("c", "$G commit -q -m FOREIGN-c -- mine/c1.txt </dev/null", always_fail=True)
    es["WOMBAT_GIT_RETRIES"] = "2"
    open(os.path.join(repo, "mine/c1.txt"), "w").write("c1-old\n")
    open(os.path.join(repo, "mine/c2.txt"), "w").write("c2\n")
    sh(["git", "add", "mine/c1.txt"], repo, env)   # tracked, so the foreign path commit can take it
    # index (our snapshot) says c1-old; the foreign path commit takes the working tree's c1-new
    open(os.path.join(repo, "mine/c1.txt"), "w").write("c1-new\n")
    r = gq(repo, es, "commit", "-m", "own-c", "--", "mine")
    revert = sh(["git", "diff", "--cached", "--name-only", "--", "mine/c1.txt"], repo, env).stdout.split()
    head_subj = sh(["git", "log", "-1", "--format=%s"], repo, env).stdout.strip()
    check("commit fails after a foreign commit took part of our paths -> no revert staged",
          os.path.exists(marker) and r.returncode == 5 and head_subj == "FOREIGN-c" and not revert
          and "HEAD moved" in r.stderr, repr((r.returncode, head_subj, revert)))
    sh(["git", "reset", "-q", "--", "mine/c1.txt"], repo, env)
    sh(["git", "add", "mine/c2.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "c2", "--", "mine/c2.txt"], repo, env)

    # (d) verifier 0.3 M1: a foreign index write drops our staging (file back to
    # untracked) and an unrelated foreign commit lands. 0.3 said "swept by another
    # process" (exit 3) although the change was in no commit.
    es, marker = shim_env("d", "$G rm -q --cached mine/d-new.txt; "
                               "c=$($G commit-tree \"$($G rev-parse 'HEAD^{tree}')\" -p HEAD "
                               "-m FOREIGN-d </dev/null) && $G update-ref HEAD $c")
    open(os.path.join(repo, "mine/d-new.txt"), "w").write("d\n")
    r = gq(repo, es, "commit", "-m", "own-d", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    in_head = sh(["git", "ls-tree", "--name-only", "HEAD", "--", "mine/d-new.txt"], repo, env).stdout.strip()
    head_subj = sh(["git", "log", "-1", "--format=%s"], repo, env).stdout.strip()
    check("dropped staging + unrelated foreign commit -> redo, own commit lands",
          os.path.exists(marker) and r.returncode == 0 and head_subj == "own-d" and in_head == "mine/d-new.txt"
          and last.get("result") == "committed", repr((r.returncode, last.get("result"), head_subj, in_head)))

    # (e) verifier 0.4 (a): a foreign path commit takes one of our two files, our
    # retry commits the other. The printed / logged file list must be the commit's.
    es, marker = shim_env("e", "$G commit -q -m FOREIGN-e -- mine/e1.txt </dev/null")
    for n in ("e1", "e2"):
        open(os.path.join(repo, "mine/%s.txt" % n), "w").write(n + "-old\n")
    sh(["git", "add", "mine/e1.txt", "mine/e2.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "e-base", "--", "mine/e1.txt", "mine/e2.txt"], repo, env)
    for n in ("e1", "e2"):
        open(os.path.join(repo, "mine/%s.txt" % n), "w").write(n + "-new\n")
    r = gq(repo, es, "commit", "-m", "own-e", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    printed = [l for l in r.stdout.splitlines() if l.startswith("M\t")]
    check("reported file list = files in our commit, not the pre-commit diff",
          os.path.exists(marker) and r.returncode == 0 and printed == ["M\tmine/e2.txt"]
          and last.get("n_files") == 1, repr((r.returncode, printed, last.get("n_files"))))

    # (f) verifier 0.4 (b): the redo's `git add` fails -> its own error is shown and logged
    shim = os.path.join(tmp, "shim-f")
    os.makedirs(shim, exist_ok=True)
    mk = os.path.join(tmp, "shim-f.fired")
    with open(os.path.join(shim, "git"), "w") as f:
        f.write("#!/bin/sh\nG=%s\n"
                "case \" $* \" in\n"
                "  *\" commit \"*) if [ ! -e %s ]; then touch %s\n"
                "    $G rm -q --cached mine/f-new.txt\n"
                "    c=$($G commit-tree \"$($G rev-parse 'HEAD^{tree}')\" -p HEAD -m FOREIGN-f </dev/null) && $G update-ref HEAD $c\n"
                "    echo \"fatal: Unable to create '$PWD/.git/index.lock': File exists.\" >&2; exit 128; fi;;\n"
                "  *\" add \"*) if [ -e %s ]; then echo SIMULATED-add-failure >&2; exit 1; fi;;\n"
                "esac\nexec $G \"$@\"\n" % (real_git, mk, mk, mk))
    os.chmod(os.path.join(shim, "git"), 0o755)
    open(os.path.join(repo, "mine/f-new.txt"), "w").write("f\n")
    r = gq(repo, dict(env, PATH=shim + os.pathsep + env["PATH"], WOMBAT_GIT_BACKOFF="0.05"),
           "commit", "-m", "own-f", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    check("redo add fails -> exit 1, its own error shown, logged redo_add_failed",
          os.path.exists(mk) and r.returncode == 1 and "SIMULATED-add-failure" in r.stderr
          and last.get("result") == "redo_add_failed", repr((r.returncode, last.get("result"))))
    sh(["git", "add", "mine/f-new.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "f", "--", "mine/f-new.txt"], repo, env)


def _scen_single_tail(tmp, env, repo):
    # N2: timeout edge cases
    rcs = [gq(repo, env, "commit", "--timeout", t, "-m", "x", "--", "mine").returncode for t in ("0", "-1", "nan", "inf")]
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,os,sys,time,importlib.util as u,importlib.machinery as m;"
        "l=m.SourceFileLoader('g',%r);g=u.module_from_spec(u.spec_from_loader('g',l));l.exec_module(g);"
        "_,c=g.repo_info();fd=os.open(g.lock_path_for(c),os.O_RDWR|os.O_CREAT);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)") % TOOL],
        cwd=repo, env=env, text=True, stdout=subprocess.PIPE)
    holder.stdout.readline()
    open(os.path.join(repo, "mine/n2.txt"), "w").write("n2\n")
    t = time.time()
    r = gq(repo, dict(env, WOMBAT_NOTE_EVERY="15"), "commit", "--timeout", "2", "-m", "x", "--", "mine")
    el = time.time() - t
    holder.kill()
    holder.wait()
    check("--timeout 0/-1/nan/inf -> exit 2; --timeout 2 with 15 s notes gives up in < 8 s",
          rcs == [2, 2, 2, 2] and r.returncode == 4 and el < 8, repr((rcs, r.returncode, round(el, 1))))

    # N3: a failed commit restores others' partial staging under the same path
    os.makedirs(os.path.join(repo, "shared"))
    pf = os.path.join(repo, "shared/partial.md")
    open(pf, "w").write("l1\nl2\n")
    sh(["git", "add", "shared/partial.md"], repo, env)
    sh(["git", "commit", "-q", "-m", "p", "--", "shared/partial.md"], repo, env)
    open(pf, "w").write("l1-staged\nl2\n")
    sh(["git", "add", "shared/partial.md"], repo, env)
    open(pf, "w").write("l1-staged\nl2-unstaged\n")
    open(os.path.join(repo, "shared/new.md"), "w").write("new\n")
    before = sh(["git", "diff", "--cached"], repo, env).stdout
    r = gq(repo, env, "commit", "-m", "", "--", "shared")   # empty message: git refuses
    after = sh(["git", "diff", "--cached"], repo, env).stdout
    check("failed commit puts the index back exactly (partial staging kept, new file unstaged)",
          r.returncode == 1 and before == after and "l1-staged" in after and "new.md" not in after,
          "rc %d, index %s" % (r.returncode, "same" if before == after else "CHANGED"))

    # verifier 0.3 M2: the same failed-commit restore, run from a subdirectory, with a
    # path below cwd and a path via '..'. 0.3 wrote cwd-relative names into the index.
    for sub_path, rel in (("sub/shared", "shared"), ("other", "../other")):
        os.makedirs(os.path.join(repo, sub_path), exist_ok=True)
        f = os.path.join(repo, sub_path, "p.md")
        open(f, "w").write("o1\n")
        sh(["git", "add", sub_path + "/p.md"], repo, env)
        sh(["git", "commit", "-q", "-m", "p-" + sub_path, "--", sub_path + "/p.md"], repo, env)
        open(f, "w").write("o2\n")
        sh(["git", "add", sub_path + "/p.md"], repo, env)
        open(f, "w").write("o3\n")
        open(os.path.join(repo, sub_path, "new.md"), "w").write("n\n")
        before = sh(["git", "diff", "--cached"], repo, env).stdout
        r = subprocess.run([sys.executable, TOOL, "commit", "-m", "", "--", rel], cwd=os.path.join(repo, "sub"),
                           env=env, text=True, capture_output=True)
        after = sh(["git", "diff", "--cached"], repo, env).stdout
        check("failed commit from a subdirectory (-- %s) restores the index exactly" % rel,
              r.returncode == 1 and before == after and "+o2" in after, "rc %d, index %s" % (
                  r.returncode, "same" if before == after else "CHANGED"))
        sh(["git", "reset", "-q", "--", sub_path], repo, env)

    # N4: argument-level refusals inside the allow-list
    sh(["git", "branch", "side"], repo, env)
    readme = os.path.join(repo, "mine/a.txt")
    content = open(readme).read()
    branch = sh(["git", "branch", "--show-current"], repo, env).stdout.strip()
    bad = [["fetch", "--update-head-ok", "origin", "+main:main"], ["fetch", "origin", "main:main"],
           ["fetch", "-u", "origin"], ["fetch", "--upload-pack=x", "origin"],
           ["branch", "-M", "main", "hijacked"], ["branch", "-D", "side"], ["branch", "--del", "side"],
           ["branch", "-f", "side", "HEAD~1"], ["diff", "--output=mine/a.txt"], ["log", "--outp=mine/a.txt"],
           ["push", "--force", "origin", "main"], ["push", "-f", "origin"], ["push", "--forc", "origin"],
           ["push", "origin", "+main"], ["push", "origin", ":side"], ["push", "--delete", "origin", "x"],
           ["push", "--mirror", "origin"], ["tag", "-d", "t1"], ["tag", "-fa", "t1", "-m", "x"],
           ["gc", "--prune=now"], ["maintenance", "run"],
           # verifier 0.3 M3: three-character abbreviations, fetch --prune-tags
           ["tag", "--d", "t1"], ["push", "--m", "origin"], ["fetch", "--prune-tags", "origin"], ["fetch", "-P", "origin"]]
    rcs = [gq(repo, env, "run", "--", *b).returncode for b in bad]
    tags = sh(["git", "tag"], repo, env).stdout.split()
    branches = sh(["git", "branch", "--format=%(refname:short)"], repo, env).stdout.split()
    check("%d state-rewriting argument forms refused, nothing changed" % len(bad),
          rcs == [2] * len(bad) and open(readme).read() == content and "t1" in tags and "side" in branches
          and sh(["git", "branch", "--show-current"], repo, env).stdout.strip() == branch, repr(rcs))
    ok = [gq(repo, env, "run", "--", *b).returncode for b in
          (["branch", "feature-x"], ["tag", "-a", "t2", "-m", "msg"], ["log", "-1", "--oneline"],
           ["fetch", "origin"], ["push", "origin", "feature-x"], ["fetch", "--prune", "origin"])]
    check("ordinary branch / annotated tag / log / fetch / push / fetch --prune still allowed",
          ok == [0] * 6, repr(ok))

    # N5: '[' in file names allowed; symlink pointing outside the repo is committed as a link
    os.makedirs(os.path.join(repo, "mine/sub"), exist_ok=True)
    open(os.path.join(repo, "mine/sub/[x].txt"), "w").write("x\n")
    os.symlink(tmp, os.path.join(repo, "mine/extlink"))
    r1 = gq(repo, env, "commit", "-m", "brackets", "--", "mine/sub/[x].txt")
    r2 = gq(repo, env, "commit", "-m", "link", "--", "mine/extlink")
    mode = sh(["git", "ls-files", "-s", "--", "mine/extlink"], repo, env).stdout.split()[:1]
    check("'[x].txt' commits; outside-pointing symlink commits as a link",
          r1.returncode == 0 and r2.returncode == 0 and mode == ["120000"], repr((r1.returncode, r2.returncode, mode)))

    # log -n 0 prints nothing (review 0.1 #6)
    r0 = gq(repo, env, "log", "-n", "0")
    r2 = gq(repo, env, "log", "-n", "2")
    check("log -n 0 prints nothing, -n 2 prints 2 lines",
          r0.stdout == "" and len(r2.stdout.splitlines()) == 2, repr(len(r0.stdout)))


# ---------------------------------------------------------------- 0.6 fast path

def shim_git(tmp, env, tag, pattern, action, at=(1,), real_git=None):
    """PATH shim for git: on the k-th call (k in `at`) whose ' argv ' matches the shell
    case pattern, run `action` first ($G is the real git; the action may `exit`)."""
    real_git = real_git or shutil.which("git", path=env.get("PATH"))
    shim = os.path.join(tmp, "fshim-" + tag)
    os.makedirs(shim, exist_ok=True)
    cnt = os.path.join(tmp, "fshim-%s.count" % tag)
    with open(os.path.join(shim, "git"), "w") as f:
        f.write("#!/bin/sh\nG=%s\n"
                "case \" $* \" in %s)\n"
                "  n=$(( $(cat %s 2>/dev/null || echo 0) + 1 )); echo $n > %s\n"
                "  case \" %s \" in *\" $n \"*) %s;; esac;;\n"
                "esac\nexec $G \"$@\"\n" % (real_git, pattern, cnt, cnt, " ".join(map(str, at)), action))
    os.chmod(os.path.join(shim, "git"), 0o755)
    return dict(env, PATH=shim + os.pathsep + env["PATH"], WOMBAT_GIT_BACKOFF="0.05"), cnt


# A commit made with plumbing only (no index, no index.lock), the way another
# worktree or a script would move HEAD: sets PATH_ (in the tree) to CONTENT_.
PLUMBING_COMMIT = ("T=$(mktemp); rm -f $T; GIT_INDEX_FILE=$T $G read-tree HEAD; "
                   "b=$(printf '%s' \"$CONTENT_\" | $G hash-object -w --stdin); "
                   "GIT_INDEX_FILE=$T $G update-index --add --cacheinfo 100644,$b,\"$PATH_\"; "
                   "t=$(GIT_INDEX_FILE=$T $G write-tree); rm -f $T; "
                   "c=$($G commit-tree $t -p HEAD -m \"$MSG_\" </dev/null); $G update-ref HEAD $c")


def plumbing(path, content, msg):
    return "PATH_=%s CONTENT_=%s MSG_=%s; %s" % (shlex.quote(path), shlex.quote(content), shlex.quote(msg),
                                                 PLUMBING_COMMIT)


def subj(repo, env, rev="HEAD"):
    return sh(["git", "log", "-1", "--format=%s", rev], repo, env, check_rc=False).stdout.strip()


def write(repo, rel, text):
    p = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def scen_fast(tmp, env):
    env = dict(env, WOMBAT_COMMIT_MODE="fast")
    state = env["WOMBAT_STATE_DIR"]
    repo = make_repo(os.path.join(tmp, "fast"), env, 20)
    write(repo, "theirs/t.txt", "t0\n")
    write(repo, "mine/base.txt", "b0\n")
    sh(["git", "add", "theirs/t.txt", "mine/base.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "base"], repo, env)

    # (fa) HEAD moved by plumbing between our build and our update-ref: the
    # compare-and-swap fails, we graft onto the new HEAD, the other change survives
    es, _ = shim_git(tmp, env, "fa", '*" update-ref "*', plumbing("theirs/t.txt", "foreign-a\n", "FOREIGN-a"))
    write(repo, "mine/a.txt", "a\n")
    r = gq(repo, es, "commit", "-m", "own-a", "--", "mine")
    last = read_log(state)[-1]
    t_in_head = sh(["git", "show", "HEAD:theirs/t.txt"], repo, env, check_rc=False).stdout
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    check("fast: HEAD moved before our update-ref -> re-graft on top, the other change kept",
          r.returncode == 0 and last.get("rebuilds") == 1 and subj(repo, env) == "own-a"
          and subj(repo, env, "HEAD~1") == "FOREIGN-a" and t_in_head == "foreign-a\n"
          and last.get("commit") == head, repr((r.returncode, last.get("rebuilds"), subj(repo, env), t_in_head)))
    sh(["git", "reset", "-q", "--", "theirs/t.txt"], repo, env)   # the plumbing commit left the index behind
    write(repo, "theirs/t.txt", "foreign-a\n")

    # (fb) the other commit already holds our change: nothing left -> exit 3
    es, _ = shim_git(tmp, env, "fb", '*" update-ref "*', plumbing("mine/b.txt", "b\n", "FOREIGN-b"))
    write(repo, "mine/b.txt", "b\n")
    r = gq(repo, es, "commit", "-m", "own-b", "--", "mine")
    last = read_log(state)[-1]
    staged = sh(["git", "diff", "--cached", "--name-only", "--", "mine"], repo, env).stdout.split()
    check("fast: our change already in the other commit -> exit 3, says so, index clean",
          r.returncode == 3 and last.get("result") == "swept_by_foreign" and "another process" in r.stderr
          and subj(repo, env) == "FOREIGN-b" and not staged, repr((r.returncode, last.get("result"), staged)))

    # (fc) HEAD moves before every update-ref -> give up with 5, nothing of ours lands
    es, _ = shim_git(tmp, env, "fc", '*" update-ref "*',
                     "PATH_=theirs/t.txt CONTENT_=$(date +%N) MSG_=FOREIGN-c; " + PLUMBING_COMMIT,
                     at=range(1, 20))
    write(repo, "mine/c.txt", "c\n")
    r = gq(repo, es, "commit", "-m", "own-c", "--", "mine")
    last = read_log(state)[-1]
    ours = [s for s in sh(["git", "log", "--format=%s"], repo, env).stdout.split("\n") if s == "own-c"]
    check("fast: HEAD keeps moving -> exit 5 after 6 tries, no commit of ours",
          r.returncode == 5 and last.get("result") == "head_kept_moving" and not ours,
          repr((r.returncode, last.get("result"), len(ours))))
    sh(["git", "reset", "-q", "--", "theirs/t.txt"], repo, env)
    sh(["git", "checkout", "-q", "--", "theirs/t.txt"], repo, env)

    # (fe) review 0.6 #1: a bare `git commit` by a non-wombat-gate process while we are
    # committing must not get in (we hold index.lock from start to end)
    rcf = os.path.join(tmp, "fe.rc")
    es, _ = shim_git(tmp, env, "fe", '*" commit-tree "*',
                     "$G add theirs/u.txt >/dev/null 2>&1; $G commit -q -m FOREIGN-e >/dev/null 2>&1; echo $? > %s" % rcf)
    write(repo, "theirs/u.txt", "u\n")
    write(repo, "mine/e.txt", "e\n")
    r = gq(repo, es, "commit", "-m", "own-e", "--", "mine")
    frc = open(rcf).read().strip() if os.path.exists(rcf) else "?"
    subjects = sh(["git", "log", "-3", "--format=%s"], repo, env).stdout.split("\n")
    in_head = sh(["git", "ls-tree", "-r", "--name-only", "HEAD", "--", "mine/e.txt"], repo, env).stdout.strip()
    check("fast: a bare git commit during ours is locked out (index.lock held throughout)",
          r.returncode == 0 and frc not in ("0", "?") and "FOREIGN-e" not in subjects and in_head == "mine/e.txt",
          repr((r.returncode, frc, subjects)))
    sh(["git", "add", "theirs/u.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "u", "--", "theirs/u.txt"], repo, env)

    # HEAD unreadable at the start (I/O error text on stderr): 0.5 took it for an
    # unborn branch / a moved HEAD
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    es, _ = shim_git(tmp, env, "fd", '*" rev-parse --verify -q HEAD "*',
                     "echo 'error: unable to read HEAD: Input/output error' >&2; exit 128")
    write(repo, "mine/d.txt", "d\n")
    r = gq(repo, es, "commit", "-m", "own-d", "--", "mine")
    last = read_log(state)[-1]
    lock_left = os.path.exists(os.path.join(repo, ".git", "index.lock"))
    check("fast: HEAD unreadable at start -> exit 1, says so, nothing committed, index.lock released",
          r.returncode == 1 and last.get("result") == "head_unreadable" and "cannot read HEAD" in r.stderr
          and "HEAD moved" not in r.stderr and not lock_left
          and sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip() == head,
          repr((r.returncode, last.get("result"), lock_left, r.stderr[-120:])))

    # exec bit staged with --chmod=+x on a core.fileMode=false repo is committed (0.5 lost it)
    sh(["git", "config", "core.fileMode", "false"], repo, env)
    write(repo, "mine/run.sh", "#!/bin/sh\n")
    sh(["git", "add", "mine/run.sh"], repo, env)
    sh(["git", "update-index", "--chmod=+x", "mine/run.sh"], repo, env)
    r = gq(repo, env, "commit", "-m", "x-bit", "--", "mine")
    mode = sh(["git", "ls-tree", "HEAD", "--", "mine/run.sh"], repo, env).stdout.split()[:1]
    check("fast: exec bit staged with --chmod=+x (core.fileMode=false) is committed",
          r.returncode == 0 and mode == ["100755"], repr((r.returncode, mode)))
    sh(["git", "config", "core.fileMode", "true"], repo, env)
    os.chmod(os.path.join(repo, "mine/run.sh"), 0o755)  # match the committed mode again

    # hooks: prepare-commit-msg edits, commit-msg sees the index being committed,
    # post-commit runs after the index is written and its output is shown;
    # --no-verify; core.hooksPath; a pre-commit hook sends the commit down the classic path
    hooks = os.path.join(repo, ".git", "hooks")

    def hook(name, body, d=hooks):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(os.path.join(d, name), 0o755)
    hook("prepare-commit-msg", 'echo "PCM-ADDED $2" >> "$1"\n')
    hook("commit-msg", 'echo "CM-SEES=[$(git diff --cached --name-only HEAD -- mine)]"\n')
    hook("post-commit", 'echo "POST-STAGED=[$(git diff --cached --name-only -- mine)]"\n')
    write(repo, "mine/h.txt", "h\n")
    r = gq(repo, env, "commit", "-m", "own-h", "--", "mine")
    body = sh(["git", "log", "-1", "--format=%B"], repo, env).stdout
    check("fast: hooks -- prepare-commit-msg edits, commit-msg sees our files, post-commit after the index",
          r.returncode == 0 and "PCM-ADDED message" in body and "CM-SEES=[mine/h.txt]" in r.stdout
          and "POST-STAGED=[]" in r.stdout, repr((r.returncode, body, r.stdout[-160:])))
    for n in ("prepare-commit-msg", "post-commit"):
        os.remove(os.path.join(hooks, n))
    hook("commit-msg", "echo rejected-by-hook >&2\nexit 1\n")
    write(repo, "mine/h2.txt", "h2\n")
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    r1 = gq(repo, env, "commit", "-m", "own-h2", "--", "mine")
    same = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip() == head
    staged = sh(["git", "diff", "--cached", "--name-only", "--", "mine"], repo, env).stdout.split()
    r2 = gq(repo, env, "commit", "--no-verify", "-m", "own-h2", "--", "mine")
    check("fast: commit-msg rejection -> exit 1, nothing committed or staged; --no-verify skips it",
          r1.returncode == 1 and "rejected-by-hook" in r1.stderr and same and not staged and r2.returncode == 0
          and subj(repo, env) == "own-h2", repr((r1.returncode, same, staged, r2.returncode)))
    os.remove(os.path.join(hooks, "commit-msg"))
    alt = os.path.join(tmp, "fast-hookspath")
    hook("commit-msg", "echo rejected-by-hookspath >&2\nexit 1\n", d=alt)
    sh(["git", "config", "core.hooksPath", alt], repo, env)
    write(repo, "mine/h3.txt", "h3\n")
    r = gq(repo, env, "commit", "-m", "own-h3", "--", "mine")
    sh(["git", "config", "--unset", "core.hooksPath"], repo, env)
    check("fast: core.hooksPath is honoured", r.returncode == 1 and "rejected-by-hookspath" in r.stderr,
          repr(r.returncode))

    # fallbacks to the classic path
    def mode_of(*cmd, **cfg):
        for k, v in cfg.items():
            sh(["git", "config", k.replace("_", "."), v], repo, env)
        write(repo, "mine/fb.txt", "%f\n" % time.time())
        r = gq(repo, env, "commit", *cmd, "--", *(["MINE"] if "ci" in cmd else ["mine"]))
        for k in cfg:
            sh(["git", "config", "--unset", k.replace("_", ".")], repo, env)
        rec = read_log(state)[-1]
        return r.returncode, rec.get("mode"), rec.get("classic_reason", "")
    hook("pre-commit", "exit 0\n")
    pc = mode_of("-m", "pc")
    pcn = mode_of("--no-verify", "-m", "pcn")
    os.remove(os.path.join(hooks, "pre-commit"))
    gpg = mode_of("-m", "gpg", commit_gpgsign="true", gpg_program="false")
    open(os.path.join(repo, ".git", "MERGE_HEAD"), "w").write(head + "\n")
    mh = mode_of("-m", "mh")
    os.remove(os.path.join(repo, ".git", "MERGE_HEAD"))
    sh(["git", "config", "core.ignorecase", "true"], repo, env)
    ci = mode_of("-m", "ci")
    sh(["git", "config", "core.ignorecase", "false"], repo, env)
    check("fast: classic path for pre-commit hook / commit.gpgSign / merge / wrong-case path on ignorecase",
          pc[1] == "classic" and "pre-commit" in pc[2] and pcn[1] == "fast"
          and gpg[1] == "classic" and "gpgSign" in gpg[2] and gpg[0] == 1
          and mh[1] == "classic" and "MERGE_HEAD" in mh[2] and ci[1] == "classic" and "case" in ci[2],
          repr((pc, pcn, gpg, mh, ci)))
    sh(["git", "checkout", "-q", "--", "."], repo, dict(env, GIT_LITERAL_PATHSPECS="0"))

    # the only file of a directory deleted, PATH = that file: the directory must go
    # too (git stores no empty trees)
    write(repo, "mine/solo/deep/only.txt", "o\n")
    sh(["git", "add", "mine/solo/deep/only.txt"], repo, env)
    sh(["git", "commit", "-q", "-m", "solo", "--", "mine/solo/deep/only.txt"], repo, env)
    os.remove(os.path.join(repo, "mine/solo/deep/only.txt"))
    r = gq(repo, env, "commit", "-q", "-m", "solo-gone", "--", "mine/solo/deep/only.txt")
    left = sh(["git", "ls-tree", "HEAD", "--", "mine/solo"], repo, env).stdout.strip()
    check("fast: deleting a directory's only file removes the emptied directories from the tree",
          r.returncode == 0 and left == "" and sh(["git", "fsck", "--no-dangling", "--no-progress"], repo, env,
                                                   check_rc=False).returncode == 0, repr((r.returncode, left)))

    # unborn branch: the first commit of a repository
    fresh = os.path.join(tmp, "fast-unborn")
    os.makedirs(fresh)
    sh(["git", "init", "-q", "-b", "main"], fresh, env)
    write(fresh, "a/x.txt", "x\n")
    r = gq(fresh, env, "commit", "-m", "first", "--", "a")
    gs = sh(["git", "reflog", "-1", "--format=%gs"], fresh, env, check_rc=False).stdout.strip()
    files = sh(["git", "ls-files"], fresh, env).stdout.split()
    check("fast: first commit on an unborn branch", r.returncode == 0 and gs == "commit (initial): first"
          and files == ["a/x.txt"] and n_commits(fresh, env) == 1, repr((r.returncode, gs, files)))

    scen_fast_oracle(tmp, env)
    scen_fast_messages(tmp, env)


def scen_fast_oracle(tmp, env):
    """Random edits under several directories, plus staged states git treats
    specially (force-added ignored files, intent-to-add, `rm --cached`). Before each
    wombat-gate commit the repository is copied and the copy runs the real thing,
    `git add -A -- PATHS && git commit -- PATHS`. Commit tree and index entries under
    PATHS must match; a foreign staged file must stay staged."""
    import random
    rnd = random.Random(1005)
    repo = make_repo(os.path.join(tmp, "fast-oracle"), env, 30)
    dirs = ["A", "A/sub", "A/sub/deep", "B", "C d", "ü", "[x]", "E"]
    names = ["f1.txt", "f 2.txt", "[k].md", "g.py", "x.log"]
    write(repo, ".gitignore", "*.log\n")
    for d in dirs:
        write(repo, d + "/" + names[0], d + "\n")
    write(repo, "Z/foreign.txt", "z0\n")
    sh(["git", "add", "--all", "--", ".gitignore", "A", "B", "C d", "ü", "[x]", "E", "Z"], repo, env)
    sh(["git", "commit", "-q", "-m", "seed"], repo, env)
    write(repo, "Z/foreign.txt", "z-staged\n")
    sh(["git", "add", "Z/foreign.txt"], repo, env)  # someone else's staging, must survive
    lit = dict(env, GIT_LITERAL_PATHSPECS="1")
    bad, n_commit, n_nothing, n_special, n_refused = [], 0, 0, 0, 0
    for rnd_i in range(30):
        for _ in range(rnd.randint(1, 6)):
            d = rnd.choice(dirs)
            p = os.path.join(repo, d, rnd.choice(names))
            rel = os.path.relpath(p, repo)
            op = rnd.random()
            if op < 0.35:
                write(repo, rel, "%d %f\n" % (rnd_i, rnd.random()))
            elif op < 0.5 and os.path.exists(p):
                os.remove(p)
            elif op < 0.57:
                shutil.rmtree(os.path.join(repo, d), ignore_errors=True)  # whole dir gone
            elif op < 0.65 and os.path.isfile(p):
                os.chmod(p, os.stat(p).st_mode ^ 0o111)
            elif op < 0.72:
                write(repo, d + "/x.log", "forced %d\n" % rnd_i)  # ignored, force-added
                sh(["git", "add", "-f", "--", d + "/x.log"], repo, lit)
                n_special += 1
            elif op < 0.79 and not os.path.exists(p):
                write(repo, rel, "ita %d\n" % rnd_i)
                sh(["git", "add", "-N", "--", rel], repo, lit)  # intent-to-add
                n_special += 1
            elif op < 0.86 and os.path.isfile(p) and sh(["git", "ls-files", "--", rel], repo, lit).stdout.strip():
                sh(["git", "rm", "-q", "--cached", "--", rel], repo, lit)  # untracked again, file stays
                n_special += 1
            elif not os.path.lexists(p + ".lnk"):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                os.symlink("target-%d" % rnd_i, p + ".lnk")
        cands = list(dirs) + sh(["git", "ls-files", "--", "A", "B", "E"], repo, env).stdout.split("\n")
        cands = [c for c in cands if c and (os.path.lexists(os.path.join(repo, c)) or
                 sh(["git", "ls-tree", "--name-only", "HEAD", "--", c], repo, lit).stdout.strip())]
        rels = sorted(set(rnd.sample(cands, min(len(cands), rnd.randint(1, 3)))))
        rels = [r for r in rels if not any(r.startswith(o + "/") for o in rels)]
        # the oracle: real git on a copy
        ocopy = os.path.join(tmp, "fast-oracle-copy")
        shutil.rmtree(ocopy, ignore_errors=True)
        shutil.copytree(repo, ocopy, symlinks=True)
        base = sh(["git", "rev-parse", "HEAD^{tree}"], repo, env).stdout.strip()
        pre_idx = sh(["git", "ls-files", "-s", "--"] + rels, ocopy, lit).stdout
        oa = sh(["git", "add", "--all", "--"] + rels, ocopy, lit, check_rc=False)
        staged = sh(["git", "diff", "--cached", "--quiet", "HEAD", "--"] + rels, ocopy, lit, check_rc=False).returncode
        oc = sh(["git", "commit", "-q", "-m", "o", "--"] + rels, ocopy, lit, check_rc=False) if staged else None
        expect = sh(["git", "rev-parse", "HEAD^{tree}"], ocopy, env).stdout.strip()
        refused = oa.returncode != 0 or (oc is not None and oc.returncode != 0)
        expect_idx = pre_idx if refused else sh(["git", "ls-files", "-s", "--"] + rels, ocopy, lit).stdout
        cwd, args = repo, list(rels)
        if rnd_i % 5 == 4:  # from a subdirectory, with '..'
            os.makedirs(os.path.join(repo, "E"), exist_ok=True)
            cwd, args = os.path.join(repo, "E"), [os.path.join("..", r) for r in rels]
        r = subprocess.run([sys.executable, TOOL, "commit", "-q", "-m", "r%d" % rnd_i, "--"] + args,
                           cwd=cwd, env=env, text=True, capture_output=True)
        got = sh(["git", "rev-parse", "HEAD^{tree}"], repo, env).stdout.strip()
        got_idx = sh(["git", "ls-files", "-s", "--"] + rels, repo, lit).stdout
        if refused:  # git refused (e.g. a path known nowhere): so must we, changing nothing
            n_refused += 1
            ok = r.returncode == 1 and got == base
        elif not staged:
            n_nothing += 1
            ok = r.returncode == 3 and got == base
        else:
            n_commit += 1
            ok = r.returncode == 0 and got == expect
        foreign = sh(["git", "diff", "--cached", "--name-only", "--", "Z"], repo, env).stdout.split()
        if not ok or got_idx != expect_idx or foreign != ["Z/foreign.txt"]:
            lg = set(sh(["git", "ls-tree", "-r", "HEAD"], repo, env).stdout.splitlines())
            le = set(sh(["git", "ls-tree", "-r", "HEAD"], ocopy, env).stdout.splitlines())
            bad.append((rnd_i, rels, r.returncode, r.stderr.strip()[-150:], ok, got_idx == expect_idx, foreign,
                        sorted(lg - le)[:3], sorted(le - lg)[:3]))
            if len(bad) > 3:
                break
    shutil.rmtree(os.path.join(tmp, "fast-oracle-copy"), ignore_errors=True)
    check("fast oracle: commit tree = real git commit -- PATHS (%d commits, %d nothing, %d refused, %d special stagings)"
          % (n_commit, n_nothing, n_refused, n_special), not [b for b in bad if not b[4]] and n_commit >= 15 and n_special >= 5,
          repr(bad[:2]))
    check("fast oracle: index under PATHS = real git's, foreign staging kept",
          not bad and n_commit >= 15, repr(bad[:2]))


def scen_fast_messages(tmp, env):
    """Same change + same message through classic (git commit) and fast: identical
    message bytes, tree and identity, also with commit.cleanup=strip."""
    base = make_repo(os.path.join(tmp, "msg-base"), env, 5)
    clones = {}
    for m in ("classic", "fast"):
        clones[m] = os.path.join(tmp, "msg-" + m)
        sh(["git", "clone", "-q", base, clones[m]], tmp, env)
    msgfile = os.path.join(tmp, "msg-file.txt")
    with open(msgfile, "w") as f:
        f.write("\n\nfrom file  \n\n\n# kept unless strip\nlast\n\n")
    cases = [["-m", "  subject  \n\n\nbody line   \n# hash line\n\n\n"], ["-m", "only"],
             ["-m", "a", "-m", "b\n\n\n"], ["-F", msgfile], ["cleanup=strip", "-m", "# c\nsubj\n# d\n"]]
    diffs = []
    for i, case in enumerate(cases):
        out = {}
        for m, repo in clones.items():
            argv = list(case)
            if argv[0] == "cleanup=strip":
                sh(["git", "config", "commit.cleanup", "strip"], repo, env)
                argv = argv[1:]
            write(repo, "m/n.txt", "%d\n" % i)
            r = gq(repo, dict(env, WOMBAT_COMMIT_MODE=m), "commit", *argv, "--", "m")
            out[m] = (r.returncode, sh(["git", "log", "-1", "--format=%B|%T|%an|%ae|%cn|%ce"], repo, env).stdout)
        if out["classic"][0] != 0 or out["classic"] != out["fast"]:
            diffs.append((i, out))
    check("fast: message parity with git commit (%d cases incl. -F, -m -m, cleanup=strip)" % len(cases),
          not diffs, repr(diffs[:1]))


def scen_fast_review2(tmp, env):
    """Verifier findings on 0.6 (N1 racy entries, N2 signals, N4 non-UTF-8 names,
    N5 unmerged entry elsewhere, invalid commit.cleanup)."""
    env = dict(env, WOMBAT_COMMIT_MODE="fast")
    state = env["WOMBAT_STATE_DIR"]

    # N1: a racily-clean entry (file rewritten within the index's timestamp tick,
    # same size) must be re-read, as git does; 0.6-draft committed the old content
    repo = make_repo(os.path.join(tmp, "racy"), env, 5)
    sh(["git", "config", "core.trustctime", "false"], repo, env)
    f = os.path.join(repo, "d", "f")
    write(repo, "d/f", "aaaa\n")
    sh(["git", "add", "d/f"], repo, env)
    st = os.stat(f)
    write(repo, "d/f", "bbbb\n")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.utime(os.path.join(repo, ".git", "index"), ns=(st.st_atime_ns, st.st_mtime_ns))
    time.sleep(1.1)
    r = gq(repo, env, "commit", "-q", "-m", "racy", "--", "d")
    got = sh(["git", "show", "HEAD:d/f"], repo, env, check_rc=False).stdout
    check("fast: racily-clean file is re-read (commits what is on disk)",
          r.returncode == 0 and got == "bbbb\n", repr((r.returncode, got)))

    # N2: SIGTERM while we hold index.lock -> lock removed, nothing committed
    repo = make_repo(os.path.join(tmp, "sigterm"), env, 5)
    hk = os.path.join(repo, ".git", "hooks", "commit-msg")
    with open(hk, "w") as fh:
        fh.write("#!/bin/sh\nsleep 5\n")
    os.chmod(hk, 0o755)
    write(repo, "d/x", "x\n")
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    p = subprocess.Popen([sys.executable, TOOL, "commit", "-m", "t", "--", "d"], cwd=repo, env=env,
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lock = os.path.join(repo, ".git", "index.lock")
    deadline = time.time() + 10
    while time.time() < deadline and not os.path.exists(lock):
        time.sleep(0.05)
    held = os.path.exists(lock)
    time.sleep(0.5)
    p.send_signal(signal.SIGTERM)
    o, e = p.communicate(timeout=30)
    left = os.path.exists(lock)
    same = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip() == head
    check("fast: SIGTERM while holding index.lock -> lock removed, exit 143, nothing committed",
          held and p.returncode == 143 and not left and same and read_log(state)[-1].get("result") == "signalled",
          repr((held, p.returncode, left, same, e[-120:])))

    # N4: a non-UTF-8 file name elsewhere in the tree must not break the graft
    repo = make_repo(os.path.join(tmp, "nonutf8"), env, 5)
    bad = os.path.join(repo.encode(), b"bad\xffname")
    with open(bad, "wb") as fh:
        fh.write(b"x\n")
    subprocess.run([b"git", b"add", b"--", b"bad\xffname"], cwd=repo, env=env, check=True)
    sh(["git", "commit", "-q", "-m", "bad name"], repo, env)
    write(repo, "d/a", "2\n")
    r = gq(repo, env, "commit", "-q", "-m", "m", "--", "d")
    still = subprocess.run([b"git", b"ls-files", b"-z"], cwd=repo, env=env, stdout=subprocess.PIPE).stdout
    check("fast: a non-UTF-8 file name elsewhere in the tree does not break the commit",
          r.returncode == 0 and b"bad\xffname" in still and read_log(state)[-1].get("mode") == "fast",
          repr((r.returncode, r.stderr[-120:])))

    # N5: an unmerged entry outside PATH (no merge in progress) -> classic path, commit lands
    repo = make_repo(os.path.join(tmp, "unmerged"), env, 5)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, env=env, input="c\n", text=True,
                          stdout=subprocess.PIPE).stdout.strip()
    info = "".join("100644 %s %d\to/c\n" % (blob, k) for k in (1, 2, 3))
    subprocess.run(["git", "update-index", "--index-info"], cwd=repo, env=env, input=info, text=True, check=True)
    write(repo, "d/a", "a\n")
    r = gq(repo, env, "commit", "-q", "-m", "u", "--", "d")
    last = read_log(state)[-1]
    check("fast: unmerged entry outside PATH -> classic path, commit lands",
          r.returncode == 0 and last.get("mode") == "classic" and "write-tree" in last.get("classic_reason", "")
          and subj(repo, env) == "u", repr((r.returncode, last.get("mode"), last.get("classic_reason"))))

    # invalid commit.cleanup: git refuses, so must we
    repo = make_repo(os.path.join(tmp, "cleanup"), env, 5)
    sh(["git", "config", "commit.cleanup", "bogus"], repo, env)
    write(repo, "d/a", "a\n")
    head = sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip()
    r = gq(repo, env, "commit", "-q", "-m", "c", "--", "d")
    check("fast: invalid commit.cleanup -> exit 1, nothing committed, lock removed",
          r.returncode == 1 and "Invalid cleanup mode" in r.stderr
          and sh(["git", "rev-parse", "HEAD"], repo, env).stdout.strip() == head
          and not os.path.exists(os.path.join(repo, ".git", "index.lock")), repr((r.returncode, r.stderr[-100:])))


def scen_head_unreadable(tmp, env):
    """Classic path: commit fails, then HEAD cannot be read (silently). 0.5 called that
    'HEAD moved', blamed a foreign commit and retried / left the index alone."""
    env = dict(env, WOMBAT_COMMIT_MODE="classic")
    repo = make_repo(os.path.join(tmp, "headfail"), env, 5)
    # two shims in a chain: commit shim -> rev-parse shim -> git
    es, _ = shim_git(tmp, env, "hc", '*" rev-parse --verify -q HEAD "*', "exit 1", at=(2, 3, 4, 5, 6))
    es, _ = shim_git(tmp, es, "hc2", '*" commit "*',
                     "echo \"fatal: Unable to create '$PWD/.git/index.lock': File exists.\" >&2; exit 128",
                     real_git=os.path.join(tmp, "fshim-hc", "git"))
    es["WOMBAT_GIT_RETRIES"] = "0"
    write(repo, "mine/x.txt", "x\n")
    r = gq(repo, es, "commit", "-m", "own-x", "--", "mine")
    last = read_log(env["WOMBAT_STATE_DIR"])[-1]
    check("classic: HEAD unreadable after a failed commit -> exit 1, not 'HEAD moved'",
          r.returncode == 1 and last.get("result") == "head_unreadable" and "HEAD moved" not in r.stderr
          and n_commits(repo, env) == 1, repr((r.returncode, last.get("result"), r.stderr[-150:])))


# Mutation check (--mutants): re-introduce a past bug into a copy of the tool or
# of the guard hook and confirm that the check written for it goes red, while the
# unmodified originals pass. Entries:
# (target "tool" | "guard", name, original snippet, mutated snippet,
#  part of the failing line: a check name for the tool, a command for the guard)
GUARD = os.path.join(HERE, "hooks", "guard.py")
GUARD_TEST = os.path.join(HERE, "hooks", "selftest_guard.sh")
MUTANTS = [
    ("tool", "path guard: allow ':' pathspec magic",
     'elif p.startswith(":"):', 'elif False:',
     "guard: ':!x'"),
    ("tool", "index restore: plain reset instead of the snapshot",
     "    if head_after(head0) != head0:\n        eprint(",
     "    git([\"reset\", \"-q\", \"--\"] + paths, env_extra=lit)\n    return\n    if head_after(head0) != head0:\n        eprint(",
     "failed commit puts the index back exactly"),
    ("tool", "index restore: no HEAD-moved guard",
     "    if head_after(head0) != head0:\n        eprint(", "    if False:\n        eprint(",
     "commit fails after a foreign commit took part"),
    ("tool", "swept detection looks only at the index",
     'if st.returncode == 0 and not st.stdout.strip():',
     'if git(["diff", "--cached", "--quiet", "--"] + args.paths, env_extra=lit).returncode == 0:',
     "dropped staging + unrelated foreign commit"),
    ("tool", "ls-files without --full-name",
     '"-z", "--full-name", "--"]', '"-z", "--"]',
     "failed commit from a subdirectory"),
    ("tool", "no agent gate on the escape hatches",
     '    return any(os.environ.get(m, "") not in ("", "0") for m in AGENT_MARKERS)',
     "    return False",
     "escape hatches: refused"),
    ("tool", "argparse abbreviations allowed",
     ", allow_abbrev=False", "",
     "guard: abbreviated flags"),
    ("tool", "no argument filter in run",
     '    longs, shorts = RUN_DENY.get(sub, ([], ""))',
     '    return None\n    longs, shorts = RUN_DENY.get(sub, ([], ""))',
     "state-rewriting argument forms refused"),
    ("tool", "file list taken before the commit",
     "        if dt.returncode == 0:", "        if False:",
     "reported file list = files in our commit"),
    ("tool", "add step not timed",
     'env_extra=lit, step="add")', "env_extra=lit)",
     "log: per-step timings"),
    ("tool", "fast: update-ref without the old value (no compare-and-swap)",
     '"HEAD", new, head or ""]', '"HEAD", new]',
     "fast: HEAD moved before our update-ref"),
    ("tool", "fast: empty directories kept in the graft",
     "                entries.pop(name, None)  # git keeps no empty directories",
     '                entries[name] = "040000 tree " + EMPTY_TREE',
     "fast: deleting a directory's only file"),
    ("tool", "fast: shared index not rewritten after the commit",
     "        os.replace(self.path, self.index)", "        os.remove(self.path)",
     "fast oracle: index under PATHS"),
    ("tool", "fast: private index built from scratch, not from the shared index",
     '            shutil.copy2(gp["index"], idx)', "            pass",
     "fast oracle: commit tree"),
    ("tool", "fast: index.lock not held (review 0.6 #1)",
     "                    self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)",
     "                    self.fd = os.open(os.devnull, os.O_RDWR)",
     "fast: a bare git commit during ours is locked out"),
    ("tool", "fast: hooks do not get the private index",
     '    if index:\n        env["GIT_INDEX_FILE"] = index', '    if False:\n        env["GIT_INDEX_FILE"] = index',
     "fast: hooks -- prepare-commit-msg"),
    ("tool", "fast: message not cleaned up like git commit",
     '    if mode == "verbatim":\n        return msg', '    if True:\n        return msg',
     "fast: message parity"),
    ("tool", "fast: commit.gpgSign does not force the classic path",
     '("commit.gpgsign", "commit.gpgSign (commit-tree would not sign)")', '("commit.nosuchkey", "x")',
     "fast: classic path for pre-commit hook"),
    ("tool", "fast: wrong-case path not detected on ignorecase",
     '    if cfg_bool(cfg.get("core.ignorecase"), False):', "    if False:",
     "fast: classic path for pre-commit hook"),
    ("tool", "fast: pre-commit hook does not force the classic path",
     '    if not args.no_verify and hook_file(gp, "pre-commit"):', '    if False:',
     "fast: classic path for pre-commit hook"),
    ("tool", "fast: a path known nowhere is skipped instead of refused",
     "            if gone:  # checked only when there is something to commit",
     "            if False:  # checked only when there is something to commit",
     "fast oracle: commit tree"),
    ("tool", "fast: index copied without its mtime (racy entries look clean)",
     "            shutil.copy2(gp[\"index\"], idx)", "            shutil.copyfile(gp[\"index\"], idx)",
     "fast: racily-clean file is re-read"),
    ("tool", "fast: no signal handlers while holding index.lock",
     "CATCH_SIGNALS = [getattr(signal, n) for n in (\"SIGTERM\", \"SIGHUP\", \"SIGQUIT\") if hasattr(signal, n)]",
     "CATCH_SIGNALS = []",
     "fast: SIGTERM while holding index.lock"),
    ("tool", "git output decoded strictly (non-UTF-8 names crash)",
     'text=True, errors="surrogateescape", input=input,', 'text=True, input=input,',
     "fast: a non-UTF-8 file name"),
    ("tool", "fast: write-tree failure is an error instead of a classic fallback",
     "            return FALLBACK", "            raise GitStepFailed(part, \"write_tree\")",
     "fast: unmerged entry outside PATH"),
    ("tool", "fast: invalid commit.cleanup accepted",
     '        raise RuntimeError("Invalid cleanup mode %s" % cleanup)', "        pass",
     "fast: invalid commit.cleanup"),
    ("tool", "HEAD read error taken for an unborn branch",
     "    if (r.stderr or \"\").strip():\n        raise HeadUnreadable", "    if False:\n        raise HeadUnreadable",
     "fast: HEAD unreadable at start"),
    ("tool", "HEAD that reads as missing taken for 'HEAD moved'",
     "    if h is None and head0 is not None:", "    if False:",
     "classic: HEAD unreadable after a failed commit"),
    ("guard", "guard: env -i not recognised",
     '                if ch == "i":', '                if False:',
     "env -i PATH=/usr/bin"),
    ("guard", "guard: fd number before a redirect taken as a path",
     "            if cur and cur[-1].isdigit():", "            if False:",
     " 2>&1 | tail -3"),
    ("guard", "guard: commit -m value parsed as flags",
     '"--cleanup", "--trailer", "--pathspec-from-file"), "mFcCt")',
     '"--cleanup", "--trailer", "--pathspec-from-file"), "")',
     'commit -m "-a" -- src/x.py'),
    ("guard", "guard: heredoc bodies not dropped",
     '    """Drop heredoc bodies (they are data, not commands)."""\n',
     '    """Drop heredoc bodies (they are data, not commands)."""\n    return text\n',
     "cat > msg.txt"),
    ("guard", "guard: shell keywords not stripped",
     "    while argv and argv[0] in KEYWORDS:\n        argv = argv[1:]\n",
     "",
     "if true; then git add ."),
    ("guard", "guard: commit -i allowed",
     '        if "i" in shorts or any(is_long(a, "--include", 3) for a in longs):',
     "        if False:",
     "commit -i -m x -- a"),
    ("guard", "guard: # inside a word starts a comment",
     '    lex.commenters = ""  # comments were removed above, quote-aware',
     '    lex.commenters = "#"',
     "echo a#b; git add ."),
    ("guard", "guard: generated-path check sees option values as paths",
     '"--cleanup", "--trailer", "--pathspec-from-file"), "mFcCt")\n        no_generated(sub, paths)',
     '"--cleanup", "--trailer", "--pathspec-from-file"), "mFcCt")\n        no_generated(sub, parse_opts(args)[2])',
     'commit -m "$(cat msg)" -- src/x.py'),
]


def _run_tool_suite(tmp, tool):
    env = dict(os.environ, SELFTEST_WOMBAT_BIN=tool)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--base", tmp,
                        "--writers", "2", "--commits", "2", "--files", "10"],
                       env=env, text=True, capture_output=True, timeout=900)
    return r.stdout.splitlines()


def _run_guard_suite(guard):
    r = subprocess.run(["bash", GUARD_TEST], env=dict(os.environ, SELFTEST_GUARD=guard),
                       text=True, capture_output=True, timeout=900)
    return r.stdout.splitlines()


def run_mutants(a):
    sources = {"tool": open(TOOL).read(), "guard": open(GUARD).read()}
    tmp = tempfile.mkdtemp(prefix="wombat-gate-mutants-", dir=a.base)
    missed = 0
    try:
        # control: the unmodified originals must pass at the same (small) scale,
        # otherwise a red check below could be noise rather than the mutation
        base = _run_tool_suite(tmp, TOOL) + _run_guard_suite(GUARD)
        base_red = [l for l in base if l.startswith("FAIL")]
        print("%s  control: unmodified tool and guard pass at mutant scale%s"
              % ("FAIL" if base_red else "PASS", (" -- " + base_red[0]) if base_red else ""), flush=True)
        if base_red:
            print("\ncontrol failed; mutant results would be meaningless")
            return 1
        for i, (target, name, old, new, must_fail) in enumerate(MUTANTS):
            src = sources[target]
            if old not in src:
                print("FAIL  mutant %-52s snippet not found (update MUTANTS)" % name)
                missed += 1
                continue
            path = os.path.join(tmp, "m%02d%s" % (i, ".py" if target == "guard" else ""))
            with open(path, "w") as f:
                f.write(src.replace(old, new))
            os.chmod(path, 0o755)
            lines = _run_tool_suite(tmp, path) if target == "tool" else _run_guard_suite(path)
            red = any(l.startswith("FAIL") and must_fail in l for l in lines)
            print("%s  mutant %-52s %s" % ("PASS" if red else "FAIL", name,
                                           "its check went red" if red else "check stayed green (!)"), flush=True)
            missed += 0 if red else 1
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d/%d mutants caught" % (len(MUTANTS) - missed, len(MUTANTS)))
    return 1 if missed else 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        repo, mode, wid, k, start_at = sys.argv[2:7]
        worker(repo, mode, int(wid), int(k), float(start_at))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=tempfile.gettempdir(), help="local directory for the throw-away repos")
    ap.add_argument("--writers", type=int, default=8)
    ap.add_argument("--commits", type=int, default=15)
    ap.add_argument("--files", type=int, default=3000, help="files in the initial commit (index size)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--mutants", action="store_true",
                    help="re-introduce past bugs into copies of the tool; each must turn its check red")
    a = ap.parse_args()
    if a.mutants:
        return run_mutants(a)

    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("wg_mod", TOOL)
    g = importlib.util.module_from_spec(importlib.util.spec_from_loader("wg_mod", loader))
    loader.exec_module(g)
    t = g.fs_type(a.base)
    if t in g.NONLOCAL_FS or (t or "").startswith("fuse"):
        print("refusing: --base %s is on a %s filesystem; the stress test must run on a local disk" % (a.base, t))
        return 2
    tmp = tempfile.mkdtemp(prefix="wombat-gate-selftest-", dir=a.base)
    state = os.path.join(tmp, "state")
    os.makedirs(state)
    env = base_env(state)
    print("wombat-gate selftest in %s (fs %s), git %s" % (tmp, t, sh(["git", "--version"], tmp, env).stdout.strip()))
    try:
        reproduced = scen_negctl(tmp, env, a.writers, a.commits, a.files)
        scen_mutation(tmp, env, a.writers, a.commits, a.files)
        scen_queue(tmp, env, a.writers, a.commits, a.files)
        scen_mixed(tmp, env, a.writers, a.commits, a.files)
        scen_single(tmp, env, "classic")
        scen_single(tmp, env, "fast")
        scen_fast(tmp, env)
        scen_fast_review2(tmp, env)
        scen_head_unreadable(tmp, env)
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print("\n%d/%d checks passed%s" % (len(RESULTS) - n_fail, len(RESULTS),
                                       "" if reproduced else " (negative control did NOT reproduce the race)"))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
