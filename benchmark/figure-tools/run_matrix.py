#!/usr/bin/env python3
"""Serial calls to existing fab remote tasks, with immutable attempt archives.

For each protocol and node count: create an exact-size private pool, install,
run and archive pending cases, then destroy it. Each case runs once for 60 measured
seconds. Failures stop the batch after cleanup. No repository edits are made.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shlex
import shutil
import signal
from statistics import fmean
import subprocess
import sys
import time

PROTOCOLS = {
    'Orca-A': 'Orca-A/benchmark',
    'Orca-B': 'Orca-B-grbc/benchmark',
    'Bullshark': 'narwhal-bullshark-ubuntu24/benchmark',
    'Mysticeti': 'mysticeti/benchmark',
    'Mahi-Mahi': 'mahi-mahi-ubuntu24/aws',
}
MODES = ('A0', 'A1', 'A3', 'A5')
NODES = (10, 20, 35)
RATES = (10000, 30000, 50000, 70000, 90000, 110000, 130000, 150000)
NATIVE_MODES = dict(A0='none', A1='silent', A3='targeted', A5='network')
DURATION = 60
HEADER_SIZE_BYTES = 20
ATTACK_START_MS = 20000
ATTACK_DURATION_MS = 30000
DEFAULT_OUTPUT = '/home/ubuntu/experiments/paper-results-v3.5-mysticeti-clock1'
SEED = 7
# Normalized source hashes for the reviewed A1 launch/parse implementation and
# Orca-B's A5-only ABA policy. Checked before any instance creation.
NATIVE_SOURCE_SHA256 = {'Orca-A': {'benchmark/benchmark/adversary.py': '8abc12f8710a8336e35efebb5467d0baa24469a24ad7de68cf05fbf920238743',
            'benchmark/benchmark/remote.py': '5c6e52bc23b5cfe1497ba074af4742e7faa19413edf5786eaf9c4f308142173e',
            'benchmark/benchmark/local.py': '0b24cb8b23ba1a0751f695edba7150c0248a7f3c71967274312545adc4c0f5a2',
            'benchmark/benchmark/logs.py': '55091972ee7369cfc9e55ad30a03ef00d1a510409d5a9c9738e27eca065f1a9d'},
 'Orca-B': {'benchmark/benchmark/adversary.py': '8abc12f8710a8336e35efebb5467d0baa24469a24ad7de68cf05fbf920238743',
            'benchmark/benchmark/remote.py': '232a7ba90562937cb2d06824703749203926c1362af48e5bc8af91b44662da42',
            'benchmark/benchmark/local.py': '6c62b3d42f56e1e3d11c458e3ef0350a897fc0f5b16ce9f03924515db17f34df',
            'benchmark/benchmark/logs.py': 'fbce810e680cc0c749caf6b49253913e00fab4d173f19c1419fd08d82e9b17b3',
            'primary/src/adversary.rs': 'c1f9e427959f667543656c602ac8262f6351428bcccdc8ac51e8e01224fd6030'},
 'Bullshark': {'benchmark/benchmark/adversary.py': '8abc12f8710a8336e35efebb5467d0baa24469a24ad7de68cf05fbf920238743',
               'benchmark/benchmark/remote.py': 'ca0ecee7f73558616689ef6bf6035911f5530769ee3eb15672231683c779de20',
               'benchmark/benchmark/local.py': '685bab741e12b52320c2bf7618c744d895865adab5ae3a3453e2df92941636ac',
               'benchmark/benchmark/logs.py': '4b6af52b0ca4641ad43d7267c9f96fb87239721f330bdd213a17096185161b12'}}


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def nearby_rate(target, nodes):
    # Common quantum: legacy clients use 20 bursts/s; shared generators use 10.
    quantum = 20 * nodes
    return ((target + quantum // 2) // quantum) * quantum


def cases(protocol='all', mode='all'):
    for name in PROTOCOLS if protocol == 'all' else [protocol]:
        for nodes in NODES:
            for attack in MODES if mode == 'all' else [mode]:
                for target in RATES:
                    crash = name in ('Orca-A', 'Orca-B', 'Bullshark') and attack == 'A1'
                    byzantine = 0 if attack == 'A0' else (nodes - 1) // 3
                    rate = nearby_rate(target, nodes)
                    yield dict(protocol=name, mode=attack, nodes=nodes,
                               nominal_rate_tps=target, input_rate_tps=rate,
                               offered_rate_tps=rate // nodes * (nodes - byzantine) if crash else rate,
                               byzantine=byzantine,
                               byzantine_ids=list(range(nodes - byzantine, nodes)) if crash else list(range(byzantine)),
                               fault_model='crash-stop' if crash else ('none' if attack == 'A0' else 'runtime'),
                               aba_scheduling='A5-only' if name == 'Orca-B' else 'not-applicable',
                               repetition=1, duration_s=DURATION, warmup_s=0, seed=SEED,
                               header_size_bytes=None if name in ('Mysticeti', 'Mahi-Mahi') else HEADER_SIZE_BYTES,
                               attack_start_after_ms=ATTACK_START_MS if name in ('Mysticeti', 'Mahi-Mahi') else None,
                               attack_duration_ms=ATTACK_DURATION_MS if name in ('Mysticeti', 'Mahi-Mahi') else None)


def case_key(case):
    return f"{case['protocol']}/{case['mode']}/n{case['nodes']}/r{case['input_rate_tps']}"


def shared(case):
    return case['protocol'] in ('Mysticeti', 'Mahi-Mahi')


def command(case, settings_path=None):
    result = ['fab', 'remote', '--nodes', str(case['nodes']), '--rate',
              str(case['input_rate_tps']), '--tx-size', '512', '--duration',
              str(case['duration_s']), '--runs', '1', '--faults', '0', '--adversary', case['mode']]
    if case['byzantine']:
        result += ['--byzantine', str(case['byzantine'])]
    if shared(case):
        result += ['--warmup', '0', '--scrape-interval', '5', '--settings', str(settings_path)]
    else:
        result += ['--workers', '1', '--seed', str(SEED), '--start-round', '1',
                   '--attack-rounds', '0', '--delay-ms', '200',
                   '--max-pending-messages', '4096', '--max-pending-bytes', '67108864']
        if case['protocol'] == 'Bullshark':
            result += ['--bullshark-alternating', 'true' if case['mode'] in ('A3', 'A5') else 'false']
    return result


def effective_settings(source, case):
    settings = read(source)
    # Use a separate settings file outside the Git checkout. Infrastructure
    # configuration is retained, while experimental variables are explicit.
    key = settings.get('key', {})
    if key.get('path'):
        path = Path(key['path']).expanduser()
        key['path'] = str((source.parent / path).resolve() if not path.is_absolute() else path)
    settings.setdefault('benchmark', {}).update(
        nodes=case['nodes'], rate=case['input_rate_tps'], duration=case['duration_s'],
        warmup=0, runs=1, faults=0, tx_size=512, scrape_interval=5,
        enable_synchronizer=True, leader_schedule='round_robin',
        leaders=4 if case['protocol'] == 'Mahi-Mahi' else 2,
        wave_length=5 if case['protocol'] == 'Mahi-Mahi' else 3,
        leader_timeout_ms=250 if case['protocol'] == 'Mahi-Mahi' else 1000,
        adversary=dict(mode=case['mode'], byzantine_authorities=list(range(case['byzantine'])),
                       seed=SEED, start_after_ms=case['attack_start_after_ms'],
                       duration_ms=case['attack_duration_ms'],
                       delay_ms=200, late_delay_ms=1000, max_pending_messages=4096,
                       max_pending_bytes=67108864, max_extra_bytes_per_round=8388608,
                       target_authority=None, target_round=None))
    return settings


def load_module(path):
    spec = importlib.util.spec_from_file_location('saved_measurement_parser', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest_files(directory, pattern):
    return {p.name: sha(p) for p in directory.glob(pattern) if p.is_file()}


def summarize_replicas(rows):
    honest = [r for r in rows if not r['byzantine']]
    positive = [r for r in honest if r['latency_ms'] is not None]
    return dict(latency_ms=fmean(r['latency_ms'] for r in positive) if positive else None,
                latency_contributors=len(positive), honest_replicas=len(honest),
                zero_commit_replicas=[r['authority'] for r in honest if r['completed'] == 0],
                replica_aggregation='unweighted mean of honest positive-observation replica means',
                replicas=rows)


def legacy_measurement(archive, case):
    logs = archive / 'logs'
    clients = sorted(logs.glob('client-*.log'))
    workers = sorted(logs.glob('worker-*.log'))
    primaries = sorted(logs.glob('primary-*.log'))
    crashed = set(case['byzantine_ids']) if case['fault_model'] == 'crash-stop' else set()
    active = sorted(set(range(case['nodes'])) - crashed)
    required = len(active)
    if not (len(clients) == len(workers) == len(primaries) == required):
        raise ValueError('Incomplete log set (expected one client, worker and primary per active node)')
    if [p.name.removeprefix('client-') for p in clients] != [p.name.removeprefix('worker-') for p in workers]:
        raise ValueError('Client/worker log identities do not match')
    if sorted(int(p.stem.split('-')[1]) for p in primaries) != active:
        raise ValueError('Primary identities do not match the active committee')
    if sorted((int(p.stem.split('-')[1]), int(p.stem.split('-')[2])) for p in clients) != [(i, 0) for i in active]:
        raise ValueError('Client identities do not match the active committee')
    texts = [[p.read_text(encoding='utf-8', errors='replace') for p in paths]
             for paths in (clients, primaries, workers)]
    if any(re.search(r'panicked at|thread .* panicked', t) for group in texts for t in group):
        raise ValueError('A process panicked; inspect archived logs')
    measure = load_module(archive / 'measurement_parser.py').Measurements(*texts)
    measure.replica_logs = [p.name for p in primaries]
    measure.source_logs = [dict(client=c.name, worker=w.name) for c, w in zip(clients, workers)]
    metrics = measure.metrics(duration=case['duration_s'], warmup=0, drain=0)
    save(archive / 'measurement_stats.json', metrics)
    if metrics['status'] != 'available':
        raise ValueError('Release/ingress instrumentation is incomplete; latency is not comparable')
    rows = []
    for path, row in zip(primaries, metrics['replicas']):
        if row['invalid_time_samples'] or row['missing_ingress_samples']:
            raise ValueError('Invalid clock ordering or missing ingress: inspect measurement_stats.json')
        ident = int(path.stem.split('-')[1])
        rows.append(dict(authority=ident, byzantine=ident in case['byzantine_ids'],
                         completed=row['completed_samples'], released=row['released_samples'],
                         unfinished=row['unfinished_samples'],
                         completion_ratio=row['completion_ratio'],
                         latency_ms=row['release_to_final']['mean_ms'],
                         latency_observations=row['release_to_final']['count']))
    result = summarize_replicas(rows)
    result.update(metric='release_to_final_ms', completion_population='sampled transactions per replica',
                  window_basis=f"release cohort [first client Start, Start + {case['duration_s']}s); final commit by cutoff",
                  zero_commit_meaning='no completed sampled transaction (not proof of no commits)',
                  attack_window=('entire run; crashed processes are not started' if crashed else
                                 ('round 1 onward' if case['mode'] != 'A0' else 'disabled')),
                  crashed_primary_ids=sorted(crashed),
                  bullshark_alternating=case['protocol'] == 'Bullshark' and case['mode'] in ('A3', 'A5'))
    return result



def fixed_window_summary(archive, record, case):
    if case['protocol'] != 'Mysticeti':
        raise ValueError('Timing schema 2 is only supported for Mysticeti')
    plan = record.get('timeline')
    if not isinstance(plan, dict):
        raise ValueError('Missing fixed timeline')
    run_id, load = record.get('run_id'), plan.get('load_ms')
    if not isinstance(run_id, str) or not run_id or type(load) is not int or load < 0:
        raise ValueError('Invalid fixed timeline identity')
    expected_plan = dict(schema=2, run_id=run_id, load_ms=load,
                         start_ms=load, end_ms=load + case['duration_s'] * 1000)
    if (set(plan) != set(expected_plan) or expected_plan['end_ms'] >= 2**53
            or any(type(plan[k]) is not type(v) or plan[k] != v
                   for k, v in expected_plan.items())):
        raise ValueError('Timeline differs from the requested zero-warmup window')
    windows = read(archive / 'native/windows.json')
    receipts = read(archive / 'native/armed.json')
    expected_ids = {str(i) for i in range(case['nodes'])}
    for records in (windows, receipts):
        if not isinstance(records, dict) or set(records) != expected_ids:
            raise ValueError('Missing or unexpected fixed-window authority')
        for key, row in records.items():
            if (not isinstance(row, dict) or type(row.get('authority')) is not int
                    or row['authority'] != int(key)
                    or any(type(row.get(k)) is not type(v) or row[k] != v
                           for k, v in expected_plan.items())):
                raise ValueError('Fixed-window authority, run or timeline mismatch')
    before, after, elapsed = {}, {}, {}
    for key, row in windows.items():
        count, total, late = row.get('count'), row.get('sum_s'), row.get('input_lateness_ms')
        if row.get('complete') is not True or type(count) is not int or count < 0:
            raise ValueError('Incomplete or invalid fixed-window transaction count')
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise ValueError('Invalid fixed-window E2E sum')
        if type(late) not in (int, float) or not math.isfinite(late) or not 0 <= late <= 500:
            raise ValueError('Input start is absent or more than 500ms late')
        before[key] = dict(count=0, sum=0)
        after[key] = dict(count=count, sum=total)
        elapsed[key] = case['duration_s']
    return load_module(archive / 'measurement_parser.py').summarize(before, after, elapsed)


def shared_measurement(archive, case):
    native = archive / 'native'
    record = read(native / 'run.json')
    if record['status'] != 'complete' or record.get('log_errors') or record.get('cleanup_errors'):
        raise ValueError('Native run is incomplete or log collection/cleanup failed')
    params = record['parameters']
    for key, expected in [('nodes', case['nodes']), ('rate', case['input_rate_tps']),
                          ('duration', case['duration_s']), ('warmup', 0), ('faults', 0), ('runs', 1)]:
        if params.get(key) != expected:
            raise ValueError(f'Native run parameter mismatch: {key}')
    attack = record['adversary']
    if attack['mode'] != case['mode'] or sorted(attack['byzantine_authorities']) != list(range(case['byzantine'])):
        raise ValueError('Native adversary differs from requested case')
    if (attack.get('start_after_ms') != case['attack_start_after_ms']
            or attack.get('duration_ms') != case['attack_duration_ms']):
        raise ValueError('Native adversary window differs from requested case')
    schema = record.get('timing_schema', 1)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError('Unsupported timing schema')
    if schema == 2:
        summary = fixed_window_summary(archive, record, case)
    else:
        windows = {}
        for line in (native / 'samples.jsonl').read_text(encoding='utf-8').splitlines():
            sample = json.loads(line)
            if sample['phase'] in ('before', 'after'):
                if sample['phase'] in windows:
                    raise ValueError('Multiple full-run windows')
                windows[sample['phase']] = sample['nodes']
        if set(windows) != {'before', 'after'}:
            raise ValueError('No complete before/after window')
        before, after = windows['before'], windows['after']
        if set(before) != {str(i) for i in range(case['nodes'])} or set(after) != set(before):
            raise ValueError('Incomplete replica scrape set')
        summary = load_module(archive / 'measurement_parser.py').summarize(
            {i: s['metrics'] for i, s in before.items()},
            {i: s['metrics'] for i, s in after.items()},
            {i: after[i]['monotonic'] - s['monotonic'] for i, s in before.items()})
    save(archive / 'measurement_stats.json', summary)
    rows = [dict(authority=int(r['authority']), byzantine=int(r['authority']) < case['byzantine'],
                 completed=r['committed_transactions'], released=None, unfinished=None,
                 completion_ratio=None, latency_ms=r['mean_latency_ms'],
                 duration_s=r['duration_s'], tps=r['tps'], fork_output_ratio=r.get('fork_output_ratio'))
            for r in summary['nodes']]
    result = summarize_replicas(rows)
    result.update(metric='shared_commit_ms', source_commit=record.get('commit'), timing_schema=schema,
                  completion_population='committed transaction entries (fork payloads not deduplicated)',
                  window_basis=(f"node-local commit window [T0,T1); {case['duration_s']}s exactly" if schema == 2 else f"commit histogram delta after readiness; {case['duration_s']}s plus scrape overhead"),
                  zero_commit_meaning='no committed transaction entries in measurement window',
                  attack_window=(f"{case['attack_start_after_ms'] / 1000:g}s.."
                                 f"{(case['attack_start_after_ms'] + case['attack_duration_ms']) / 1000:g}s "
                                 + ('after common measurement start' if schema == 2 else 'after each node initializes')) if case['mode'] != 'A0' else 'disabled',
                  bullshark_alternating=False)
    return result


def copy_native(source, destination):
    destination.mkdir()
    # Keep reproducible public evidence. Genesis directories can contain keys.
    for name in ('run.json', 'samples.jsonl', 'summary.json', 'summary.csv', 'summary.txt', 'windows.json', 'armed.json'):
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    for name in ('logs', 'metrics'):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name)
    for name in ('committee.yaml', 'parameters.yaml', 'public-config.yaml', 'client-parameters.yaml'):
        for path in (source / name, source / 'genesis' / name):
            if path.is_file():
                shutil.copy2(path, destination / name)


def validate_legacy_snapshot(snapshot, case):
    if (snapshot.get('nodes') != case['nodes']
            or snapshot.get('rate') != case['input_rate_tps']
            or snapshot.get('adversary', {}).get('mode') != NATIVE_MODES[case['mode']]
            or snapshot.get('benchmark_parameters', {}).get('duration') != case['duration_s']
            or snapshot.get('node_parameters', {}).get('header_size') != case['header_size_bytes']):
        raise ValueError('Archived parameter snapshot does not match requested nodes/rate/mode/duration/header size')
    crashed = case['byzantine_ids'] if case['fault_model'] == 'crash-stop' else []
    active = sorted(set(range(case['nodes'])) - set(crashed))
    config = snapshot.get('adversary', {})
    if (snapshot.get('fault_model') != ('crash-stop' if crashed else 'runtime')
            or snapshot.get('active_launch_ids') != active
            or snapshot.get('inactive_launch_ids') != crashed
            or snapshot.get('offered_rate_tps') != case['offered_rate_tps']):
        raise ValueError('Archived parameter snapshot has a different process population or offered rate')
    if crashed and (config.get('fault_model') != 'crash-stop'
                    or config.get('byzantine_ids') != crashed
                    or config.get('bullshark_alternating') is not False):
        raise ValueError('Archived parameter snapshot is not the requested A1 crash model')
    if case['protocol'] == 'Orca-B' and snapshot.get('aba_scheduling') != 'A5-only':
        raise ValueError('Archived parameter snapshot does not record A5-only ABA scheduling')


def run_case(case, bench, output, retry_failed=False, lifecycle=None):
    directory = output / case_key(case)
    directory.mkdir(parents=True, exist_ok=True)
    selected = directory / 'selected.json'
    if selected.is_file():
        old = read(selected)
        if old['case'] != case:
            raise ValueError(f'Existing configuration differs: {selected}')
        if old['status'] == 'complete':
            print(f'SKIP completed: {case_key(case)}', flush=True)
            return
        if not retry_failed:
            raise RuntimeError(f'Previous attempt failed: {directory}. Inspect it, then use --retry-failed.')
    archive = directory / datetime.now(timezone.utc).strftime('attempt-%Y%m%dT%H%M%S.%fZ')
    archive.mkdir()
    meta = dict(case=case, status='running', attempt=str(archive.name), started_utc=datetime.now(timezone.utc).isoformat())
    save(selected, meta)
    save(archive / 'metadata.json', meta)
    native_before = {p.name for p in (bench / 'results').glob('run-*') if p.is_dir()}
    logs_before = digest_files(bench / 'logs', '*.log') if not shared(case) else {}
    results_before = digest_files(bench / 'results', '*') if not shared(case) else {}
    configuration = archive / 'effective-settings.json'
    if shared(case):
        source = lifecycle.settings_path if lifecycle else bench / 'settings.json'
        save(configuration, effective_settings(source, case))
    cmd = command(case, configuration)
    if lifecycle:
        cmd = lifecycle.wrap(cmd)
        meta['testbed_scope'] = lifecycle.record['scope']
    meta['command'] = shlex.join(cmd)
    meta['cwd'] = str(bench)
    meta['controller_commit'] = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=bench, text=True).strip()
    meta['controller_git_status'] = subprocess.check_output(
        ['git', 'status', '--porcelain'], cwd=bench, text=True)
    save(archive / 'metadata.json', meta)
    (archive / 'command.txt').write_text(f'cd {shlex.quote(str(bench))}\n{shlex.join(cmd)}\n', encoding='utf-8')
    print(f'RUN {case_key(case)}\n{shlex.join(cmd)}', flush=True)
    interrupted = False
    returncode = -1
    try:
        with (archive / 'console.log').open('w', encoding='utf-8') as log:
            process_options = dict(env=lifecycle.env) if lifecycle else {}
            process = subprocess.Popen(cmd, cwd=bench, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1,
                                       **process_options)
            try:
                for line in process.stdout:
                    print(line, end='', flush=True)
                    log.write(line)
                    log.flush()
                returncode = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                # Wait for the foreground Fabric process to exit before releasing
                # the global lock; never start another case concurrently.
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        meta['returncode'] = returncode
        if shared(case):
            runs = [p for p in (bench / 'results').glob('run-*') if p.is_dir() and p.name not in native_before]
            if len(runs) != 1:
                raise ValueError(f'Expected exactly one new native run directory; found {len(runs)}')
            copy_native(runs[0], archive / 'native')
            package = 'mysticeti_aws' if case['protocol'] == 'Mysticeti' else 'mahi_aws'
            shutil.copy2(bench / package / 'metrics.py', archive / 'measurement_parser.py')
            if returncode:
                raise ValueError(f'fab remote exited with {returncode}')
            measurement = shared_measurement(archive, case)
        else:
            if (bench / 'logs').is_dir():
                shutil.copytree(bench / 'logs', archive / 'logs')
            raw = archive / 'native-results'
            raw.mkdir()
            changed = []
            for name, digest in digest_files(bench / 'results', '*').items():
                if results_before.get(name) != digest:
                    shutil.copy2(bench / 'results' / name, raw / name)
                    changed.append(name)
            shutil.copy2(bench / 'benchmark' / 'measurements.py', archive / 'measurement_parser.py')
            fresh = digest_files(bench / 'logs', '*.log')
            if not fresh or any(logs_before.get(name) == digest for name, digest in fresh.items()):
                raise ValueError('Missing or unchanged logs; refusing to reuse stale results')
            snapshots = [read(raw / n) for n in changed if n.endswith('.parameters.json')]
            if len(snapshots) != 1:
                raise ValueError('Missing or ambiguous per-run parameters snapshot')
            snapshot = snapshots[0]
            validate_legacy_snapshot(snapshot, case)
            if returncode:
                raise ValueError(f'fab remote exited with {returncode}')
            measurement = legacy_measurement(archive, case)
        measurement.update(case=case, status='complete', archive=str(archive),
                           controller_commit=meta['controller_commit'])
        if lifecycle:
            measurement['testbed_scope'] = lifecycle.record['scope']
        save(archive / 'measurement.json', measurement)
        latency = measurement['latency_ms']
        lines = [case_key(case), f'Status: complete',
                 f'Fault model: {case["fault_model"]}',
                 f'Requested rate (tx/s): {case["input_rate_tps"]}',
                 f'Configured offered rate (tx/s): {case["offered_rate_tps"]}',
                 f'ABA scheduling: {case["aba_scheduling"]}',
                 f'Measured duration (s): {case["duration_s"]}',
                 f'Header size (B): {case["header_size_bytes"] if case["header_size_bytes"] is not None else "protocol default"}',
                 f'Adversary window: {measurement["attack_window"]}',
                 f'Latency metric: {measurement["metric"]}',
                 f'Honest replica mean latency (ms): {latency if latency is not None else "N/A"}',
                 f'Latency contributors: {measurement["latency_contributors"]}/{measurement["honest_replicas"]}',
                 f'Zero completed observations at replicas: {measurement["zero_commit_replicas"]}',
                 f'Archive: {archive}']
        (archive / 'summary.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        meta['status'] = 'complete'
    except BaseException as error:
        meta.update(status='interrupted' if interrupted else 'failed', error=str(error), returncode=returncode)
        # Save evidence already downloaded by the native task before the
        # lifecycle finally terminates the pool, including early interrupts.
        try:
            if shared(case) and not (archive / 'native').exists():
                candidates = [p for p in (bench / 'results').glob('run-*') if p.is_dir() and p.name not in native_before]
                if len(candidates) == 1:
                    copy_native(candidates[0], archive / 'native')
            elif not shared(case) and not (archive / 'logs').exists() and (bench / 'logs').is_dir():
                shutil.copytree(bench / 'logs', archive / 'logs')
                meta['logs_require_freshness_check'] = True
        except Exception as archive_error:
            meta['archive_error'] = str(archive_error)
        raise
    finally:
        meta['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save(archive / 'metadata.json', meta)
        save(selected, meta)
        files = {str(p.relative_to(archive)): sha(p) for p in sorted(archive.rglob('*')) if p.is_file() and p.name != 'SHA256.json'}
        save(archive / 'SHA256.json', files)


def plan_file(plan, output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'experiment-matrix.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plan[0]))
        writer.writeheader()
        writer.writerows(plan)


def require_prebuilt_remote(name, bench):
    """Reject old launch semantics or ABA policies before allocating instances."""
    for relative, expected in NATIVE_SOURCE_SHA256.get(name, {}).items():
        path = bench.parent / relative
        try:
            digest = hashlib.sha256(path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()
        except OSError as error:
            raise ValueError(f'{name}: missing source at {path}') from error
        if digest != expected:
            raise ValueError(f'{name}: {relative} differs from the v3.5 release; '
                             'update the controller with the v3.5 repository updater before running')


def upgrade_pool_policy(output, campaign, current):
    """Keep the CLI flag, but never relabel an old experiment as the current plan."""
    changed = [name for name in campaign if campaign[name] != current.get(name)]
    if changed:
        raise ValueError('v3.5 changes legacy A1 to process crashes and Orca-B ABA scheduling to A5 only. '
                         'Start a new --output directory; previous experiment results remain unchanged.')
    return {**campaign, **current}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', choices=['all', *PROTOCOLS], default='all')
    parser.add_argument('--mode', choices=['all', *MODES], default='all')
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/experiments'))
    parser.add_argument('--output', type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--upgrade-pool-policy', action='store_true',
                        help='Compatibility check only; changed experiment parameters require a new output directory')
    parser.add_argument('--cleanup-only', action='store_true', help='Destroy only recorded unfinished batch pools; never create or run')
    args = parser.parse_args()
    plan = list(cases(args.protocol, args.mode))
    output = args.output.resolve()
    print('Cleanup recorded batch pools only; no create/install/remote.' if args.cleanup_only else
          f'{len(plan)} configurations; one run each; {DURATION} measured seconds; warmup=0; strictly serial. '
          f'Orca-A/Orca-B/Bullshark header size: {HEADER_SIZE_BYTES} B. '
          f'Mysticeti/Mahi-Mahi adversary window: {ATTACK_START_MS / 1000:g}-'
          f'{(ATTACK_START_MS + ATTACK_DURATION_MS) / 1000:g} s; origin: schema 2 = common measurement start, schema 1 = node initialization.')
    if args.dry_run:
        plan_file(list(cases()), output)
        from lifecycle import instances_per_region, regions
        for name in dict.fromkeys(c['protocol'] for c in plan):
            bench = args.root / PROTOCOLS[name]
            protocol_plan = [c for c in plan if c['protocol'] == name]
            for nodes in sorted({case['nodes'] for case in protocol_plan}):
                size_plan = [case for case in protocol_plan if case['nodes'] == nodes]
                count = (instances_per_region(nodes, regions(bench))
                         if (bench / 'settings.json').is_file() else f'<{nodes}/configured-regions; exact division required>')
                print(f'\n[{name} n={nodes}] exactly {nodes} instances; private scope; cwd={bench}')
                print('# FIGURE_SCOPE and scoped settings are generated during execution; this plan does not create them.')
                prefix = ['fab', '-r', str(Path(__file__).resolve().parent), '-c', 'protocol_tasks']
                scope_settings = (['--settings', str(output / 'lifecycle' / name / f'SCOPE-n{nodes}/settings.json')]
                                  if name in ('Mysticeti', 'Mahi-Mahi') else [])
                print(shlex.join(prefix + ['create', '--nodes', str(count)] + scope_settings))
                print('# Verify exact running instance inventory')
                if name not in ('Mysticeti', 'Mahi-Mahi'):
                    print(shlex.join(prefix + ['ready']))
                print(shlex.join(prefix + ['install'] + scope_settings))
                for case in size_plan:
                    settings = output / case_key(case) / 'attempt-TIMESTAMP/effective-settings.json'
                    print(shlex.join(prefix + command(case, settings)[1:]))
                print('# All results archived, or exception/interrupt cleanup')
                print(shlex.join(prefix + ['destroy'] + scope_settings))
                print('# Verify batch inventory is empty before the next node count or protocol')
        return
    if sys.platform != 'linux':
        parser.error('Execute on the Ubuntu EC2 controller. Dry-run and plotting also work on Windows.')
    if not shutil.which('fab'):
        parser.error('fab is not on PATH; activate the Fabric Python environment first')
    if not args.cleanup_only and importlib.util.find_spec('matplotlib') is None:
        parser.error('Install matplotlib in this Python environment before running the matrix')
    from lifecycle import ProtocolLifecycle, run_protocol
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('SIGTERM')))
    import fcntl
    # A controller-wide lock also covers separate output directories and protocols.
    with Path('/tmp/five-protocol-figure-matrix.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another five-protocol matrix is running on this controller')
        if args.cleanup_only:
            for name in dict.fromkeys(c['protocol'] for c in plan):
                ProtocolLifecycle(name, args.root / PROTOCOLS[name], output).recover()
            return
        campaign_path = output / 'campaign.json'
        campaign = read(campaign_path) if campaign_path.is_file() else {}
        identities = {}
        for name in dict.fromkeys(c['protocol'] for c in plan):
            bench = args.root / PROTOCOLS[name]
            if not (bench / 'fabfile.py').is_file() or not (bench / 'settings.json').is_file():
                parser.error(f'Missing fabfile.py/settings.json in {bench}; complete setup first')
            try:
                require_prebuilt_remote(name, bench)
            except ValueError as error:
                parser.error(str(error))
            identity = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=bench, text=True).strip(),
                            settings_sha256=sha(bench / 'settings.json'),
                            fabfile_sha256=sha(bench / 'fabfile.py'),
                            wrapper_sha256=sha(Path(__file__)),
                            lifecycle_sha256=sha(Path(__file__).with_name('lifecycle.py')),
                            protocol_tasks_sha256=sha(Path(__file__).with_name('protocol_tasks.py')))
            if not args.upgrade_pool_policy and name in campaign and campaign[name] != identity:
                parser.error(f'{name} source/settings/tools changed since this batch began; use a new --output directory')
            identities[name] = identity
        output.mkdir(parents=True, exist_ok=True)
        if args.upgrade_pool_policy:
            if args.protocol != 'all' or args.mode != 'all':
                parser.error('--upgrade-pool-policy requires the full protocol/mode selection')
            try:
                campaign = upgrade_pool_policy(output, campaign, identities)
            except ValueError as error:
                parser.error(str(error))
        else:
            campaign.update(identities)
        save(campaign_path, campaign)
        plan_file(list(cases()), output)
        for name in dict.fromkeys(c['protocol'] for c in plan):
            protocol_plan = [c for c in plan if c['protocol'] == name]
            run_protocol(protocol_plan, args.root / PROTOCOLS[name], output, run_case, args.retry_failed)
    subprocess.run([sys.executable, str(Path(__file__).with_name('plot_figures.py')), str(output)], check=True)


if __name__ == '__main__':
    main()
