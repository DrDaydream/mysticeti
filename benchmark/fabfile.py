"""Run from benchmark/: fab check, info, create, install, remote, kill, stop, start, destroy."""
from contextlib import contextmanager
from functools import wraps
import json
from pathlib import Path

from fabric import task
from invoke.exceptions import Exit

from mysticeti_aws.cloud import Cloud
from mysticeti_aws.remote import Runner
from mysticeti_aws.settings import integer, load_settings, select_adversary, validate_benchmark


@contextmanager
def operation_lock():
    import fcntl
    with (Path(__file__).parent / '.mysticeti-operation.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another lifecycle/install/remote command is running. Use fab kill to interrupt it.') from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def guarded(function=None, *, lock=True):
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            try:
                if lock:
                    with operation_lock():
                        return fn(*args, **kwargs)
                return fn(*args, **kwargs)
            except KeyboardInterrupt:
                raise Exit('Interrupted; experiment cleanup was attempted. Use fab kill if any host was unreachable.', code=130)
            except Exception as error:
                raise Exit(str(error), code=1) from error
        return invoke
    return decorate(function) if function else decorate


@task
@guarded(lock=False)
def check(ctx, settings='settings.json'):
    """Validate settings and the private-key path without creating cloud resources."""
    config = load_settings(settings)
    if not Path(config['key']['path']).is_file():
        raise ValueError(f'SSH private key not found: {config["key"]["path"]}')
    config['benchmark'] = validate_benchmark(config['benchmark'])
    print(json.dumps(config, indent=2))
    print('Settings valid. fab info checks AWS access; fab create creates new instances.')


@task
@guarded(lock=False)
def info(ctx, settings='settings.json'):
    """Show this Mysticeti testbed's instances and SSH commands."""
    config = load_settings(settings)
    records = Cloud(config).info()
    print(f'Testbed: {config["testbed_id"]}; owned instances: {len(records)}')
    for row in records:
        print(f'{row["region"]:18} {row["instance_id"]:22} {row["state"]:12} {row["ip"] or "-"}')
        if row['state'] == 'running' and row['ip']:
            print(f'  ssh -i {config["key"]["path"]} ubuntu@{row["ip"]}')


@task
@guarded
def create(ctx, nodes=2, settings='settings.json'):
    """Add N new Ubuntu 24.04 instances PER REGION (five regions, N=2 means 10)."""
    config = load_settings(settings)
    count = integer(nodes, 'nodes per region', 1, 128)
    print(f'Creating {count} per region, {count * len(config["instances"]["regions"])} NEW instances.', flush=True)
    hosts = Cloud(config).create(count)
    print(f'Running owned instances: {len(hosts)}. Next: fab install')


@task
@guarded
def install(ctx, settings='settings.json'):
    """Resolve clean controller source and visibly compile that exact commit on controller and nodes."""
    Runner(load_settings(settings)).install()


@task
@guarded
def remote(ctx, nodes=None, rate=None, duration=None, warmup=None, tx_size=None,
           faults=None, runs=None, leaders=None, leader_timeout_ms=None, scrape_interval=None,
           adversary=None, byzantine=None, byzantine_ids=None, settings='settings.json'):
    """Run prebuilt Mysticeti validators and collect shared commit metrics; never compile."""
    config = load_settings(settings)
    overrides = dict(nodes=nodes, rate=rate, duration=duration, warmup=warmup, tx_size=tx_size,
                     faults=faults, runs=runs, leaders=leaders, leader_timeout_ms=leader_timeout_ms,
                     scrape_interval=scrape_interval)
    parameters = dict(config['benchmark'])
    parameters.update({key: value for key, value in overrides.items() if value is not None})
    parameters = select_adversary(parameters, adversary, byzantine, byzantine_ids)
    Runner(config).remote(validate_benchmark(parameters))


@task
@guarded(lock=False)
def kill(ctx, settings='settings.json'):
    """Stop only this testbed's Mysticeti tmux socket; retain EC2 and result files."""
    Runner(load_settings(settings)).kill()


@task
@guarded
def stop(ctx, settings='settings.json'):
    """Stop this testbed's EC2 instances; EBS storage is retained."""
    print(f'Stopped {Cloud(load_settings(settings)).stop()} owned instances.')


@task
@guarded
def start(ctx, max=2, settings='settings.json'):
    """Start existing instances up to MAX total running instances per region."""
    hosts = Cloud(load_settings(settings)).start(integer(max, 'max', 1, 128))
    print(f'Running owned instances: {len(hosts)}. Existing builds are reused by fab remote.')


@task
@guarded
def destroy(ctx, settings='settings.json'):
    """Terminate this owned Mysticeti testbed and remove its dedicated security groups."""
    print(f'Destroyed {Cloud(load_settings(settings)).destroy()} owned instances.')
