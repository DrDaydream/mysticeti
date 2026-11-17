import copy
import json
from contextlib import nullcontext
from pathlib import Path
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


def test_install_cannot_reuse_the_controller_adapter_checkout():
    from mysticeti_aws import remote
    settings = copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir'] = str(remote.PACKAGE_DIR.parent)
    runner = remote.Runner(settings, cloud=SimpleNamespace())
    with pytest.raises(ValueError, match='source_dir'):
        runner._resolve_source()
