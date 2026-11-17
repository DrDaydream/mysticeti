"""Regression coverage for create -> install -> remote in the original checkout."""
import copy
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_provenance import checkout_fixture, git


def test_controller_source_can_be_the_adapter_checkout(monkeypatch, tmp_path):
    from mysticeti_aws import remote
    from mysticeti_aws.settings import DEFAULTS
    checkout = checkout_fixture(tmp_path / 'checkout')
    settings = copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir'] = str(checkout)
    monkeypatch.setattr(remote, 'PACKAGE_DIR', checkout / 'benchmark')
    assert remote.Runner(settings)._resolve_source()['commit'] == git(checkout, 'rev-parse', 'HEAD')


def test_shared_checkout_rejects_a_different_ref_without_changing_head(monkeypatch, tmp_path):
    from mysticeti_aws import remote
    from mysticeti_aws.settings import DEFAULTS
    checkout = checkout_fixture(tmp_path / 'checkout')
    previous = git(checkout, 'rev-parse', 'HEAD')
    git(checkout, 'commit', '--allow-empty', '-m', 'new controller')
    current = git(checkout, 'rev-parse', 'HEAD')
    settings = copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir'] = str(checkout)
    settings['repo']['ref'] = previous
    monkeypatch.setattr(remote, 'PACKAGE_DIR', checkout / 'benchmark')
    with pytest.raises(ValueError, match='repo.ref.*HEAD'):
        remote.Runner(settings)._resolve_source()
    assert git(checkout, 'rev-parse', 'HEAD') == current


def test_source_sync_at_current_revision_preserves_branch_and_settings(tmp_path):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('bash unavailable')
    checkout = checkout_fixture(tmp_path / 'checkout')
    # An unreachable origin proves an up-to-date checkout needs no fetch.
    repository = (tmp_path / 'unreachable-origin').as_posix()
    git(checkout, 'remote', 'set-url', 'origin', repository)
    (checkout / '.gitignore').write_text('benchmark/settings.json\n')
    git(checkout, 'add', '.gitignore')
    git(checkout, 'commit', '-m', 'ignore local settings')
    settings = checkout / 'benchmark/settings.json'
    settings.parent.mkdir()
    settings.write_text('{"local": "preserve me"}')
    before = git(checkout, 'symbolic-ref', 'HEAD')
    commit = git(checkout, 'rev-parse', 'HEAD')
    script = Path(__file__).parents[1] / 'scripts/sync_source.sh'
    result = subprocess.run([str(bash), script.as_posix(), checkout.as_posix(),
                             commit, repository], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert git(checkout, 'symbolic-ref', 'HEAD') == before
    assert settings.read_text() == '{"local": "preserve me"}'


def test_install_uses_shared_checkout_and_the_same_revision_on_nodes(monkeypatch, tmp_path):
    from mysticeti_aws import remote
    from mysticeti_aws.settings import DEFAULTS
    checkout = checkout_fixture(tmp_path / 'checkout')
    commit = git(checkout, 'rev-parse', 'HEAD')
    settings = copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir'] = str(checkout)
    monkeypatch.setattr(remote, 'PACKAGE_DIR', checkout / 'benchmark')
    host = SimpleNamespace(ip='203.0.113.1', region='test')
    cloud = SimpleNamespace(hosts=lambda: [host], sync_network=lambda: None)
    runner = remote.Runner(settings, cloud=cloud, results_dir=tmp_path / 'results')
    local_commands, remote_commands, verified = [], [], []
    monkeypatch.setattr(runner, '_wait_ssh', lambda host: None)
    monkeypatch.setattr(remote.subprocess, 'run', lambda args, **kw: local_commands.append(args))
    # check_output internally calls run; keep source resolution using the real Git helper.
    monkeypatch.setattr(runner, '_resolve_source', lambda: {
        'commit': commit, 'repository': settings['repo']['url'], 'source_ref': 'HEAD'})
    monkeypatch.setattr(remote, 'verify_build', lambda source, provenance:
                        verified.append((source, provenance['commit'])) or {})

    class SSH:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def put(self, *args, **kwargs): pass
        def run(self, command, **kwargs):
            remote_commands.append(command)
            return SimpleNamespace(stdout='{}')

    monkeypatch.setattr(runner, '_ssh', lambda host: SSH())
    runner.install()
    assert local_commands == [['bash', str(checkout / 'benchmark/scripts/install_node.sh'),
                               str(checkout), commit, '4', settings['repo']['url']]]
    installs = [command for command in remote_commands if command.startswith('bash ')]
    assert len(installs) == 1
    assert commit in installs[0] and settings['repo']['url'] in installs[0]
    assert verified == [(checkout, commit), (checkout, commit)]
    assert (tmp_path / 'results/installation.json').is_file()
