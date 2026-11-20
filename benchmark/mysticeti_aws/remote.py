"""Install a resolved source commit; run Mysticeti experiments without rebuilding."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid

from fabric import Connection
from paramiko import PKey
from paramiko.ssh_exception import AuthenticationException
import yaml

from .build import verification_script, verify_build
from .cloud import Cloud
from .metrics import parse_snapshot, summarize
from .report import format_summary
from .settings import ADVERSARY_DEFAULTS, load_allocation, validate_benchmark
from .source import resolve_source
from .timing import make_plan, validate_receipts, summarize_windows

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def generate_config(binary, ips, output, remote_directory, benchmark):
    benchmark = validate_benchmark(benchmark)
    leaders, leader_timeout_ms = benchmark["leaders"], benchmark["leader_timeout_ms"]
    output = Path(output)
    subprocess.run([str(binary), 'benchmark-genesis', '--ips', *ips,
                    '--working-directory', str(output), '--number-of-leaders', str(leaders)], check=True)
    parameters_path = output / 'parameters.yaml'
    parameters = yaml.safe_load(parameters_path.read_text())
    if benchmark['adversary']['mode'] != 'A0' and not set(ADVERSARY_DEFAULTS).issubset(parameters.get('adversary', {})):
        raise RuntimeError('Binary genesis lacks adversary support. Run fab install first.')
    parameters['adversary'] = benchmark['adversary']
    parameters['wave_length'] = benchmark['wave_length']
    parameters['enable_pipelining'] = True
    parameters['leader_timeout'] = {'secs': leader_timeout_ms // 1000,
                                    'nanos': (leader_timeout_ms % 1000) * 1_000_000}
    parameters_path.write_text(yaml.safe_dump(parameters, sort_keys=False), encoding='utf-8')
    for index in range(len(ips)):
        path = output / 'private' / f'{index}.yaml'
        private = yaml.safe_load(path.read_text())
        private['storage_path'] = f'{remote_directory}/private/val-{index}'
        path.write_text(yaml.safe_dump(private, sort_keys=False), encoding='utf-8')


class Runner:
    def __init__(self, settings, cloud=None, results_dir=None):
        self.settings = settings
        self._cloud = cloud
        self.provenance = {}
        self.source = Path(settings['remote']['source_dir'])
        self.binary = self.source / 'target/release/mysticeti'
        self.results_dir = Path(results_dir or PACKAGE_DIR / 'results')
        self.socket = 'mysticeti-' + hashlib.sha256(settings['testbed_id'].encode()).hexdigest()[:12]
        self._key = None

    @property
    def cloud(self):
        if self._cloud is None:
            self._cloud = Cloud(self.settings)
        return self._cloud

    def _resolve_source(self, *, for_install=False):
        checkout = PACKAGE_DIR.parent.resolve()
        source = self.source.resolve()
        if source in checkout.parents or checkout in source.parents:
            raise ValueError('remote.source_dir must equal the controller checkout or be a separate directory')
        repository = dict(self.settings['repo'])
        if source == checkout:
            # The normal workflow needs no ref edits between installing and running:
            # install builds this checkout; remote uses the last recorded build.
            if for_install:
                repository['ref'] = 'HEAD'
            else:
                try:
                    record = json.loads((self.source / '.mysticeti-build.json').read_text(encoding='utf-8'))
                    commit = record['commit']
                    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
                        raise ValueError('invalid installed commit')
                    if record.get('repository') != repository['url']:
                        raise ValueError('installed repository does not match repo.url')
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise RuntimeError(f'Invalid build at {self.source}: {error}. Run fab install first.') from error
                repository['ref'] = commit
        return resolve_source(checkout, repository)

    def _ssh(self, host):
        if self._key is None:
            self._key = PKey.from_path(Path(self.settings['key']['path']).expanduser())
        return Connection(host.ip, user='ubuntu', connect_timeout=15, connect_kwargs={
            'pkey': self._key, 'allow_agent': False, 'look_for_keys': False,
            'banner_timeout': 30, 'auth_timeout': 30,
        })

    def _parallel(self, function, items):
        items = list(items)
        if not items:
            return []
        output, errors = {}, []
        with ThreadPoolExecutor(max_workers=min(len(items), self.settings['remote']['parallelism'])) as pool:
            futures = {pool.submit(function, item): index for index, item in enumerate(items)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    output[index] = future.result()
                except Exception as error:
                    errors.append(f'{items[index]}: {error}')
        if errors:
            raise RuntimeError('\n'.join(errors))
        return [output[index] for index in range(len(items))]

    def _wait_ssh(self, host):
        deadline = time.monotonic() + 240
        while True:
            try:
                with self._ssh(host) as connection:
                    connection.run('true', hide=True, timeout=15)
                return
            except AuthenticationException:
                raise
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def install(self):
        self.provenance = self._resolve_source(for_install=True)
        hosts = self.cloud.hosts()
        if not hosts:
            raise RuntimeError('No Mysticeti instances found. Run fab info; create or start the testbed.')
        scripts = PACKAGE_DIR / 'scripts'
        arguments = [str(self.source), self.provenance['commit'],
                     str(self.settings['remote']['build_jobs']), self.provenance['repository']]
        print(f'Installing and compiling commit {self.provenance["commit"]} on the controller (output is visible)...', flush=True)
        subprocess.run(['bash', str(scripts / 'install_node.sh'), *arguments], check=True)
        verify_build(self.source, self.provenance)
        self._parallel(self._wait_ssh, hosts)
        self.cloud.sync_network()

        def install_host(host):
            remote_tmp = '/tmp/mysticeti-install-' + uuid.uuid4().hex
            print(f'[{host.region} {host.ip}] Installing and compiling...', flush=True)
            with self._ssh(host) as connection:
                connection.run(f'mkdir -m 700 {shlex.quote(remote_tmp)}', hide=True)
                try:
                    for name in ('install_node.sh', 'build_node.sh', 'sync_source.sh', 'record_build.py'):
                        connection.put(str(scripts / name), remote=f'{remote_tmp}/{name}')
                    command = shlex.join(['bash', f'{remote_tmp}/install_node.sh', *arguments])
                    connection.run(command, hide=False, pty=False)
                    result = connection.run(verification_script(str(self.source), self.provenance), hide=True)
                    return {'host': host.ip, 'build': json.loads(result.stdout)}
                finally:
                    connection.run(f'rm -f -- {shlex.quote(remote_tmp + "/install_node.sh")} '
                                   f'{shlex.quote(remote_tmp + "/build_node.sh")} '
                                   f'{shlex.quote(remote_tmp + "/sync_source.sh")} '
                                   f'{shlex.quote(remote_tmp + "/record_build.py")} && '
                                   f'rmdir -- {shlex.quote(remote_tmp)}', hide=True, warn=True)
        records = self._parallel(install_host, hosts)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        save_json(self.results_dir / 'installation.json', {
            **self.provenance, 'controller': verify_build(self.source, self.provenance), 'nodes': records,
        })
        print(f'Built commit {self.provenance["commit"]} on controller and {len(hosts)} nodes; ready for fab remote.', flush=True)

    def remote(self, parameters):
        parameters = validate_benchmark(parameters)
        if not (self.source / '.mysticeti-build.json').is_file():
            verify_build(self.source)
        self.provenance = self._resolve_source()
        self.controller_build = verify_build(self.source, self.provenance)
        print(f'Using installed Mysticeti commit {self.provenance["commit"]}.', flush=True)
        hosts = self.cloud.hosts()
        if len(hosts) < parameters['nodes']:
            raise RuntimeError(f'Need {parameters["nodes"]} running Mysticeti instances; found {len(hosts)}')
        hosts = hosts[:parameters['nodes']]
        self.cloud.sync_network()

        def preflight(host):
            with self._ssh(host) as connection:
                result = connection.run(verification_script(str(self.source), self.provenance), hide=True)
                connection.run('command -v tmux >/dev/null && chronyc waitsync 1 0.01 0.0 1', hide=True, timeout=15)
                return {'host': host.ip, 'build': json.loads(result.stdout)}
        self.installed = self._parallel(preflight, hosts)
        for repetition in range(parameters['runs']):
            print(f'Experiment {repetition + 1}/{parameters["runs"]}', flush=True)
            self._run_once(hosts, parameters)

    def _stop(self, hosts):
        def stop_host(host):
            try:
                with self._ssh(host) as connection:
                    connection.run(f'tmux -L {self.socket} kill-server 2>/dev/null || true', hide=True, timeout=15)
            except Exception as error:
                return f'{host.ip}: {error}'
            return None
        return [error for error in self._parallel(stop_host, hosts) if error]

    def kill(self):
        errors = self._stop(self.cloud.hosts())
        if errors:
            raise RuntimeError('Could not stop every node: ' + '\n'.join(errors))
        print('Stopped Mysticeti experiment processes; EC2 instances remain running.')

    def _configure_and_start(self, hosts, parameters, directory, remote_directory):
        genesis = directory / 'genesis'
        generate_config(self.binary, [host.ip for host in hosts], genesis, remote_directory,
                        parameters)
        allocation = load_allocation(parameters['rate'], len(hosts), parameters['faults'])
        active = list(enumerate(hosts[:len(hosts) - parameters['faults']]))

        def launch(item):
            index, host = item
            storage = f'{remote_directory}/private/val-{index}'
            with self._ssh(host) as connection:
                connection.run(f'mkdir -p -- {shlex.quote(storage)}', hide=True)
                for filename in ('committee.yaml', 'parameters.yaml', f'private/{index}.yaml'):
                    connection.put(str(genesis / filename), remote=f'{remote_directory}/{filename}')
                node_args = [str(self.binary), 'run', '--authority', str(index),
                             '--committee-path', f'{remote_directory}/committee.yaml',
                             '--parameters-path', f'{remote_directory}/parameters.yaml',
                             '--private-config-path', f'{remote_directory}/private/{index}.yaml']
                # This prototype's BlockFetcherWorker returns early when USE_SYNCER
                # exists (even "0"). Remove it to enable missing-block fetching.
                syncer_env = 'unset USE_SYNCER' if parameters['enable_synchronizer'] else 'export USE_SYNCER=1'
                script = '\n'.join([
                    '#!/usr/bin/env bash', 'set -euo pipefail',
                    f'export TPS={allocation["per_node_tps"]}', f'export TRANSACTION_SIZE={parameters["tx_size"]}',
                    'export CONSENSUS_ONLY=1', syncer_env, 'export INITIAL_DELAY=0', 'export RUST_LOG=info',
                    f'export BENCHMARK_CONTROL_DIR={shlex.quote(remote_directory)}',
                    f'export BENCHMARK_RUN_ID={shlex.quote(directory.name)}',
                    f'exec {shlex.join(node_args)} > {shlex.quote(remote_directory + "/node.log")} 2>&1', '',
                ])
                connection.put(io.BytesIO(script.encode()), remote=f'{remote_directory}/start.sh')
        self._parallel(launch, active)

        def start_node(item):
            index, host = item
            with self._ssh(host) as connection:
                command = shlex.join(['bash', f'{remote_directory}/start.sh'])
                connection.run(f'tmux -L {self.socket} new-session -d -s node-{index} {shlex.quote(command)}', hide=True)
        self.launch_started = time.monotonic()
        self._parallel(start_node, active)
        self.launch_completed = time.monotonic()

    def _collect(self, active, committee_size):
        def collect(item):
            index, host = item
            with self._ssh(host) as connection:
                connection.open()
                start = time.monotonic()
                result = connection.run(
                    f'tmux -L {self.socket} has-session -t node-{index} && '
                    f'curl -fsS --max-time 10 http://127.0.0.1:{1500 + committee_size + index}/metrics',
                    hide=True, timeout=20)
                end = time.monotonic()
                snapshot = parse_snapshot(result.stdout)
                if not re.search(r'^benchmark_ready\s+1(?:\.0)?\s*$', result.stdout, re.M):
                    raise ValueError('Validator runtime is not ready')
                return str(index), {'metrics': snapshot, 'monotonic': (start + end) / 2,
                                    'request_seconds': end - start, 'text': result.stdout}
        return dict(self._parallel(collect, active))

    def _record(self, directory, name, samples):
        snapshots = directory / 'metrics'
        snapshots.mkdir(exist_ok=True)
        record = {}
        for index, sample in samples.items():
            (snapshots / f'{name}-node-{index}.prom').write_text(sample['text'], encoding='utf-8')
            record[index] = {key: value for key, value in sample.items() if key != 'text'}
        with (directory / 'samples.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'phase': name, 'nodes': record}) + '\n')

    def _wait_ready(self, active, committee_size):
        deadline = time.monotonic() + 120
        while True:
            try:
                return self._collect(active, committee_size)
            except Exception as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Validators did not expose shared metrics within 120s: {error}') from error
                time.sleep(2)

    def _download_logs(self, active, directory, remote_directory):
        (directory / 'logs').mkdir(exist_ok=True)
        def download(item):
            index, host = item
            try:
                with self._ssh(host) as connection:
                    connection.get(f'{remote_directory}/node.log', local=str(directory / 'logs' / f'node-{index}.log'))
            except Exception as error:
                return f'{host.ip}: {error}'
            return None
        return [error for error in self._parallel(download, active) if error]

    def _read_control(self, active, remote_directory, filename):
        def read(item):
            index, host = item
            with self._ssh(host) as connection:
                command = (f'tmux -L {self.socket} has-session -t node-{index} && '
                           f'cat -- {shlex.quote(remote_directory + "/" + filename)}')
                result = connection.run(command, hide=True, timeout=15)
                return str(index), yaml.safe_load(result.stdout)
        return dict(self._parallel(read, active))

    def _measure_window(self, active, committee_size, parameters, directory, remote_directory, record):
        subprocess.run(['chronyc', 'waitsync', '1', '0.01', '0.0', '1'], check=True)
        plan = make_plan(record['run_id'], int(time.time() * 1000) + 30000,
                         parameters['warmup'], parameters['duration'])
        record['timeline'] = plan
        record['timing_limits'] = dict(lead_seconds=30, arm_guard_seconds=5,
                                      max_input_lateness_ms=500, final_timeout_seconds=30)
        save_json(directory / 'run.json', record)
        expected = [str(index) for index, _ in active]
        arm_deadline = time.monotonic() + (plan['load_ms'] / 1000 - time.time()) - 5
        end_deadline = time.monotonic() + (plan['end_ms'] / 1000 - time.time())

        def arm(item):
            _, host = item
            with self._ssh(host) as connection:
                temp = remote_directory + '/start.yaml.tmp'
                connection.put(io.BytesIO(yaml.safe_dump(plan).encode()), remote=temp)
                connection.run(shlex.join(['mv', '--', temp, remote_directory + '/start.yaml']), hide=True, timeout=15)
        self._parallel(arm, active)
        while True:
            try:
                receipts = self._read_control(active, remote_directory, 'armed.yaml')
                validate_receipts(plan, receipts, expected, int(time.time() * 1000))
                if time.monotonic() >= arm_deadline:
                    raise ValueError('Arm deadline expired')
                break
            except Exception:
                if time.monotonic() >= arm_deadline:
                    raise
                time.sleep(0.2)
        save_json(directory / 'armed.json', receipts)
        sequence = 0
        while time.monotonic() < end_deadline:
            time.sleep(min(parameters['scrape_interval'], max(0, end_deadline - time.monotonic())))
            samples = self._collect(active, committee_size)
            self._record(directory, f'sample-{sequence:04}', samples)
            sequence += 1
        while True:
            try:
                windows = self._read_control(active, remote_directory, 'window.yaml')
                break
            except Exception:
                if time.monotonic() >= end_deadline + 30:
                    raise
                time.sleep(0.2)
        save_json(directory / 'windows.json', windows)
        return summarize_windows(plan, windows, expected)

    def _run_once(self, hosts, parameters):
        parameters = validate_benchmark(parameters)
        run_id = datetime.now(timezone.utc).strftime('run-%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        directory = self.results_dir / run_id
        directory.mkdir(parents=True, exist_ok=False)
        remote_directory = self.settings['remote']['run_dir'] + '/' + run_id
        active = list(enumerate(hosts[:len(hosts) - parameters['faults']]))
        allocation = load_allocation(parameters['rate'], len(hosts), parameters['faults'])
        record = {'run_id': run_id, 'commit': self.provenance.get('commit'), 'source': self.provenance, 'status': 'starting', 'parameters': parameters,
                  'timing_schema': 2,
                  'load': allocation, 'builds': getattr(self, 'installed', []),
                  'controller_build': getattr(self, 'controller_build', None),
                  'adversary': parameters['adversary'],
                  'crashed_authorities': list(range(len(active), len(hosts))),
                  'enable_synchronizer': parameters['enable_synchronizer'],
                  'leader_schedule': parameters['leader_schedule'], 'enable_pipelining': True,
                  'hosts': [dict(authority=i, ip=h.ip, region=h.region, instance_id=h.instance_id,
                                 started=i < len(active), byzantine=i in parameters['adversary']['byzantine_authorities'])
                            for i, h in enumerate(hosts)]}
        save_json(directory / 'run.json', record)
        print(f'Results: {directory}\nTarget load: {allocation}; crashed tail members are not started.', flush=True)
        problem = None
        summary = None
        try:
            previous_errors = self._stop(hosts)
            if previous_errors:
                raise RuntimeError('Could not stop a previous experiment: ' + '\n'.join(previous_errors))
            self._configure_and_start(hosts, parameters, directory, remote_directory)
            self._wait_ready(active, len(hosts))
            print('All active nodes ready; scheduling a common measurement window.', flush=True)
            summary = self._measure_window(active, len(hosts), parameters, directory, remote_directory, record)
            summary.update(load=allocation, commit=self.provenance.get('commit'), run_id=run_id,
                           adversary=parameters['adversary'], crashed_authorities=record['crashed_authorities'],
                           enable_synchronizer=parameters['enable_synchronizer'])
            save_json(directory / 'summary.json', summary)
            with (directory / 'summary.csv').open('w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(summary['nodes'][0]))
                writer.writeheader()
                writer.writerows(summary['nodes'])
            record['status'] = 'complete'
            # Print before SSH cleanup/log downloads, which can take a long time.
            preview = dict(record, status='measurement complete; cleanup/log download pending')
            text = format_summary(preview, summary, directory)
            print(text, flush=True)
            (directory / 'summary.txt').write_text(text, encoding='utf-8')
        except BaseException as error:
            record['status'] = 'failed'
            record['error'] = str(error) or type(error).__name__
            problem = error
        finally:
            if hasattr(self, 'launch_started'):
                record['launch_dispatch_seconds'] = getattr(self, 'launch_completed', time.monotonic()) - self.launch_started
            print('Stopping experiment nodes and downloading logs...', flush=True)
            stop_errors = self._stop(hosts)
            log_errors = self._download_logs(active, directory, remote_directory)
            record['cleanup_errors'] = stop_errors
            record['log_errors'] = log_errors
            if stop_errors or log_errors:
                record['status'] = 'failed'
                if problem is None:
                    problem = RuntimeError('Cleanup/log collection failed; see run.json')
            save_json(directory / 'run.json', record)
            text = format_summary(record, summary, directory)
            (directory / 'summary.txt').write_text(text, encoding='utf-8')
            if summary is None or problem is not None:
                print(text, flush=True)
            else:
                print(f'Final status: {record["status"]}. Summary saved: {directory / "summary.txt"}', flush=True)
        if problem is not None:
            raise problem
