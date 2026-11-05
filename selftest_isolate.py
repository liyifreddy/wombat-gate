#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""0.9 tests: path claims, isolated worktrees, integrate without holding the queue (W1).

Synthetic repositories on a local disk, no network. Fixtures are retained and their
path is printed. No production repository is modified.
"""
import fcntl
import json
import os
from pathlib import Path
import shlex
import select
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
TOOL = Path(os.environ.get('SELFTEST_WOMBAT_BIN') or HERE / 'bin' / 'wombat-gate')
ROOT = Path(tempfile.mkdtemp(prefix='wombat-09-'))
print('Fixtures:', ROOT, flush=True)
PY = sys.executable


class Base(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / self.id().split('.')[-1]
        self.repo = self.root / 'repo'
        self.repo.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('GIT_', 'WOMBAT_')) and k not in (
                        'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_CODE_ENTRYPOINT')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        WOMBAT_STATE_DIR=str(self.root / 'state'), PYTHONDONTWRITEBYTECODE='1',
                        WOMBAT_NOTE_EVERY='3600')
        self.git('init', '-q', '-b', 'main')
        self.write('old.txt', 'old\n')
        self.write('other.txt', 'other\n')
        self.write('src/a/f.txt', 'a\n')
        self.write('src/b/f.txt', 'b\n')
        self.git('add', '--', 'old.txt', 'other.txt', 'src')
        self.git('commit', '-qm', 'base')

    def write(self, name, text, where=None):
        p = Path(where or self.repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')

    def run_cmd(self, argv, cwd=None, env=None, timeout=60):
        return subprocess.run(argv, cwd=str(cwd or self.repo), env=env or self.env,
                              text=True, capture_output=True, timeout=timeout)

    def git(self, *args, cwd=None):
        r = self.run_cmd(['git', *args], cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def gate(self, *args, cwd=None, env=None):
        return self.run_cmd([PY, str(TOOL), *args], cwd, env)

    def commit_main(self, name, text, session='other'):
        self.write(name, text)
        r = self.gate('commit', '--session', session, '-m', 'main ' + name, '--', name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.git('rev-parse', 'HEAD')

    def repo_status(self):
        return self.git('status', '--porcelain', '--ignored', '-uall')


class ClaimTests(Base):
    def test_claim_overlap_reported_nonoverlap_allowed(self):
        a = self.gate('claim', '--as', 'S-A', '--', 'src/a', 'notes/a.md')
        self.assertEqual(a.returncode, 0, a.stderr)
        parent = self.gate('claim', '--as', 'S-B', '--', 'src')
        self.assertEqual(parent.returncode, 7, parent.stderr)
        self.assertIn('S-A', parent.stderr)
        self.assertIn('src/a', parent.stderr)
        child = self.gate('claim', '--as', 'S-B', '--', 'src/a/f.txt')
        self.assertEqual(child.returncode, 7, child.stderr)
        same = self.gate('claim', '--as', 'S-B', '--', 'notes/a.md')
        self.assertEqual(same.returncode, 7, same.stderr)
        ok = self.gate('claim', '--as', 'S-B', '--', 'src/b', 'src/ab')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        listing = self.gate('claims', '--json')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        rows = json.loads(listing.stdout)['claims']
        self.assertEqual(sorted((r['session'], r['path']) for r in rows),
                         [('S-A', 'notes/a.md'), ('S-A', 'src/a'), ('S-B', 'src/ab'), ('S-B', 'src/b')])

    def test_claim_refused_whole_claim_on_any_overlap(self):
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src/a').returncode, 0)
        r = self.gate('claim', '--as', 'S-B', '--', 'src/b', 'src/a')
        self.assertEqual(r.returncode, 7, r.stderr)
        rows = json.loads(self.gate('claims', '--json').stdout)['claims']
        self.assertNotIn('S-B', {x['session'] for x in rows})

    def test_claim_same_session_widens(self):
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src/a').returncode, 0)
        r = self.gate('claim', '--as', 'S-A', '--', 'src')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_release_then_other_can_claim(self):
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src').returncode, 0)
        self.assertEqual(self.gate('release', '--as', 'S-A').returncode, 0)
        r = self.gate('claim', '--as', 'S-B', '--', 'src/a')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_release_named_paths_only(self):
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src/a', 'src/b').returncode, 0)
        self.assertEqual(self.gate('release', '--as', 'S-A', '--', 'src/a').returncode, 0)
        self.assertEqual(self.gate('claim', '--as', 'S-B', '--', 'src/a').returncode, 0)
        self.assertEqual(self.gate('claim', '--as', 'S-B', '--', 'src/b').returncode, 7)

    def test_dead_owner_claim_cleared(self):
        p = subprocess.Popen(['sleep', '60'])
        try:
            env = dict(self.env, WOMBAT_OWNER_PID=str(p.pid))
            self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src', env=env).returncode, 0)
            self.assertEqual(self.gate('claim', '--as', 'S-B', '--', 'src/a').returncode, 7)
        finally:
            p.kill()
            p.wait()
        r = self.gate('claim', '--as', 'S-B', '--', 'src/a')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('S-A', r.stderr)   # says whose stale claim was cleared

    def test_claim_needs_name_and_literal_paths(self):
        self.assertEqual(self.gate('claim', '--', 'src').returncode, 2)
        for bad in ('.', 'src/*', ':/src', '../x'):
            self.assertEqual(self.gate('claim', '--as', 'S-A', '--', bad).returncode, 2, bad)
        self.assertEqual(self.gate('claim', '--as', 'S-A').returncode, 2)

    def test_claims_live_outside_repo(self):
        before = self.repo_status()
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src').returncode, 0)
        self.assertEqual(self.repo_status(), before)
        self.assertTrue(list((self.root / 'state' / 'locks').glob('*.claims.json')))

    def test_commit_warns_outside_own_claims(self):
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src/a').returncode, 0)
        self.assertEqual(self.gate('claim', '--as', 'S-B', '--', 'src/b').returncode, 0)
        self.write('other.txt', 'changed\n')
        r = self.gate('commit', '--session', 'S-A', '-m', 'x', '--', 'other.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('not in your claims', r.stderr)
        self.write('src/b/f.txt', 'changed\n')
        r = self.gate('commit', '--session', 'S-A', '-m', 'y', '--', 'src/b/f.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('S-B', r.stderr)
        self.write('src/a/f.txt', 'changed\n')
        r = self.gate('commit', '--session', 'S-A', '-m', 'z', '--', 'src/a/f.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('claim', r.stderr)

    def test_no_claims_no_noise(self):
        self.write('other.txt', 'changed\n')
        r = self.gate('commit', '--session', 'S-A', '-m', 'x', '--', 'other.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('claim', r.stderr)

    def test_claims_shared_across_worktrees(self):
        wt = self.root / 'wt2'
        self.git('worktree', 'add', '-q', '-b', 'side', str(wt))
        self.assertEqual(self.gate('claim', '--as', 'S-A', '--', 'src/a').returncode, 0)
        r = self.gate('claim', '--as', 'S-B', '--', 'src', cwd=wt)
        self.assertEqual(r.returncode, 7, r.stderr)


class IsolateTests(Base):
    def isolate(self, sess, *more):
        r = self.gate('isolate', '--as', sess, *more)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.gate('claims', '--json').stdout)['isolates']
        return Path(next(x['path'] for x in rows if x['session'] == sess))

    def test_isolate_commit_integrate_done(self):
        wt = self.isolate('S-A')
        self.assertTrue((wt / 'old.txt').exists())
        self.assertNotEqual(wt.resolve(), self.repo.resolve())
        self.assertTrue(str(wt.resolve()).startswith(str(self.root.resolve())))   # beside the repo
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=wt)
        self.assertIn('S-A', branch)
        self.write('src/a/new.txt', 'from A\n', wt)
        r = self.gate('commit', '--session', 'S-A', '-m', 'A work', '--', 'src/a/new.txt', cwd=wt)
        self.assertEqual(r.returncode, 0, r.stderr)
        main = self.commit_main('other.txt', 'main moved\n')
        before = self.repo_status()
        r = self.gate('integrate', '--as', 'S-A', '--check', 'test -f src/a/new.txt', '--done')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git('show', 'HEAD:src/a/new.txt'), 'from A')
        self.assertEqual(self.git('show', 'HEAD:other.txt'), 'main moved')
        self.git('merge-base', '--is-ancestor', main, 'HEAD')
        self.assertFalse(wt.exists())
        self.assertEqual(self.git('branch', '--list', branch), '')
        self.assertEqual(json.loads(self.gate('claims', '--json').stdout)['isolates'], [])
        self.assertEqual(self.repo_status(), before)

    def test_isolate_done_after_separate_integrate(self):
        wt = self.isolate('S-A')
        self.write('x.txt', 'x\n', wt)
        self.assertEqual(self.gate('commit', '--session', 'S-A', '-m', 'x', '--', 'x.txt', cwd=wt).returncode, 0)
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=wt)
        r = self.gate('integrate', '--rebase', branch, '--check', 'true')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.gate('isolate', '--done', '--as', 'S-A')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wt.exists())

    def test_isolate_done_refuses_unmerged(self):
        wt = self.isolate('S-A')
        self.write('x.txt', 'x\n', wt)
        self.assertEqual(self.gate('commit', '--session', 'S-A', '-m', 'x', '--', 'x.txt', cwd=wt).returncode, 0)
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=wt)
        r = self.gate('isolate', '--done', '--as', 'S-A')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue((wt / 'x.txt').exists())
        self.assertNotEqual(self.git('branch', '--list', branch), '')

    def test_isolate_done_preserves_source_commit_while_queued(self):
        wt = self.isolate('S-A')   # initially merged into main
        branch = self.git('symbolic-ref', '--short', 'HEAD', cwd=wt)
        self.assertEqual(self.gate('who').returncode, 0)
        locks = [p for p in (self.root / 'state' / 'locks').glob('*.lock')
                 if not p.name.endswith('.registry.lock')]
        self.assertEqual(len(locks), 1)
        env = dict(self.env, WOMBAT_NOTE_EVERY='0.05')
        proc = None
        try:
            # Model a writer holding the same queue lock used by gate commits.
            with locks[0].open('r+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                proc = subprocess.Popen(
                    [PY, str(TOOL), 'isolate', '--done', '--as', 'S-A', '--timeout', '10'],
                    cwd=str(self.repo), env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                ready, _, _ = select.select([proc.stderr], [], [], 5)
                self.assertTrue(ready, 'cleanup did not reach the queue')
                note = proc.stderr.readline()
                self.assertIn('waiting', note)
                # Finish the source commit before allowing cleanup to acquire the queue.
                self.write('new-unmerged.txt', 'source work during queue wait\n', wt)
                self.git('add', '--', 'new-unmerged.txt', cwd=wt)
                self.git('commit', '-qm', 'source advanced while cleanup queued', cwd=wt)
                newer = self.git('rev-parse', 'HEAD', cwd=wt)
                fcntl.flock(lock, fcntl.LOCK_UN)
            out, err = proc.communicate(timeout=15)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertTrue(wt.exists(), 'unmerged worktree removed while waiting for queue')
        self.assertEqual(self.git('rev-parse', branch), newer)
        self.assertEqual((wt / 'new-unmerged.txt').read_text(), 'source work during queue wait\n')
        self.assertTrue('changed' in err or 'not in' in err, err)
        rows = json.loads(self.gate('claims', '--json').stdout)['isolates']
        self.assertEqual([r['session'] for r in rows], ['S-A'])

    def test_isolate_done_refuses_dirty(self):
        wt = self.isolate('S-A')
        self.write('scratch.txt', 'uncommitted\n', wt)
        r = self.gate('isolate', '--done', '--as', 'S-A')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue((wt / 'scratch.txt').exists())

    def test_isolate_conflict_stops_at_source(self):
        wt = self.isolate('S-A')
        self.write('old.txt', 'from A\n', wt)
        self.assertEqual(self.gate('commit', '--session', 'S-A', '-m', 'A', '--', 'old.txt', cwd=wt).returncode, 0)
        main = self.commit_main('old.txt', 'from main\n')
        r = self.gate('integrate', '--as', 'S-A', '--check', 'true', '--done')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('old.txt', r.stderr)
        self.assertIn(str(wt), r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), main)
        self.assertTrue(wt.exists())
        self.assertEqual(self.git('show', 'HEAD:old.txt'), 'from main')

    def test_two_isolates_and_rerun(self):
        a = self.isolate('S-A')
        b = self.isolate('S-B')
        self.assertNotEqual(a, b)
        again = self.isolate('S-A')
        self.assertEqual(a, again)

    def test_isolate_claims_paths_first(self):
        self.isolate('S-A', '--', 'src')
        r = self.gate('isolate', '--as', 'S-B', '--', 'src/a')
        self.assertEqual(r.returncode, 7, r.stderr)
        rows = json.loads(self.gate('claims', '--json').stdout)['isolates']
        self.assertEqual([x['session'] for x in rows], ['S-A'])

    def test_isolate_needs_name(self):
        self.assertEqual(self.gate('isolate').returncode, 2)


class W1Tests(Base):
    def side_branch(self):
        wt = self.root / 'side'
        self.git('worktree', 'add', '-q', '-b', 'topic', str(wt))
        self.write('topic.txt', 'topic\n', wt)
        self.git('add', '--', 'topic.txt', cwd=wt)
        self.git('commit', '-qm', 'topic', cwd=wt)
        return wt

    def slow_check(self, seconds):
        marker = self.root / 'check-started'
        code = 'import pathlib,time; pathlib.Path(%r).write_text("x"); time.sleep(%s)' % (str(marker), seconds)
        return marker, shlex.join([PY, '-c', code])

    def start_integrate(self, *extra, seconds=2):
        marker, check = self.slow_check(seconds)
        proc = subprocess.Popen([PY, str(TOOL), 'integrate', '--rebase', 'topic', '--check', check, *extra],
                                cwd=str(self.repo), env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), 'check did not start')
        return proc

    def test_commit_lands_during_long_check(self):
        self.side_branch()
        proc = self.start_integrate()
        try:
            who = self.gate('who')
            self.assertIn(': free;', who.stdout)
            self.write('other.txt', 'landed during check\n')
            r = self.gate('commit', '--session', 'other', '--timeout', '1', '-m', 'during', '--', 'other.txt')
            self.assertEqual(r.returncode, 0, r.stderr)
            during = self.git('rev-parse', 'HEAD')
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn('rebasing again', out + err)
        self.git('merge-base', '--is-ancestor', during, 'HEAD')
        self.assertEqual(self.git('show', 'HEAD:topic.txt'), 'topic')
        self.assertEqual(self.git('show', 'HEAD:other.txt'), 'landed during check')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'topic'))

    def test_attempt_limit(self):
        self.side_branch()
        proc = self.start_integrate('--attempts', '1', seconds=1)
        try:
            self.write('other.txt', 'landed during check\n')
            r = self.gate('commit', '--session', 'other', '--timeout', '1', '-m', 'during', '--', 'other.txt')
            self.assertEqual(r.returncode, 0, r.stderr)
            during = self.git('rev-parse', 'HEAD')
            out, err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn('target moved', err)
        self.assertEqual(self.git('rev-parse', 'HEAD'), during)

    def test_attempts_env_and_usage(self):
        self.side_branch()
        self.assertEqual(self.gate('integrate', '--rebase', 'topic', '--attempts', '0', '--check', 'true').returncode, 2)
        env = dict(self.env, WOMBAT_INTEGRATE_ATTEMPTS='junk')
        self.assertEqual(self.gate('integrate', '--rebase', 'topic', '-n', '--check', 'true', env=env).returncode, 0)

    def test_no_retry_without_movement(self):
        self.side_branch()
        r = self.gate('integrate', '--rebase', 'topic', '--check', 'true')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('rebasing again', r.stdout + r.stderr)
        log = [json.loads(l) for l in (self.root / 'state' / 'wombat-gate.log').read_text().splitlines()]
        rec = [x for x in log if x.get('op') == 'integrate'][-1]
        self.assertEqual(rec.get('attempts'), 1)
        self.assertEqual(rec.get('result'), 'integrated')


if __name__ == '__main__':
    unittest.main(verbosity=2)
