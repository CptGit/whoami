import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_TOOLS = '''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ['RELEASE_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
if name == 'sleep':
    sys.exit(0)
if name == 'sudo':
    if '-l' in sys.argv:
        sys.exit(1 if os.environ.get('RELEASE_TEST_DENY_SUDO') else 0)
    current = (root / 'current').resolve()
    with (root / 'restarts').open('a') as out:
        out.write(current.name + '\\n')
    if (current / 'fail_restart').exists():
        sys.exit(1)
    (root / 'running').write_text(current.name)
elif name == 'curl':
    current = (root / 'current').resolve()
    if (current / 'fail_health').exists():
        sys.exit(1)
    release = (root / 'running').read_text()
    if (current / 'wrong_health').exists():
        release = 'wrong-release'
    print(json.dumps({'status': 'ok', 'release': release}))
'''


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'deployment'
        self.upstream = self.base / 'upstream'
        self.tools = self.base / 'bin'
        self.tools.mkdir()
        for name in ('sudo', 'curl', 'sleep'):
            path = self.tools / name
            path.write_text(FAKE_TOOLS)
            path.chmod(0o755)
        self.env = {
            **os.environ, 'PATH': str(self.tools) + os.pathsep + os.environ['PATH'],
            'RELEASE_TEST_ROOT': str(self.root), 'RELEASE_TEST_PYTHON': sys.executable,
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
        }
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-q', '-b', 'main')
        self.git(self.upstream, 'config', 'user.email', 'test@example.invalid')
        self.git(self.upstream, 'config', 'user.name', 'Test')
        (self.upstream / 'scripts').mkdir()
        (self.upstream / 'scripts/setup.sh').write_text(
            '#!/bin/bash\nset -eu\n'
            'if [[ -f fail_setup ]]; then exit 1; fi\n'
            'mkdir -p .venv/bin\nln -s "$RELEASE_TEST_PYTHON" .venv/bin/python3\n'
        )
        (self.upstream / '.gitignore').write_text('.venv/\n.release.env\n')
        (self.upstream / 'main.py').write_text('app = object()\n')
        (self.upstream / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
        (self.upstream / 'uv.lock').write_text('version = 1\n')
        self.first = self.commit()

    def git(self, cwd, *args):
        result = subprocess.run(['git', '-C', str(cwd), *args], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, marker=None):
        if marker:
            (self.upstream / marker).touch()
        (self.upstream / 'version.txt').write_text(os.urandom(8).hex())
        self.git(self.upstream, 'add', '.')
        self.git(self.upstream, 'commit', '-q', '-m', 'fixture release')
        return self.git(self.upstream, 'rev-parse', 'HEAD')

    def deploy(self, sha=None, mode='deploy', env=None, root=None):
        return subprocess.run(
            ['bash', str(ROOT / 'scripts/release.sh'), str(root or self.root), str(self.upstream),
             sha or self.first, 'whoami.service', mode],
            env=env or self.env, capture_output=True, text=True, timeout=15,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def current(self):
        return (self.root / 'current').resolve().name

    def test_successful_releases_keep_four_versions_and_preserve_data(self):
        self.assert_success(self.deploy())
        release = self.root / 'current'
        self.assertEqual(self.git(release, 'rev-parse', 'HEAD'), self.first)
        self.assertEqual(self.git(release, 'rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')
        self.assertTrue((release / '.venv/bin/python3').exists())
        self.assertEqual((release / '.release.env').read_text(), f'WHOAMI_RELEASE={self.first}\n')

        data = self.root / 'shared/custom/private.json'
        data.write_text('["persistent"]')
        unmanaged = self.root / 'releases' / ('f' * 40)
        unmanaged.mkdir()
        (unmanaged / 'operator.txt').write_text('keep')
        commits = [self.first]
        for _ in range(5):
            commits.append(self.commit())
            self.assert_success(self.deploy(commits[-1]))
        managed = {p.name for p in (self.root / 'releases').iterdir() if p != unmanaged}
        self.assertEqual(managed, set(commits[-4:]))
        self.assertEqual(self.current(), commits[-1])
        self.assertEqual((self.root / 'previous').resolve().name, commits[-2])
        registered = self.git(self.root / 'repo.git', 'worktree', 'list', '--porcelain')
        self.assertNotIn(str(self.root / 'releases' / self.first), registered)
        self.assertEqual(data.read_text(), '["persistent"]')
        self.assertTrue((unmanaged / 'operator.txt').exists())

    def test_wrong_health_identity_restores_previous_release(self):
        self.assert_success(self.deploy())
        bad = self.commit('wrong_health')
        result = self.deploy(bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.first)
        self.assertEqual((self.root / 'running').read_text(), self.first)
        self.assertFalse((self.root / 'releases' / bad).exists())
        self.assertIn('原版本已恢复', result.stderr)

    def test_existing_checkout_symlink_and_inaccessible_roots_are_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.upstream, target_is_directory=True)
        for path in (self.upstream, alias):
            with self.subTest(path=path.name):
                self.assertNotEqual(self.deploy(root=path).returncode, 0)
                self.assertFalse((self.upstream / '.whoami-releases').exists())
                self.assertEqual(self.git(self.upstream, 'status', '--porcelain'), '')
        if os.geteuid() != 0:
            restricted = self.base / 'restricted'
            restricted.mkdir()
            self.addCleanup(restricted.chmod, 0o700)
            for permissions in (0o500, 0o000):
                with self.subTest(permissions=oct(permissions)):
                    restricted.chmod(permissions)
                    result = self.deploy(root=restricted / 'release', mode='prepare')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('部署账户无法访问或写入发布目录', result.stderr)
                    self.assertNotIn('mkdir:', result.stderr)
            restricted.chmod(0o700)
            self.assertEqual(list(restricted.iterdir()), [])

    def test_dependency_failure_leaves_current_untouched_and_removes_failed_worktree(self):
        self.assert_success(self.deploy())
        bad = self.commit('fail_setup')
        result = self.deploy(bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.first)
        self.assertFalse((self.root / 'releases' / bad).exists())
        self.assertEqual((self.root / 'restarts').read_text().splitlines(), [self.first])

    def test_restart_failure_also_restores_old_release(self):
        self.assert_success(self.deploy())
        bad = self.commit('fail_restart')
        self.assertNotEqual(self.deploy(bad).returncode, 0)
        self.assertEqual(self.current(), self.first)
        self.assertEqual((self.root / 'running').read_text(), self.first)

    def test_manual_rollback_switches_previous_without_fetch_or_setup(self):
        self.assert_success(self.deploy())
        second = self.commit()
        self.assert_success(self.deploy(second))
        # Removing origin makes a fetch fail; rollback must work offline.
        self.git(self.root / 'repo.git', 'remote', 'remove', 'origin')
        self.assert_success(self.deploy(mode='rollback'))
        self.assertEqual(self.current(), self.first)
        self.assertEqual((self.root / 'previous').resolve().name, second)

    def test_failed_rollback_keeps_running_release_and_previous_pointer(self):
        self.assert_success(self.deploy())
        second = self.commit()
        self.assert_success(self.deploy(second))
        (self.root / 'releases' / self.first / 'fail_health').touch()
        self.assertNotEqual(self.deploy(mode='rollback').returncode, 0)
        self.assertEqual(self.current(), second)
        self.assertEqual((self.root / 'previous').resolve().name, self.first)

    def test_prepare_only_does_not_switch_or_require_sudo(self):
        result = self.deploy(mode='prepare', env={**self.env, 'RELEASE_TEST_DENY_SUDO': '1'})
        self.assert_success(result)
        self.assertFalse((self.root / 'current').exists())
        self.assertFalse((self.root / 'restarts').exists())
        self.assertTrue((self.root / 'state' / f'{self.first}.ready').exists())
        self.assert_success(self.deploy())

    def test_missing_sudo_stops_before_creating_root(self):
        result = self.deploy(env={**self.env, 'RELEASE_TEST_DENY_SUDO': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.root.exists())

    def test_release_lock_rejects_concurrent_deployment(self):
        self.assert_success(self.deploy(mode='prepare'))
        with (self.root / '.deploy.lock').open('a') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'current').exists())


class SharedDataTests(unittest.TestCase):
    def test_custom_libraries_are_written_outside_release_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            custom = Path(temp) / 'shared/custom'
            code = '''
import asyncio
import main
asyncio.run(main.save_library(main.LibrarySchema(name="shared-fixture", characters=["角色甲", "角色乙"])))
result = asyncio.run(main.get_library("shared-fixture"))
assert result["characters"] == ["角色甲", "角色乙"]
assert result["protected"] is False
'''
            result = subprocess.run(
                [sys.executable, '-c', code],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'WHOAMI_CUSTOM_DIR': str(custom)},
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((custom / 'shared-fixture.json').is_file())
