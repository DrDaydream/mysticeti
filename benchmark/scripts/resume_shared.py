#!/usr/bin/env python3
"""Resume original v3.5 cases; defer a failed Mysticeti group until after Mahi."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys

ORIGINAL_TOOLS = {
    'run_matrix.py': 'd3131c1fc4f7cf16b2efc7bd007f7de91b9ea54995c0c79736b45652995689be',
    'plot_figures.py': '913211bc1581797954efbf06c4967c7f2475c4418fa2e2067292d7a10cc04158',
    'lifecycle.py': '1b5664d2ef967d3db898ab5281962f54f92a46e8055be3ef2b0d6c2a518035ee',
    'protocol_tasks.py': '6af2b80f03892bdea9afbaf98c6f30c90c6ca616d36e5b23379bb787af064104',
    'fabfile.py': '1ad757ca93c7321c688666ff3d4219e2d7501b7e770f60353a45f070dafb8a30',
}
BASE = 'b813f80705c3fb9ef240e2b319fd1675ed0be91a'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def schedule(path, run_protocol, cleaned):
    """One deferred retry phase. Interrupts never advance the schedule."""
    state = read(path) if Path(path).exists() else dict(phase='mysticeti', first_mysticeti_error=None)
    while state['phase'] != 'done':
        phase = state['phase']
        if phase not in ('mysticeti', 'mahi', 'retry-mysticeti'):
            raise ValueError('Unknown continuation phase')
        protocol = 'Mahi-Mahi' if phase == 'mahi' else 'Mysticeti'
        print(f'Continuation phase: {phase}', flush=True)
        save(path, state)
        try:
            run_protocol(protocol)
        except Exception as error:
            state['last_error'] = f'{type(error).__name__}: {error}'
            save(path, state)
            if phase != 'mysticeti' or not cleaned(protocol):
                raise
            state.update(first_mysticeti_error=str(error), phase='mahi')
            print('Mysticeti failed; its pool is cleaned. Run Mahi-Mahi before retrying.', flush=True)
        else:
            state.pop('last_error', None)
            state['phase'] = ('mahi' if phase == 'mysticeti' else
                              'retry-mysticeti' if phase == 'mahi' and state['first_mysticeti_error'] is not None else 'done')
        save(path, state)
    return 0


def continuation_plan(matrix, previous):
    pending, completed = [], []
    for protocol in ('Mysticeti', 'Mahi-Mahi'):
        for case in matrix.cases(protocol):
            selected = previous / matrix.case_key(case) / 'selected.json'
            meta = read(selected) if selected.is_file() else None
            if meta and meta['case'] != case:
                raise ValueError(f'Previous case parameters differ: {selected}')
            if meta and meta['status'] == 'complete':
                attempt = meta['attempt']
                if Path(attempt).name != attempt or not attempt.startswith('attempt-'):
                    raise ValueError(f'Invalid archived attempt: {selected}')
                measurement = selected.parent / attempt / 'measurement.json'
                record = read(measurement)
                if record['case'] != case or record['status'] != 'complete':
                    raise ValueError(f'Completed archive is inconsistent: {measurement}')
                completed.append(dict(case=case, selected=str(selected), selected_sha256=sha(selected),
                                      measurement=str(measurement), measurement_sha256=sha(measurement)))
            else:
                pending.append(case)
    return dict(schema=1, previous=str(previous), previous_campaign_sha256=sha(previous / 'campaign.json'),
                completed_previous=completed, pending=pending,
                duration_s=60, attack_start_after_ms=20000, attack_duration_ms=30000,
                attack_origin='each validator initialization', policy='Mysticeti -> Mahi-Mahi -> deferred Mysticeti retry')


def validate_identity(protocol, current, original):
    """Only Mysticeti's specifically checked zero-export source may change."""
    for field in ('settings_sha256', 'fabfile_sha256'):
        if current[field] != original[field]:
            raise ValueError(f'{protocol} {field} differs from the original experiment')
    if protocol == 'Mahi-Mahi' and current['commit'] != original['commit']:
        raise ValueError('Mahi-Mahi source differs from the original experiment')
    if protocol == 'Mysticeti' and original['commit'] != BASE:
        raise ValueError('Original Mysticeti source is not b813f80')
    for name, field in [('run_matrix.py', 'wrapper_sha256'), ('lifecycle.py', 'lifecycle_sha256'),
                        ('protocol_tasks.py', 'protocol_tasks_sha256')]:
        if original[field] != ORIGINAL_TOOLS[name]:
            raise ValueError('Original campaign toolkit does not match v3.5')


