"""Create -> install -> sequential remote/archive -> destroy, per protocol and size."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import uuid

TOOLS = Path(__file__).resolve().parent


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)


def case_key(case):
    return f"{case['protocol']}/{case['mode']}/n{case['nodes']}/r{case['input_rate_tps']}"


def regions(bench):
    result = read(Path(bench) / 'settings.json')['instances']['regions']
    result = [result] if isinstance(result, str) else result
    if not result or len(set(result)) != len(result):
        raise ValueError('Expected a nonempty unique region list')
    return result


def instances_per_region(nodes, configured_regions):
    """Native create allocates equally per region; never round up the pool."""
    count, remainder = divmod(nodes, len(configured_regions))
    if count < 1 or remainder:
        raise ValueError(f'Cannot create exactly {nodes} instances equally across '
                         f'{len(configured_regions)} regions. The node count must be divisible '
                         'by the region count; the full 10/20/35 plan supports 1 or 5 regions.')
    return count


class ProtocolLifecycle:
    def __init__(self, name, bench, output):
        self.name = name
        self.bench = Path(bench).resolve()
        self.root = Path(output).resolve() / 'lifecycle' / name
        self.state_path = self.root / 'state.json'
        self.shared = name in ('Mysticeti', 'Mahi-Mahi')
        self.record = None

    def _load(self):
        self.record = read(self.state_path)
        if self.record['protocol'] != self.name or self.record['bench'] != str(self.bench):
            raise ValueError('Lifecycle record does not match the selected protocol checkout')
        self.directory = self.root / self.record['scope']
        self.settings_path = self.directory / 'settings.json'

    @property
    def env(self):
        return dict(os.environ, FIGURE_PROTOCOL=self.name, FIGURE_SCOPE=self.record['scope'],
                    FIGURE_BENCH_DIR=str(self.bench), FIGURE_SETTINGS=str(self.settings_path))

    def wrap(self, command):
        if command[0] != 'fab':
            raise ValueError('Expected a Fabric task')
        return ['fab', '-r', str(TOOLS), '-c', 'protocol_tasks', *command[1:]]

    def _task(self, name, *arguments):
        command = ['fab', name, *map(str, arguments)]
        if self.shared and name != 'inventory':
            command += ['--settings', str(self.settings_path)]
        command = self.wrap(command)
        print(f'[{self.name}] {shlex.join(command)}', flush=True)
        expected_success = (f'Initialized testbed of {self.record["total_instances"]} nodes'
                            if self.name == 'Orca-A' and name == 'install' else None)
        saw_success = expected_success is None
        with (self.directory / f'{name}.log').open('a', encoding='utf-8') as log:
            log.write(f'\n{datetime.now(timezone.utc).isoformat()} {shlex.join(command)}\n')
            process = subprocess.Popen(command, cwd=self.bench, env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, errors='replace', bufsize=1, start_new_session=True)
            try:
                for line in process.stdout:
                    if expected_success and expected_success in line:
                        saw_success = True
                    print(line, end='', flush=True)
                    log.write(line)
                    log.flush()
                code = process.wait()
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                raise
            finally:
                process.stdout.close()
        if code:
            raise RuntimeError(f'{self.name}: {name} exited {code}; see {self.directory / (name + ".log")}')
        if not saw_success:
            raise RuntimeError(f'{self.name}: native install returned without its success message; '
                               f'see {self.directory / "install.log"}')

    def _state(self, status, **values):
        self.record.update(status=status, updated_utc=datetime.now(timezone.utc).isoformat(), **values)
        save(self.state_path, self.record)
        save(self.directory / 'state.json', self.record)

    def _inventory(self, label):
        path = self.directory / f'{label}.json'
        self._task('inventory', '--output', path)
        result = read(path)
        if result['scope'] != self.record['scope']:
            raise ValueError('Inventory scope mismatch')
        return result

    def recover(self):
        if not self.state_path.is_file():
            return
        self._load()
        if self.record['status'] != 'terminated':
            print(f'[{self.name}] Cleaning an unfinished lifecycle before proceeding.', flush=True)
            self.cleanup()

    def begin(self, nodes):
        configured_regions = regions(self.bench)
        per_region = instances_per_region(nodes, configured_regions)
        scope = f'fig-{self.name.lower()}-{uuid.uuid4().hex[:12]}'
        self.directory = self.root / scope
        self.directory.mkdir(parents=True)
        self.settings_path = self.directory / 'settings.json'
        settings = read(self.bench / 'settings.json')
        if self.shared:
            settings['testbed_id'] = scope
        key = settings.get('key', {})
        if key.get('path'):
            path = Path(key['path']).expanduser()
            key['path'] = str((self.bench / path).resolve() if not path.is_absolute() else path)
        save(self.settings_path, settings)
        self.record = dict(protocol=self.name, bench=str(self.bench), scope=scope,
                           regions=configured_regions, nodes_per_region=per_region,
                           committee_nodes=nodes, total_instances=nodes,
                           creation_started=False,
                           created_utc=datetime.now(timezone.utc).isoformat())
        self._state('planned')

    def create(self):
        initial = self._inventory('before-create')
        if initial['instances'] or initial['security_groups']:
            raise RuntimeError('Unexpected existing resources in the new batch scope')
        self._state('creating', creation_started=True)
        self._task('create', '--nodes', self.record['nodes_per_region'])
        for attempt in range(12):
            inventory = self._inventory('created-instances')
            rows = inventory['instances']
            ready = all(sum(r['region'] == region and r['state'] == 'running' and bool(r['ip'])
                            for r in rows) == self.record['nodes_per_region']
                        for region in self.record['regions'])
            if ready and len(rows) == self.record['total_instances']:
                self._state('created', instances=rows)
                return
            if attempt != 11:
                time.sleep(5)
        raise RuntimeError('Created pool is incomplete or not ready; cleanup will run')

    def install(self):
        if not self.shared:
            self._state('waiting_ssh')
            self._task('ready')
        self._state('installing')
        self._task('install')
        self._state('running')

    def failed(self, error):
        self._state('failed', experiment_error=f'{type(error).__name__}: {error}')

    def cleanup(self):
        if not self.record.get('creation_started'):
            self._state('terminated', cleanup_error=None, cleanup_note='No create attempted; no resources adopted or deleted')
            return
        self._state('destroying')
        try:
            self._task('destroy')
            inventory = self._inventory('after-destroy')
            if inventory['instances'] or inventory['security_groups']:
                raise RuntimeError('Batch resources remain after destroy')
        except BaseException as error:
            self._state('cleanup_failed', cleanup_error=f'{type(error).__name__}: {error}')
            raise
        self._state('terminated', cleanup_error=None)


def run_protocol(plan, bench, output, run_case, retry_failed=False):
    name = plan[0]['protocol']
    phase = ProtocolLifecycle(name, bench, output)
    # A process kill or host reboot may have interrupted the previous finally.
    # Recover recorded resources even when all experiment points are complete.
    phase.recover()
    pending = []
    for case in plan:
        selected = Path(output) / case_key(case) / 'selected.json'
        if selected.is_file():
            saved = read(selected)
            if saved['case'] != case:
                raise ValueError(f'Existing case differs: {selected}')
            if saved['status'] == 'complete':
                continue
            if not retry_failed:
                raise RuntimeError(f'Previous case failed: {selected}; use --retry-failed after inspection')
        pending.append(case)
    if not pending:
        print(f'SKIP completed protocol plan: {name}', flush=True)
        return
    sizes = sorted({case['nodes'] for case in pending})
    configured_regions = regions(bench)
    # Check every remaining size before creating any new billable resources.
    for nodes in sizes:
        instances_per_region(nodes, configured_regions)
    completed_here = 0
    for nodes in sizes:
        size_plan = [case for case in pending if case['nodes'] == nodes]
        phase.begin(nodes)
        print(f'[{name}] Creating exactly {nodes} instances for {len(size_plan)} pending cases.', flush=True)
        try:
            phase.create()
            phase.install()
            for index, case in enumerate(size_plan, 1):
                print(f'[{name} n={nodes} {index}/{len(size_plan)}; '
                      f'protocol {completed_here+1}/{len(pending)}]', flush=True)
                run_case(case, bench, output, retry_failed, lifecycle=phase)
                completed_here += 1
        except BaseException as error:
            phase.failed(error)
            raise
        finally:
            phase.cleanup()
