import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
VERSION = '24.22.3'


class NodeSetupTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('node_setup', ROOT / 'scripts/setup-node.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / '.node-version').write_text(VERSION)
        for name, value in (('system', 'Linux'), ('machine', 'x86_64')):
            mocked = patch.object(self.module.platform, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(self.module.shutil, 'which', return_value=None)
        mocked.start()
        self.addCleanup(mocked.stop)

    def node(self, destination, version=VERSION):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f'#!/bin/sh\necho v{version}\n')
        destination.chmod(0o755)
        return destination

    def archive(self, unsafe=False):
        name = f'node-v{VERSION}-linux-x64'
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as package:
            if unsafe:
                member = tarfile.TarInfo('../../escape')
                member.size = 4
                package.addfile(member, io.BytesIO(b'fail'))
            payload = f'#!/bin/sh\necho v{VERSION}\n'.encode()
            member = tarfile.TarInfo(name + '/bin/node')
            member.size = len(payload)
            member.mode = 0o755
            package.addfile(member, io.BytesIO(payload))
        return name + '.tar.gz', data.getvalue()

    def download(self, archive, bad_hash=False):
        name, contents = archive
        digest = '0' * 64 if bad_hash else hashlib.sha256(contents).hexdigest()
        def open_url(url, timeout):
            if url.endswith('SHASUMS256.txt'):
                return io.BytesIO(f'{digest}  {name}\n'.encode())
            self.assertTrue(url.endswith('/' + name))
            return io.BytesIO(contents)
        return patch.object(self.module, 'urlopen', side_effect=open_url)

    def test_installs_verified_archive_and_reuses_it_on_next_setup(self):
        with self.download(self.archive()) as download:
            self.module.setup(self.project)
            self.assertEqual(download.call_count, 2)
        with patch.object(self.module, 'urlopen', side_effect=AssertionError('unexpected download')):
            self.module.setup(self.project)
        self.assertEqual(self.module.node_version(self.project / '.tools/node/bin/node'), 'v' + VERSION)

    def test_checksum_failure_preserves_previous_node(self):
        old = self.node(self.project / '.tools/node/bin/node', '24.0.0')
        with self.download(self.archive(), bad_hash=True), self.assertRaisesRegex(RuntimeError, 'SHA256'):
            self.module.setup(self.project)
        self.assertEqual(self.module.node_version(old), 'v24.0.0')

    def test_archive_cannot_escape_installation_directory(self):
        with self.download(self.archive(unsafe=True)), self.assertRaises(tarfile.TarError):
            self.module.setup(self.project)
        self.assertFalse((self.project / '.tools/escape').exists())
        self.assertFalse((self.project / '.tools/node').exists())

