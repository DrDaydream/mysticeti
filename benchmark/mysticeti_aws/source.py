"""Resolve an exact modified-source commit before installation or any cloud action."""
import re
import subprocess
from pathlib import Path

ADVERSARY_MODULE = 'mysticeti-core/src/adversary.rs'


def resolve_source(checkout, repository):
    checkout = Path(checkout)
    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(checkout), *args],
                                           text=True, stderr=subprocess.PIPE).strip()
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f'Cannot validate source checkout {checkout}: {error.stderr.strip()}') from error
    if git('remote', 'get-url', 'origin') != repository['url']:
        raise RuntimeError('Controller checkout origin does not match repo.url')
    if git('status', '--porcelain', '--untracked-files=normal'):
        raise RuntimeError('Use a clean controller checkout: commit source edits before fab install/remote')
    ref = repository['ref']
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', ref):
        raise RuntimeError('Invalid source ref')
    commit = git('rev-parse', '--verify', '--end-of-options', ref + '^{commit}')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError('Source ref did not resolve to a full commit hash')
    try:
        git('cat-file', '-e', commit + ':' + ADVERSARY_MODULE)
    except RuntimeError as error:
        raise RuntimeError('Selected source commit lacks the adversary module; upstream binaries cannot run A0-A5 experiments') from error
    return {'repository': repository['url'], 'source_ref': ref, 'commit': commit}
