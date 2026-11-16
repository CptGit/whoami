#!/usr/bin/env python3
"""Prepare the pinned development Node.js without changing the system installation."""
import hashlib
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen


def node_version(binary):
    try:
        return subprocess.check_output([str(binary), '--version'], text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def setup(project):
    version = (project / '.node-version').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('.node-version 必须指定完整版本号。')
    expected = 'v' + version
    target = project / '.tools/node'
    if node_version(target / 'bin/node') == expected:
        return expected

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.node-', dir=target.parent) as temporary:
        temporary = Path(temporary)
        system_node = shutil.which('node')
        if system_node and node_version(system_node) == expected:
            staged = temporary / 'node'
            staged.symlink_to(Path(system_node).resolve().parent.parent, target_is_directory=True)
        else:
            system = {'Linux': 'linux', 'Darwin': 'darwin'}.get(platform.system())
            arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
            if not system or not arch:
                raise RuntimeError('自动安装 Node 支持 Linux / macOS 的 x64、arm64 架构。')
            name = f'node-{expected}-{system}-{arch}'
            archive_name = name + '.tar.gz'
            base = f'https://nodejs.org/dist/{expected}/'
            with urlopen(base + 'SHASUMS256.txt', timeout=30) as response:
                checksums = dict((line.split()[1], line.split()[0]) for line in response.read().decode().splitlines() if line.strip())
            checksum = checksums.get(archive_name, '')
            if not re.fullmatch(r'[0-9a-f]{64}', checksum):
                raise RuntimeError('官方校验文件中缺少所需 Node 安装包。')
            archive = temporary / archive_name
            with urlopen(base + archive_name, timeout=60) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            with archive.open('rb') as downloaded:
                actual_checksum = hashlib.file_digest(downloaded, 'sha256').hexdigest()
            if actual_checksum != checksum:
                raise RuntimeError('Node 安装包 SHA256 校验失败。')
            with tarfile.open(archive) as package:
                package.extractall(temporary, filter='data')
            staged = temporary / name
            if node_version(staged / 'bin/node') != expected:
                raise RuntimeError('下载的 Node 无法在当前系统运行。')

        # Keep the previous tool intact if downloading or verification fails.
        backup = temporary / 'previous'
        if target.exists() or target.is_symlink():
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup.exists() or backup.is_symlink():
                backup.rename(target)
            raise
    return expected


if __name__ == '__main__':
    print(f'Node {setup(Path(__file__).resolve().parents[1])} 已就绪。')
