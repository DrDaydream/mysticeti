#!/usr/bin/env python3
"""Probe the compiled program and atomically publish a provenance manifest."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

# All fields are emitted by the Rust genesis command, including optional targets.
FIELDS = {'mode', 'byzantine_authorities', 'seed', 'start_after_ms', 'duration_ms',
          'delay_ms', 'late_delay_ms', 'max_pending_messages', 'max_pending_bytes',
          'max_extra_bytes_per_round', 'target_authority', 'target_round'}


def run_binary(args, **kwargs):
    return subprocess.run(args, **kwargs)


def write_build_record(source, revision, repository):
    source = Path(source).resolve()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise RuntimeError('A full resolved source commit is required')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != revision or git('remote', 'get-url', 'origin') != repository:
        raise RuntimeError('Source provenance changed during build')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise RuntimeError('Tracked source changed during build')
    git('cat-file', '-e', revision + ':' + 'mysticeti-core/src/adversary.rs')
    binary = source / 'target/release/mysticeti'
    run_binary([str(binary), '--help'], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix='adversary-genesis-') as directory:
        args = [str(binary), 'benchmark-genesis', '--ips', *(['127.0.0.1'] * 4),
                '--working-directory', directory]
        run_binary(args, check=True, stdout=subprocess.DEVNULL)
        public = yaml.safe_load((Path(directory) / 'parameters.yaml').read_text())
        parameters = public
        attack = parameters.get('adversary', {})
        if not isinstance(attack, dict) or set(attack) != FIELDS or attack.get('mode') != 'A0':
            raise RuntimeError('Built binary does not expose the A0-A5 adversary schema')
    digest = hashlib.sha256()
    with binary.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    record = {'commit': revision, 'repository': repository, 'rust_toolchain': '1.72.1',
              'rustflags': '-C target-cpu=native', 'adversary_schema': 1,
              'sha256': digest.hexdigest()}
    temporary = source / ('.mysticeti-build.json' + '.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    temporary.replace(source / '.mysticeti-build.json')
    print('Build verified: ' + record['sha256'])
    return record


if __name__ == '__main__':
    write_build_record(*sys.argv[1:])
