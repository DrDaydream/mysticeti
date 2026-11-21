#!/usr/bin/env python3
"""Install the reviewed figure toolkit with an exact source-commit lock."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


FILES = ('run_matrix.py', 'plot_figures.py', 'lifecycle.py', 'protocol_tasks.py', 'fabfile.py')


def source_commit(repository):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args], text=True).strip()
    if git('status', '--porcelain'):
        raise ValueError('Use a clean, committed checkout before preparing the toolkit')
    commit = git('rev-parse', 'HEAD')
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Could not resolve source commit')
    return commit


def prepare(root):
    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[2]
    commit = source_commit(repository)
    source = repository / 'benchmark' / 'figure-tools-shared-upstream'
    target = root / 'figure-tools-shared-upstream'
    payload = {name: (source / name).read_bytes() for name in FILES}
    for name, data in payload.items():
        compile(data, name, 'exec')
    lock = dict(protocols={
                    'Mysticeti': dict(commit=commit, upstream_base='68d9038f4055e67ced4ae96c1de46b9b0239413b'),
                    'Mahi-Mahi': dict(commit='3c7d32fa815defca8ab82129a461d5a417082e7b', upstream_base='7c928c06bd436c96b94065119e0e8c2d4ac1bcb6')},
                toolkit_sha256={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()})
    payload['source-lock.json'] = (json.dumps(lock, indent=2) + '\n').encode('utf-8')
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError('Toolkit target must be a normal directory directly under the experiment root')
    # Validate every file before writing; never overwrite another delivery.
    for name, data in payload.items():
        path = target / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError(f'Existing toolkit differs; preserved without changes: {path}')
    target.mkdir(parents=True, exist_ok=True)
    for name, data in payload.items():
        path = target / name
        if not path.exists():
            path.write_bytes(data)
    print(f'Toolkit: {target}\nSource commit: {commit}')
    print('Mysticeti/Mahi-Mahi: upstream normal protocol plus existing A3/A5 adversary overlay.')
    print('A0/A1: upstream terminal cumulative statistics; A3/A5: existing window deltas.')
    print('All modes: warmup=0, duration=60; A1: native A0 plus tail crash faults.')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/experiments'))
    prepare(parser.parse_args().root)
