#!/usr/bin/env python3
"""Update the original v3.5 toolkit in place, keeping a recoverable backup."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import uuid


ORIGINAL = {
    'run_matrix.py': 'd3131c1fc4f7cf16b2efc7bd007f7de91b9ea54995c0c79736b45652995689be',
    'plot_figures.py': '913211bc1581797954efbf06c4967c7f2475c4418fa2e2067292d7a10cc04158',
}
RUNTIME = ('run_matrix.py', 'plot_figures.py', 'lifecycle.py', 'protocol_tasks.py', 'fabfile.py')


def digest(data):
    return hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()


def update(target):
    bundled = Path(__file__).resolve().parents[1] / 'figure-tools'
    target = Path(target).resolve()
    if target == bundled.resolve():
        raise ValueError('Target must be the original external toolkit directory')
    current, desired, changed = {}, {}, []
    for name in RUNTIME:
        path = target / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing or symlinked toolkit file: {path}')
        current[name], desired[name] = path.read_bytes(), (bundled / name).read_bytes()
        before, after = digest(current[name]), digest(desired[name])
        if before == after:
            continue
        if name not in ORIGINAL or before != ORIGINAL[name]:
            raise ValueError(f'{path}: unrecognized version or local edits; no files changed')
        compile(desired[name].decode('utf-8'), str(path), 'exec')
        changed.append(name)
    if not changed:
        print(f'Already updated: {target}')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = target / f'backup-before-clock-{stamp}-{uuid.uuid4().hex[:8]}'
    backup.mkdir()
    for name in RUNTIME:
        shutil.copy2(target / name, backup / name)
    for name in changed:
        path = target / name
        if path.read_bytes() != current[name]:
            raise RuntimeError(f'{path} changed during update; backup is at {backup}')
        temporary = path.with_name(name + '.clock-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(desired[name])
            shutil.copymode(path, temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    print(f'Updated original toolkit: {target}')
    print(f'Backup: {backup}')
    print('Use a NEW output directory. Existing campaign identities and results were not changed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path,
                        help='Original v3.5 toolkit, normally /home/ubuntu/experiments/figure-tools')
    args = parser.parse_args()
    try:
        update(args.directory)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
