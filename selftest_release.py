#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""0.8 regression tests; synthetic repositories on a local disk, no network.
Fixtures are retained and their path is printed. No production repository is modified.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
TOOL = HERE / 'bin' / 'wombat-gate'
ROOT = Path(tempfile.mkdtemp(prefix='wombat-08-'))
print('Fixtures:', ROOT, flush=True)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / self.id().split('.')[-1]
        self.repo = self.root / 'repo'
        self.repo.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'WOMBAT_'))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        WOMBAT_STATE_DIR=str(self.root / 'state'), PYTHONDONTWRITEBYTECODE='1')
        self.git('init', '-q', '-b', 'main')
        self.write('old.txt', 'old\n')
        self.write('other.txt', 'other\n')
        self.git('add', '--', 'old.txt', 'other.txt')
        self.git('commit', '-qm', 'base')

    def write(self, name, text, where=None):
        p = (where or self.repo) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')

    def run_cmd(self, argv, cwd=None):
        return subprocess.run(argv, cwd=cwd or self.repo, env=self.env,
                              text=True, capture_output=True, timeout=30)

    def git(self, *args, cwd=None):
        r = self.run_cmd(['git', *args], cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def gate(self, *args, cwd=None):
        return self.run_cmd([sys.executable, str(TOOL), *args], cwd)

    def index(self):
        return hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest()

    def staged_other(self):
        self.write('other.txt', 'foreign staged\n')
        self.git('add', '--', 'other.txt')
        return self.git('ls-files', '-s', '--', 'other.txt')

    def check_delete(self, mode, chinese=False, rename=False):
        self.env['WOMBAT_COMMIT_MODE'] = mode
        old, new = ('中文 旧.txt', '中文 新.txt') if chinese else ('old.txt', 'new.txt')
        if chinese:
            self.write(old, 'old\n')
            self.git('add', '--', old)
            self.git('commit', '-qm', 'unicode base')
        other = self.staged_other()
        if rename:
            self.git('mv', '--', old, new)
        else:
            self.git('rm', '--', old)
        paths = [old, new] if rename else [old]
        before, head = self.index(), self.git('rev-parse', 'HEAD')
        preview = self.gate('commit', '-n', '-m', 'preview', '--expect-files', str(len(paths)), '--', *paths)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn('改名 1' if rename else '删 1', preview.stdout)
        rows = [s for s in preview.stdout.splitlines() if '\t' in s]
        self.assertEqual(len(rows), len(paths), preview.stdout)
        self.assertEqual(self.index(), before)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        refused = self.gate('commit', '-m', 'wrong count', '--expect-files', '99', '--', *paths)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(self.index(), before)
        r = self.gate('commit', '-m', 'move', '--expect-files', str(len(paths)), '--', *paths)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.git('diff-tree', '--no-commit-id', '--no-renames', '--name-only', '-r', '-z', 'HEAD').strip('\0').split('\0')
        self.assertEqual(set(got), set(paths))
        self.assertEqual(self.git('ls-files', '-s', '--', 'other.txt'), other)
        self.assertEqual(self.git('show', 'HEAD:other.txt'), 'other')

    def test_fast_staged_delete(self): self.check_delete('fast')
    def test_classic_staged_delete(self): self.check_delete('classic')
    def test_fast_rename(self): self.check_delete('fast', rename=True)
    def test_classic_rename(self): self.check_delete('classic', rename=True)
    def test_fast_unicode_rename(self): self.check_delete('fast', chinese=True, rename=True)
    def test_classic_unicode_rename(self): self.check_delete('classic', chinese=True, rename=True)
    def test_fast_unicode_delete(self): self.check_delete('fast', chinese=True)
    def test_classic_unicode_delete(self): self.check_delete('classic', chinese=True)

    def test_unknown_path_refused(self):
        r = self.gate('commit', '-m', 'bad', '--', 'missing.txt')
        self.assertNotEqual(r.returncode, 0)

    def test_subdirectory_staged_delete(self):
        self.write('sub/旧.txt', 'data\n')
        self.git('add', '--', 'sub/旧.txt')
        self.git('commit', '-qm', 'sub')
        self.git('rm', '--', 'sub/旧.txt')
        r = self.gate('commit', '-m', 'delete', '--', '旧.txt', cwd=self.repo / 'sub') if (self.repo / 'sub').exists() else None
        # git rm removes an empty parent; recreate it to exercise a subdirectory cwd.
        if r is None:
            (self.repo / 'sub').mkdir()
            r = self.gate('commit', '-m', 'delete', '--', '旧.txt', cwd=self.repo / 'sub')
        self.assertEqual(r.returncode, 0, r.stderr)

    def integration(self, conflict=False):
        self.source = self.root / 'source'
        self.git('worktree', 'add', '-qb', 'topic', str(self.source))
        self.write('old.txt' if conflict else 'topic.txt', 'source\n', self.source)
        self.git('add', '--', 'old.txt' if conflict else 'topic.txt', cwd=self.source)
        self.git('commit', '-qm', 'topic', cwd=self.source)
        self.write('old.txt', 'main advanced\n')
        self.git('add', '--', 'old.txt')
        self.git('commit', '-qm', 'main')
        self.main = self.git('rev-parse', 'HEAD')

    def integrate(self, *args):
        return self.gate('integrate', '--rebase', 'topic', *args)

    def test_integrate_rebase_checks_ff_preserves_foreign(self):
        self.integration()
        other = self.staged_other()
        r = self.integrate('--check', '%s -c "import pathlib; assert pathlib.Path(\'topic.txt\').exists()"' % sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'topic'))
        self.assertEqual(self.git('show', 'HEAD:old.txt'), 'main advanced')
        self.assertEqual(self.git('ls-files', '-s', '--', 'other.txt'), other)
        self.assertEqual(self.git('show', 'HEAD:other.txt'), 'other')
        self.assertTrue(self.source.exists())

    def test_integrate_check_failure(self):
        self.integration()
        r = self.integrate('--check', '%s -c "raise SystemExit(7)"' % sys.executable)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)

    def test_integrate_conflict(self):
        self.integration(conflict=True)
        r = self.integrate('--check', '%s -c "pass"' % sys.executable)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('rebase stopped', r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)
        self.assertTrue((self.source / 'old.txt').exists())

    def test_integrate_dirty_source(self):
        self.integration()
        self.write('topic.txt', 'uncommitted\n', self.source)
        r = self.integrate('--check', 'true')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)

    def test_integrate_overlap(self):
        self.integration()
        self.write('topic.txt', 'foreign untracked\n')
        r = self.integrate('--check', 'true')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('overlapping', r.stderr)
        self.assertEqual((self.repo / 'topic.txt').read_text(), 'foreign untracked\n')

    def test_integrate_ignored_collision(self):
        self.integration()
        self.write('.gitignore', 'topic.txt\n')
        self.write('topic.txt', 'ignored foreign\n')
        r = self.integrate('--check', 'true')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((self.repo / 'topic.txt').read_text(), 'ignored foreign\n')

    def test_classic_failed_hook_restores_staged_delete(self):
        self.env['WOMBAT_COMMIT_MODE'] = 'classic'
        self.git('rm', '--', 'old.txt')
        other = self.staged_other()
        hook = self.repo / '.git/hooks/commit-msg'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        before = self.git('ls-files', '-s')
        r = self.gate('commit', '-m', 'reject', '--', 'old.txt')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.git('ls-files', '-s'), before)
        self.assertEqual(self.git('ls-files', '-s', '--', 'other.txt'), other)

    def test_integrate_queue_timeout(self):
        import fcntl
        self.integration()
        self.assertEqual(self.integrate('-n', '--check', 'true').returncode, 0)
        lock = next((self.root / 'state' / 'locks').glob('*.lock'))
        source_head = self.git('rev-parse', 'topic')
        with lock.open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.integrate('--timeout', '0.1', '--check', 'true')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(self.git('rev-parse', 'topic'), source_head)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)

    def test_integrate_index_override_refused(self):
        self.integration()
        self.env['GIT_INDEX_FILE'] = str(self.repo / '.git/index')
        self.assertEqual(self.integrate('--check', 'true').returncode, 2)

    def test_integrate_source_ignored_collision(self):
        self.integration()
        self.write('.gitignore', 'main-only.txt\n', self.source)
        self.git('add', '--', '.gitignore', cwd=self.source)
        self.git('commit', '-qm', 'ignore cache', cwd=self.source)
        self.write('main-only.txt', 'precious ignored\n', self.source)
        self.write('main-only.txt', 'target tracked\n')
        self.git('add', '--', 'main-only.txt')
        self.git('commit', '-qm', 'main new tracked path')
        main = self.git('rev-parse', 'HEAD')
        r = self.integrate('--check', 'true')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('source has ignored files', r.stderr)
        self.assertEqual((self.source / 'main-only.txt').read_text(), 'precious ignored\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), main)

    def test_integrate_signal_cleans_check(self):
        import signal
        import time
        self.integration()
        marker = self.root / 'check.pid'
        code = "import os,time,pathlib;pathlib.Path(%r).write_text(str(os.getpid()));time.sleep(60)" % str(marker)
        import shlex
        command = shlex.join([sys.executable, '-c', code])
        proc = subprocess.Popen([sys.executable, str(TOOL), 'integrate', '--rebase', 'topic', '--check', command],
                                cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), 'check did not start')
            child_pid = int(marker.read_text())
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 143, err)
            self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_integrate_check_required(self):
        self.integration()
        self.assertEqual(self.integrate().returncode, 2)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)

    def test_integrate_dry_run(self):
        self.integration()
        source_head = self.git('rev-parse', 'topic')
        r = self.integrate('-n', '--check', 'true')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)
        self.assertEqual(self.git('rev-parse', 'topic'), source_head)

    def test_integrate_check_dirties_source(self):
        self.integration()
        r = self.integrate('--check', '%s -c "open(\'new.txt\',\'w\').write(\'x\')"' % sys.executable)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)

    def test_integrate_timeout(self):
        self.integration()
        r = self.integrate('--check-timeout', '0.1', '--check', '%s -c "import time; time.sleep(2)"' % sys.executable)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.main)


if __name__ == '__main__':
    unittest.main(verbosity=2)