def validate_mysticeti_source(repo):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()
    if git('merge-base', BASE, 'HEAD') != BASE:
        raise ValueError('Mysticeti is not based on the original failed-run source')
    allowed = {'mysticeti-core/src/metrics.rs', 'mysticeti-core/tests/zero_shared_metrics.rs',
               'benchmark/scripts/resume_shared.py', 'benchmark/scripts/restore_original_v35.py',
               'benchmark/tests/test_resume_shared.py', 'benchmark/ZERO_OUTPUT_RESUME.md'}
    if set(git('diff', '--name-only', BASE, 'HEAD').splitlines()) - allowed:
        raise ValueError('Mysticeti contains changes beyond the minimal zero-output/resume patch')
    filename = 'mysticeti-core/src/metrics.rs'
    original = subprocess.check_output(['git', 'show', BASE + ':' + filename], cwd=repo, text=True)
    replacement = ('        // Export genuine zero counters before the first shared transaction\n'
                   '        // commits. Merely creating the child does not observe a latency sample.\n'
                   '        metrics.latency_s.with_label_values(&["shared"]);\n'
                   '        (Arc::new(metrics), reporter)')
    expected = original.replace('        (Arc::new(metrics), reporter)', replacement, 1)
    if (repo / filename).read_text() != expected:
        raise ValueError('Mysticeti validator change is not the reviewed zero-counter initialization')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/home/ubuntu/experiments'))
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    tools = root / 'figure-tools'
    previous = (args.previous or root / 'paper-results-v3.5-60s-h20-crash-a1').resolve()
    output = (args.output or root / 'paper-results-v3.5-60s-zero-output-resume').resolve()
    if output == previous or previous in output.parents or output in previous.parents:
        parser.error('Keep the continuation output separate from the original archives')
    for name, expected in ORIGINAL_TOOLS.items():
        if sha(tools / name) != expected:
            parser.error(f'{name} is not the original v3.5 toolkit; run restore_original_v35.py first')
    sys.path.insert(0, str(tools))
    import run_matrix as matrix
    from lifecycle import ProtocolLifecycle, run_protocol
    if not (previous / 'campaign.json').is_file():
        parser.error('Missing original campaign.json')
    original_campaign = read(previous / 'campaign.json')
    plan = continuation_plan(matrix, previous)
    print(f"Original complete shared cases: {len(plan['completed_previous'])}; pending: {len(plan['pending'])}", flush=True)
    print('Parameters: duration=60s, warmup=0s; attack=20-50s after each node initializes.', flush=True)
    if args.dry_run:
        for protocol in ('Mysticeti', 'Mahi-Mahi'):
            cases = [c for c in plan['pending'] if c['protocol'] == protocol]
            print(protocol, len(cases), 'pending cases')
            for case in cases:
                print(matrix.case_key(case), f"duration={case['duration_s']}s")
        return 0
    if sys.platform != 'linux' or not shutil.which('fab'):
        parser.error('Run on the Linux controller with Fabric on PATH')
    import fcntl
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('SIGTERM')))
    with open('/tmp/five-protocol-figure-matrix.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = output / 'continuation.json'
        if output.exists() and any(output.iterdir()):
            if not manifest.is_file() or read(manifest) != plan:
                parser.error('Different or changed continuation manifest; retain the existing output')
        identities = {}
        for protocol in ('Mysticeti', 'Mahi-Mahi'):
            bench = root / matrix.PROTOCOLS[protocol]
            commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=bench, text=True).strip()
            dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=bench, text=True)
            if dirty:
                parser.error(f'{protocol} checkout has uncommitted files')
            identities[protocol] = dict(commit=commit, settings_sha256=sha(bench / 'settings.json'),
                                        fabfile_sha256=sha(bench / 'fabfile.py'), tools=ORIGINAL_TOOLS,
                                        resume_sha256=sha(__file__))
            validate_identity(protocol, identities[protocol], original_campaign[protocol])
            if protocol == 'Mysticeti':
                validate_mysticeti_source(bench.parent)
                source_dir = Path(read(bench / 'settings.json')['remote']['source_dir']).expanduser()
                if source_dir.resolve() != bench.parent.resolve():
                    parser.error('Mysticeti remote.source_dir must be this controller checkout so install builds the zero-output commit')
        campaign_path = output / 'campaign.json'
        if campaign_path.exists() and read(campaign_path) != identities:
            parser.error('Source/settings/resume code changed since continuation started')
        output.mkdir(parents=True, exist_ok=True)
        save(manifest, plan)
        save(campaign_path, identities)
        matrix.plan_file(list(matrix.cases()), output)
        def run(protocol):
            cases = [c for c in plan['pending'] if c['protocol'] == protocol]
            if cases:
                run_protocol(cases, root / matrix.PROTOCOLS[protocol], output,
                             matrix.run_case, retry_failed=True)
            else:
                ProtocolLifecycle(protocol, root / matrix.PROTOCOLS[protocol], output).recover()
        def cleaned(protocol):
            state = output / 'lifecycle' / protocol / 'state.json'
            return not state.exists() or read(state)['status'] == 'terminated'
        result = schedule(output / 'schedule.json', run, cleaned)
    subprocess.run([sys.executable, str(tools / 'plot_figures.py'), str(output)], check=True)
    print(f'Continuation complete. Original results remain at {previous}; new results at {output}.')
    return result


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Interrupted. Cleanup attempted; repeat the same command to resume.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
