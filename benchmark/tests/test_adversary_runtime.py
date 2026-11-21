import copy
import json
import os
from contextlib import nullcontext
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from mysticeti_aws.settings import DEFAULTS, validate_benchmark


def genesis_fixture(args, **kwargs):
    output = Path(args[args.index('--working-directory') + 1])
    ips = args[args.index('--ips') + 1:args.index('--working-directory')]
    output.mkdir(parents=True, exist_ok=True)
    (output / 'committee.yaml').write_text('fixture: true')
    identifiers = [dict(network_address=f'{ip}:{1500+i}', metrics_address=f'{ip}:{1500+len(ips)+i}') for i, ip in enumerate(ips)]
    attack = validate_benchmark(DEFAULTS['benchmark'])['adversary']
    if 'mysticeti_aws' == 'mysticeti_aws':
        (output / 'parameters.yaml').write_text(yaml.safe_dump({'identifiers': identifiers, 'adversary': attack}))
        (output / 'private').mkdir(exist_ok=True)
        for i in range(len(ips)):
            (output / f'private/{i}.yaml').write_text(yaml.safe_dump({'authority_index': i, 'storage_path': '/old'}))
    else:
        (output / 'public-config.yaml').write_text(yaml.safe_dump({'identifiers': identifiers, 'parameters': {'adversary': attack}}))
        for i in range(len(ips)):
            (output / f'private-config-{i}.yaml').write_text(yaml.safe_dump({'authority': i, 'storage_path': '/old'}))


def test_all_replicas_receive_same_attack_configuration_and_synchronizer(monkeypatch, tmp_path):
    from mysticeti_aws import remote
    monkeypatch.setattr(remote.subprocess, 'run', genesis_fixture)
    settings = copy.deepcopy(DEFAULTS)
    runner = remote.Runner(settings, cloud=SimpleNamespace())
    uploads = {}
    class SSH:
        def __init__(self, host): self.ip = host.ip
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, command, **kwargs): return SimpleNamespace(stdout='')
        def put(self, source, remote):
            uploads[(self.ip, remote)] = source.getvalue().decode() if hasattr(source, 'getvalue') else Path(source).read_text()
    monkeypatch.setattr(runner, '_ssh', SSH)
    monkeypatch.setattr(runner, '_parallel', lambda fn, values: [fn(v) for v in values])
    parameters = validate_benchmark(dict(settings['benchmark'], nodes=4, rate=400, adversary={'mode': 'A5'}))
    hosts = [SimpleNamespace(ip=f'203.0.113.{i+1}') for i in range(4)]
    runner._configure_and_start(hosts, parameters, tmp_path, '/runs/test')
    name = 'parameters.yaml' if 'mysticeti_aws' == 'mysticeti_aws' else 'public-config.yaml'
    configs = [uploads[(h.ip, '/runs/test/' + name)] for h in hosts]
    assert len(set(configs)) == 1
    public = yaml.safe_load(configs[0])
    actual = public if 'mysticeti_aws' == 'mysticeti_aws' else public['parameters']
    assert actual['adversary'] == parameters['adversary']
    assert actual['wave_length'] in (3, 4, 5)
    if 'mysticeti_aws' == 'mysticeti_aws':
        assert all('export USE_SYNCER=1' in uploads[(h.ip, '/runs/test/start.sh')] for h in hosts)
    else:
        assert actual['enable_synchronizer'] is True


@pytest.mark.parametrize('mode,enabled', [(f'A{i}', True) for i in range(6)] + [('A0', False)])
@pytest.mark.parametrize('inherited', [None, '0', '1'])
def test_node_launch_sets_effective_legacy_fetcher_environment(monkeypatch, tmp_path, mode, enabled, inherited):
    """Execute the uploaded script: parent/tmux variables must not invert the setting.

    The prototype's BlockFetcherWorker returns early when USE_SYNCER exists,
    irrespective of its value. Restore the upstream launcher's export, including
    this inverted flag, while explicitly overriding inherited environment.
    """
    from mysticeti_aws import remote
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('bash unavailable')
    run = subprocess.run
    monkeypatch.setattr(remote.subprocess, 'run', genesis_fixture)
    runner = remote.Runner(copy.deepcopy(DEFAULTS), cloud=SimpleNamespace())
    node = tmp_path / 'node'
    node.mkdir()
    runner.binary = tmp_path / 'fake-mysticeti'
    runner.binary.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "${USE_SYNCER-unset}"\n', newline='\n')
    runner.binary.chmod(0o755)
    uploaded = []

    class SSH:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, *args, **kwargs): pass
        def put(self, source, remote):
            if remote.endswith('/start.sh'):
                uploaded.append(source.getvalue().decode())

    monkeypatch.setattr(runner, '_ssh', lambda host: SSH())
    monkeypatch.setattr(runner, '_parallel', lambda fn, values: [fn(v) for v in values])
    parameters = validate_benchmark(dict(copy.deepcopy(DEFAULTS['benchmark']), nodes=4,
                                        enable_synchronizer=enabled, adversary={'mode': mode}))
    runner._configure_and_start([SimpleNamespace(ip='127.0.0.1') for _ in range(4)],
                                parameters, tmp_path / 'config', node.as_posix())
    assert len(uploaded) == 4
    env = dict(os.environ)
    env.pop('USE_SYNCER', None)
    if inherited is not None:
        env['USE_SYNCER'] = inherited
    for index, script in enumerate(uploaded):
        start = tmp_path / f'start-{index}.sh'
        start.write_text(script, newline='\n')
        result = run([str(bash), start.as_posix()], env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert (node / 'node.log').read_text().strip() == ('1' if enabled else 'unset')


def test_fabric_remote_parser_accepts_attack_selector_and_count(monkeypatch, tmp_path):
    import fabfile
    from invoke import Collection, Program
    captured = []
    class Runner:
        def __init__(self, config): pass
        def remote(self, parameters): captured.append(parameters)
    settings = tmp_path / 'settings.json'
    settings.write_text('{}')
    monkeypatch.setattr(fabfile, 'Runner', Runner)
    monkeypatch.setattr(fabfile, 'operation_lock', nullcontext)
    Program(namespace=Collection(fabfile.remote)).run([
        'fab', 'remote', '--settings', str(settings), '--adversary', 'A3', '--byzantine', '2', '--warmup', '30'], exit=False)
    assert len(captured) == 1
    assert captured[0]['adversary']['mode'] == 'A3'
    assert captured[0]['adversary']['byzantine_authorities'] == [0, 1]
    assert captured[0]['adversary']['start_after_ms'] == 30000


def test_install_rejects_a_source_nested_inside_the_controller_checkout():
    from mysticeti_aws import remote
    settings = copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir'] = str(remote.PACKAGE_DIR.parent / 'nested-source')
    runner = remote.Runner(settings, cloud=SimpleNamespace())
    with pytest.raises(ValueError, match='source_dir'):
        runner._resolve_source()
