import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_remote_refuses_missing_build_without_installing(monkeypatch, tmp_path):
    from mysticeti_aws.remote import Runner
    from mysticeti_aws.settings import DEFAULTS
    settings=copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir']=str(tmp_path/'unbuilt')
    runner=Runner(settings, cloud=SimpleNamespace())
    monkeypatch.setattr(runner,'install',lambda: pytest.fail('remote tried to install'))
    with pytest.raises(RuntimeError,match='fab install'):
        runner.remote(settings['benchmark'])


def test_generated_private_paths_and_timeout_match_remote_runtime(monkeypatch,tmp_path):
    import yaml
    from mysticeti_aws.remote import generate_config
    from mysticeti_aws.settings import DEFAULTS
    def fake_genesis(args, **kwargs):
        output=Path(args[args.index('--working-directory')+1])
        (output/'private').mkdir(parents=True)
        (output/'committee.yaml').write_text('fixture: true')
        (output/'parameters.yaml').write_text('leader_timeout: {secs: 2, nanos: 0}\n')
        for i in range(4):
            (output/f'private/{i}.yaml').write_text(f'authority_index: {i}\nkeypair: 0\nstorage_path: /controller/val-{i}\n')
    monkeypatch.setattr('mysticeti_aws.remote.subprocess.run',fake_genesis)
    generate_config('/binary',['1.2.3.4']*4,tmp_path/'genesis','/home/ubuntu/experiments/mysticeti-runs/unique',dict(DEFAULTS['benchmark'],nodes=4))
    private=yaml.safe_load((tmp_path/'genesis/private/2.yaml').read_text())
    params=yaml.safe_load((tmp_path/'genesis/parameters.yaml').read_text())
    assert private['storage_path']=='/home/ubuntu/experiments/mysticeti-runs/unique/private/val-2'
    assert params['leader_timeout']=={'secs':1,'nanos':0}


def test_collection_failure_stops_nodes_and_keeps_failure_record(monkeypatch,tmp_path):
    from mysticeti_aws.remote import Runner
    from mysticeti_aws.settings import DEFAULTS
    settings=copy.deepcopy(DEFAULTS)
    runner=Runner(settings,cloud=SimpleNamespace(),results_dir=tmp_path)
    hosts=[SimpleNamespace(ip=f'1.2.3.{i}',region='us-east-1',instance_id=f'i-{i}') for i in range(1,5)]
    calls=[]
    monkeypatch.setattr(runner,'_stop',lambda hosts: calls.append('stop') or [])
    monkeypatch.setattr(runner,'_configure_and_start',lambda *args: None)
    monkeypatch.setattr(runner,'_wait_ready',lambda *args: (_ for _ in ()).throw(RuntimeError('node died')))
    monkeypatch.setattr(runner,'_download_logs',lambda *args: calls.append('logs') or [])
    params=dict(settings['benchmark'],nodes=4,warmup=0)
    with pytest.raises(RuntimeError,match='node died'):
        runner._run_once(hosts,params)
    assert calls.count('stop')>=2
    assert calls[-1]=='logs'
    records=list(tmp_path.glob('*/run.json'))
    assert len(records)==1
    assert json.loads(records[0].read_text())['status']=='failed'


def test_end_to_end_runner_uses_prebuilt_binary_and_reports_replica_mean(monkeypatch,tmp_path):
    import hashlib
    import threading
    import yaml
    from mysticeti_aws.remote import Runner
    from mysticeti_aws.settings import DEFAULTS,REPOSITORY,RUSTFLAGS,RUST_TOOLCHAIN
    provenance={'commit':'a'*40,'repository':REPOSITORY,'source_ref':'HEAD'}
    settings=copy.deepcopy(DEFAULTS)
    settings['remote']['source_dir']=str(tmp_path/'source')
    binary=Path(settings['remote']['source_dir'])/'target/release/mysticeti'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'prebuilt-fixture'); binary.chmod(0o755)
    build={**provenance,'adversary_schema':1,'rustflags':RUSTFLAGS,'rust_toolchain':RUST_TOOLCHAIN,
           'sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
    (binary.parents[2]/'.mysticeti-build.json').write_text(json.dumps(build))
    commands=[]; commands_lock=threading.Lock(); counters={}
    hosts=[SimpleNamespace(ip=f'1.2.3.{i}',region='us-east-1',instance_id=f'i-{i}') for i in range(4)]
    cloud=SimpleNamespace(hosts=lambda:hosts,sync_network=lambda:None)
    class SSH:
        def __init__(self,host): self.host=host
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def open(self): pass
        def put(self,*args,**kwargs): pass
        def get(self,remote,local): Path(local).write_text('node log')
        def run(self,command,**kwargs):
            with commands_lock:
                commands.append(command)
                if '/metrics' in command:
                    index=counters.get(self.host.ip,0)+1
                    counters[self.host.ip]=index
                    return SimpleNamespace(stdout=f'latency_s_count{{workload="shared"}} {index*100}\nlatency_s_sum{{workload="shared"}} {index*20}\n')
            return SimpleNamespace(stdout=json.dumps(build))
    def genesis(args,**kwargs):
        assert args[0]==str(binary)
        assert args[1]=='benchmark-genesis'
        output=Path(args[args.index('--working-directory')+1]); (output/'private').mkdir(parents=True)
        (output/'committee.yaml').write_text('fixture: true')
        (output/'parameters.yaml').write_text('leader_timeout: {secs: 2, nanos: 0}\n')
        for i in range(4):
            (output/f'private/{i}.yaml').write_text(yaml.safe_dump({'authority_index':i,'keypair':0,'storage_path':'/old'}))
    monkeypatch.setattr('mysticeti_aws.remote.subprocess.run',genesis)
    runner=Runner(settings,cloud=cloud,results_dir=tmp_path/'results')
    monkeypatch.setattr(runner,'_ssh',SSH)
    monkeypatch.setattr(runner,'_resolve_source',lambda:provenance)
    runner.remote(dict(settings['benchmark'],nodes=4,duration=1,warmup=0,scrape_interval=1))
    summary=json.loads(next((tmp_path/'results').glob('*/summary.json')).read_text())
    assert summary['mean_latency_ms']==pytest.approx(200)
    assert len(summary['nodes'])==4
    assert summary['replica_tps_mean']==pytest.approx(sum(n['tps'] for n in summary['nodes'])/4)
    assert len(list((tmp_path/'results').glob('*/logs/node-*.log')))==4
    assert all(not any(word in command.split() for word in ('cargo','git','apt-get','rustup')) for command in commands)
