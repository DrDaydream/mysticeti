"""Internal Fabric collection: scope existing protocol tasks to one fresh pool.

The scope is supplied by lifecycle.py. No repository source files are edited.
"""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

from fabric import Connection, task
from invoke import Task
from invoke.exceptions import CommandTimedOut
from paramiko import AuthenticationException, BadHostKeyException, SSHException

PROTOCOL = os.environ['FIGURE_PROTOCOL']
SCOPE = os.environ['FIGURE_SCOPE']
BENCH = Path(os.environ['FIGURE_BENCH_DIR']).resolve()
SETTINGS = Path(os.environ['FIGURE_SETTINGS']).resolve()
if not re.fullmatch(r'fig-[a-z0-9-]{1,22}-[0-9a-f]{12}', SCOPE):
    raise ValueError('Refusing an invalid or non-batch instance scope')
if PROTOCOL not in ('Orca-A', 'Orca-B', 'Bullshark', 'Mysticeti', 'Mahi-Mahi'):
    raise ValueError('Unknown protocol')
if not SCOPE.startswith(f'fig-{PROTOCOL.lower()}-'):
    raise ValueError('Instance scope belongs to a different protocol')
os.chdir(BENCH)
sys.path.insert(0, str(BENCH))
IS_SHARED = PROTOCOL in ('Mysticeti', 'Mahi-Mahi')
LIVE_STATES = ['pending', 'running', 'stopping', 'stopped', 'shutting-down']

if IS_SHARED:
    package = 'mysticeti_aws' if PROTOCOL == 'Mysticeti' else 'mahi_aws'
    Cloud = importlib.import_module(package + '.cloud').Cloud
    load_settings = importlib.import_module(package + '.settings').load_settings
    original_init = Cloud.__init__

    def scoped_init(self, settings, *args, **kwargs):
        if settings['testbed_id'] != SCOPE:
            raise ValueError('Refusing an operation outside this batch testbed')
        original_init(self, settings, *args, **kwargs)

    Cloud.__init__ = scoped_init
else:
    from benchmark.instance import InstanceManager
    from benchmark.remote import Bench
    from run_matrix import HEADER_SIZE_BYTES

    native_run = Bench.run

    def figure_run(self, benchmark_parameters, node_parameters, debug=False):
        """Apply the figure plan's header threshold before native validation/upload."""
        node_parameters = dict(node_parameters, header_size=HEADER_SIZE_BYTES)
        return native_run(self, benchmark_parameters, node_parameters, debug)

    Bench.run = figure_run
    # Apply before importing the repository fabfile, so all its imports and
    # manager instances see the same private instance and security-group names.
    InstanceManager.INSTANCE_NAME = SCOPE
    InstanceManager.SECURITY_GROUP_NAME = SCOPE
    original_make = InstanceManager.make.__func__

    @classmethod
    def scoped_make(cls, settings_file='settings.json'):
        return original_make(cls, str(SETTINGS))

    InstanceManager.make = scoped_make

spec = importlib.util.spec_from_file_location('_figure_protocol_fabfile', BENCH / 'fabfile.py')
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)
for name, value in vars(native).items():
    if isinstance(value, Task):
        globals()[name] = value
del name, value


def legacy_inventory(manager):
    rows, groups = [], []
    for region, client in manager.clients.items():
        pages = client.get_paginator('describe_instances').paginate(Filters=[
            {'Name': 'tag:Name', 'Values': [SCOPE]},
            {'Name': 'instance-state-name', 'Values': LIVE_STATES}])
        for page in pages:
            for reservation in page.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
                    if tags.get('Name') == SCOPE and instance['State']['Name'] in LIVE_STATES:
                        rows.append(dict(region=region, instance_id=instance['InstanceId'],
                                         state=instance['State']['Name'], ip=instance.get('PublicIpAddress')))
        found = client.describe_security_groups(Filters=[{'Name': 'group-name', 'Values': [SCOPE]}])
        groups.extend(dict(region=region, group_id=g['GroupId']) for g in found.get('SecurityGroups', [])
                      if g.get('GroupName') == SCOPE)
    return rows, groups


