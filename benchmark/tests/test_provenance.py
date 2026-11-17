import hashlib
import json
import subprocess

import pytest


def git(checkout, *args):
    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()


def checkout_fixture(path, attack=True):
    path.mkdir()
    git(path, 'init')
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    git(path, 'remote', 'add', 'origin', 'https://github.com/DrDaydream/mysticeti.git')
    source = path / 'mysticeti-core/src/adversary.rs'
    source.parent.mkdir(parents=True)
    source.write_text('// fixture attack source' if attack else '// baseline')
    if not attack:
        source.rename(source.with_name('lib.rs'))
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'fixture')
    return path


def test_source_revision_is_clean_checkout_head_not_original_upstream(tmp_path):
    from mysticeti_aws.source import resolve_source
    checkout = checkout_fixture(tmp_path / 'checkout')
    record = resolve_source(checkout, {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'HEAD'})
    assert record['commit'] == git(checkout, 'rev-parse', 'HEAD')
    assert record['repository'] == 'https://github.com/DrDaydream/mysticeti.git'
    (checkout / 'mysticeti-core/src/adversary.rs').write_text('// user changes')
    with pytest.raises(RuntimeError, match='clean'):
        resolve_source(checkout, {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'HEAD'})


def test_explicit_ref_is_resolved_and_must_include_attack_source(tmp_path):
    from mysticeti_aws.source import resolve_source
    checkout = checkout_fixture(tmp_path / 'checkout', attack=False)
    git(checkout, 'tag', 'baseline')
    source = checkout / 'mysticeti-core/src/adversary.rs'
    source.write_text('// fixture attack source')
    git(checkout, 'add', '.')
    git(checkout, 'commit', '-m', 'attack')
    git(checkout, 'tag', 'attack-v1')
    actual = resolve_source(checkout, {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'attack-v1'})
    assert actual['commit'] == git(checkout, 'rev-parse', 'attack-v1')
    with pytest.raises(RuntimeError, match='adversary'):
        resolve_source(checkout, {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'baseline'})


def test_source_cannot_resolve_another_repository(tmp_path):
    from mysticeti_aws.source import resolve_source
    checkout = checkout_fixture(tmp_path / 'checkout')
    git(checkout, 'remote', 'set-url', 'origin', 'https://github.com/example/unrelated.git')
    with pytest.raises(RuntimeError, match='origin'):
        resolve_source(checkout, {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'HEAD'})


def build_fixture(path):
    from mysticeti_aws.settings import RUSTFLAGS, RUST_TOOLCHAIN
    binary = path / 'target/release/mysticeti'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'verified fixture')
    binary.chmod(0o755)
    expected = {'commit': 'a' * 40, 'repository': 'https://github.com/DrDaydream/mysticeti.git', 'source_ref': 'HEAD'}
    record = dict(expected, rust_toolchain=RUST_TOOLCHAIN, rustflags=RUSTFLAGS,
                  sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), adversary_schema=1)
    (path / '.mysticeti-build.json').write_text(json.dumps(record))
    return expected, record


def test_build_provenance_rejects_wrong_revision_and_old_binary_manifest(tmp_path):
    from mysticeti_aws.build import verify_build
    expected, record = build_fixture(tmp_path)
    assert verify_build(tmp_path, expected) == record
    with pytest.raises(RuntimeError, match='fab install'):
        verify_build(tmp_path, dict(expected, commit='b' * 40))
    del record['adversary_schema']
    (tmp_path / '.mysticeti-build.json').write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match='fab install'):
        verify_build(tmp_path, expected)


def test_remote_verification_executes_same_provenance_checks(tmp_path, capsys):
    from mysticeti_aws.build import verification_script
    expected, record = build_fixture(tmp_path)
    script = verification_script(str(tmp_path), expected)
    code = '\n'.join(script.splitlines()[1:-1])
    exec(code, {})
    assert json.loads(capsys.readouterr().out) == record
