"""Verify exact source provenance and the built binary before every experiment."""
import hashlib
import inspect
import json
import os
import re
from pathlib import Path

from .settings import RUSTFLAGS, RUST_TOOLCHAIN


def verify_build(source, provenance=None):
    source = Path(source)
    try:
        record = json.loads((source / '.mysticeti-build.json').read_text())
        if not provenance or not re.fullmatch(r'[0-9a-f]{40}', provenance.get('commit', '')):
            raise ValueError('expected source commit must be resolved from the controller checkout')
        expected = {'commit': provenance['commit'], 'repository': provenance['repository'],
                    'rust_toolchain': RUST_TOOLCHAIN, 'rustflags': RUSTFLAGS,
                    'adversary_schema': 1}
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError('source commit, repository, adversary capability, or build flags do not match')
        binary = source / 'target/release/mysticeti'
        if not binary.is_file() or (os.name != 'nt' and not os.access(binary, os.X_OK)):
            raise ValueError('executable missing')
        digest = hashlib.sha256()
        with binary.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != record.get('sha256'):
            raise ValueError('binary checksum mismatch')
        return record
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as error:
        raise RuntimeError(f'Invalid build at {source}: {error}. Run fab install first.') from error


def verification_script(source, provenance):
    # Pure Python version/checksum verification; remote runs never invoke Git or Cargo.
    code = '\n'.join([
        'import hashlib, json, os, re', 'from pathlib import Path',
        f'RUST_TOOLCHAIN={RUST_TOOLCHAIN!r}', f'RUSTFLAGS={RUSTFLAGS!r}',
        inspect.getsource(verify_build), f'print(json.dumps(verify_build({source!r}, {provenance!r})))',
    ])
    return "python3 - <<'ADVERSARY_VERIFY_PY'\n" + code + '\nADVERSARY_VERIFY_PY'