@task
def inventory(ctx, output):
    """Write only this batch's live EC2 instance IDs and security groups."""
    if IS_SHARED:
        cloud = Cloud(load_settings(str(SETTINGS)))
        rows = cloud.info()
        groups = []
        for region in cloud.regions:
            group = cloud._security_group(region)
            if group:
                groups.append(dict(region=region, group_id=group['GroupId']))
    else:
        rows, groups = legacy_inventory(InstanceManager.make())
    Path(output).write_text(json.dumps(dict(scope=SCOPE, instances=rows, security_groups=groups), indent=2)+'\n', encoding='utf-8')
    print(f'{SCOPE}: {len(rows)} live instances; {len(groups)} security groups', flush=True)


def wait_for_ssh(hosts, connect_kwargs, timeout=300):
    """Wait for authenticated SSH before invoking the unchanged native installer."""
    pending = dict.fromkeys(hosts, 'not yet checked')
    if not pending:
        raise RuntimeError('No instances available in this batch scope')
    deadline = time.monotonic() + timeout
    while pending:
        for host in list(pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'SSH readiness timed out: {pending}')
            attempt_timeout = min(10, remaining)
            options = dict(connect_kwargs)
            options.update(allow_agent=False, look_for_keys=False,
                           banner_timeout=attempt_timeout, auth_timeout=attempt_timeout)
            try:
                with Connection(host, user='ubuntu', connect_timeout=attempt_timeout,
                                connect_kwargs=options) as connection:
                    connection.run('true', hide=True, in_stream=False, pty=False,
                                   timeout=attempt_timeout)
            except (AuthenticationException, BadHostKeyException) as error:
                raise RuntimeError(f'SSH authentication/host-key failure for {host}: {error}') from error
            except (OSError, EOFError, SSHException, CommandTimedOut) as error:
                pending[host] = f'{type(error).__name__}: {error}'
            else:
                del pending[host]
                print(f'SSH ready: {host}', flush=True)
        if pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'SSH readiness timed out: {pending}')
            print(f'Waiting for SSH on {len(pending)} instances: {pending}', flush=True)
            time.sleep(min(5, remaining))


if not IS_SHARED:
    @task
    def create(ctx, nodes=2):
        """Create N instances per region; propagate errors from legacy adapters."""
        InstanceManager.make().create_instances(int(nodes))

    @task
    def ready(ctx):
        """Verify authenticated SSH without changing the native install task."""
        from benchmark.remote import Bench
        bench = Bench(ctx)
        wait_for_ssh(bench.manager.hosts(flat=True), bench.connect)

    if PROTOCOL != 'Orca-A':
        @task
        def install(ctx):
            """Install on this batch's instances; propagate install failures."""
            from benchmark.remote import Bench
            Bench(ctx).install()
    # Orca-A keeps the exact native.install Task exported above. Its native
    # failure handler prints and returns; lifecycle checks its success output.

    @task
    def destroy(ctx):
        """Terminate this private pool and remove its private security groups."""
        manager = InstanceManager.make()
        problem = None
        try:
            manager.terminate_instances()
        except Exception as error:
            problem = error
        rows, groups = legacy_inventory(manager)
        if rows:
            raise RuntimeError(f'{len(rows)} instances remain in {SCOPE}: {problem}')
        # Legacy destroy returns early when creation made zero instances, and
        # can race with ENI deletion. Finish only this scope's remaining groups.
        for group in groups:
            client = manager.clients[group['region']]
            for attempt in range(12):
                try:
                    client.delete_security_group(GroupId=group['group_id'])
                    break
                except Exception as error:
                    code = getattr(error, 'response', {}).get('Error', {}).get('Code')
                    if code == 'InvalidGroup.NotFound':
                        break
                    if code != 'DependencyViolation' or attempt == 11:
                        raise
                    print(f'Waiting for ENI cleanup: {group["group_id"]}', flush=True)
                    time.sleep(5)
