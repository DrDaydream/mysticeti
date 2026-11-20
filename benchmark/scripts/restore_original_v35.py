#!/usr/bin/env python3
"""Restore original v3.5 tools/settings without changing source or result archives."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

from resume_shared import ORIGINAL_TOOLS

BASE = 'b813f80705c3fb9ef240e2b319fd1675ed0be91a'
URL = ('https://raw.githubusercontent.com/DrDaydream/Orca-A/'
       'daabc5ed1a852cd690f7aaccf1a6fdf51cd57cff/five-protocol-tools-v3.5.zip')
PACKAGE_SHA = 'c95ec84c18c866c192ef3a96d9314f64877bbf3ff4421c2f47f94568280e78fd'


def restore(root, previous, package=None):
    bench, toolkit = root / 'mysticeti/benchmark', root / 'figure-tools'
    campaign = json.loads((previous / 'campaign.json').read_text(encoding='utf-8-sig'))
    original = campaign['Mysticeti']
    if original['commit'] != BASE:
        raise ValueError('Original campaign is not the b813f80 experiment')
    digest = lambda b: hashlib.sha256(b).hexdigest()
    candidates = [bench / 'settings.json', *sorted(bench.glob('settings*.json')),
                  *sorted((root / 'rollback-backups').glob('*/settings.json'))]
    matched = [p for p in candidates if p.is_file() and digest(p.read_bytes()) == original['settings_sha256']]
    if not matched:
        raise ValueError('No settings backup matches the original campaign; files were not changed')
    settings = matched[0].read_bytes()
    payload = package.read_bytes() if package else urllib.request.urlopen(URL, timeout=60).read()
    if digest(payload) != PACKAGE_SHA:
        raise ValueError('Original toolkit package checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        restored = {name: archive.read('five-protocol-tools/' + name) for name in ORIGINAL_TOOLS}
    for name, data in restored.items():
        if digest(data) != ORIGINAL_TOOLS[name]:
            raise ValueError('Original toolkit file checksum mismatch: ' + name)
        compile(data, name, 'exec')
        path = toolkit / name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing or symlinked existing toolkit file: ' + str(path))
    for name, key in [('run_matrix.py', 'wrapper_sha256'), ('lifecycle.py', 'lifecycle_sha256'),
                      ('protocol_tasks.py', 'protocol_tasks_sha256')]:
        if ORIGINAL_TOOLS[name] != original[key]:
            raise ValueError('Original toolkit differs from the previous campaign: ' + name)
    # The zero-output branch retains the original native runner, parser and Fab CLI.
    if digest((bench / 'fabfile.py').read_bytes()) != original['fabfile_sha256']:
        raise ValueError('Mysticeti native Fab CLI differs from the original experiment')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = root / 'rollback-backups' / stamp
    backup.mkdir(parents=True)
    shutil.copy2(bench / 'settings.json', backup / 'settings.json')
    for name in restored:
        shutil.copy2(toolkit / name, backup / name)
    for path, data in [(bench / 'settings.json', settings), *[(toolkit / n, b) for n, b in restored.items()]]:
        temporary = path.with_name(path.name + '.restore-' + stamp)
        temporary.write_bytes(data)
        shutil.copymode(path, temporary)
        temporary.replace(path)
    print('Original v3.5 restored; duration=60s, attack=20-50s after each node initializes.')
    print('Settings restored from:', matched[0])
    print('Previous patched files retained at:', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/experiments'))
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--package', type=Path, help='Verified original zip; otherwise download the pinned release')
    args = parser.parse_args()
    root = args.root.resolve()
    previous = (args.previous or root / 'paper-results-v3.5-60s-h20-crash-a1').resolve()
    import fcntl
    with open('/tmp/five-protocol-figure-matrix.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore(root, previous, args.package)


if __name__ == '__main__':
    main()
