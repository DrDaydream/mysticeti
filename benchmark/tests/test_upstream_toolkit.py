"""End-to-end offline matrix fixtures: no SSH or AWS operations."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mysticeti_aws.settings import load_allocation, validate_benchmark


REPO = Path(__file__).resolve().parents[2]
COMMIT = 'b' * 40


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def toolkit(tmp_path, monkeypatch):
    prepare = module(REPO / 'benchmark/scripts/prepare_shared_upstream.py', 'prepare_upstream_test')
    monkeypatch.setattr(prepare, 'source_commit', lambda _: COMMIT)
    path = prepare.prepare(tmp_path)
    matrix = module(path / 'run_matrix.py', 'upstream_matrix_test')
    return prepare, path, matrix


def test_preparation_is_idempotent_and_preserves_original_and_conflicting_files(toolkit, tmp_path):
    prepare, path, matrix = toolkit
    original = tmp_path / 'figure-tools'
    original.mkdir()
    (original / 'run_matrix.py').write_text('original')
    before = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    prepare.prepare(tmp_path)
    assert {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()} == before
    (path / 'plot_figures.py').write_text('user edit')
    with pytest.raises(ValueError, match='preserved'):
        prepare.prepare(tmp_path)
    assert (path / 'plot_figures.py').read_text() == 'user edit'
    assert (original / 'run_matrix.py').read_text() == 'original'
    assert (path / 'run_matrix.py').read_bytes() == before['run_matrix.py']


def test_all_96_cases_validate_and_attacks_keep_existing_parameters(toolkit, tmp_path):
    _, path, matrix = toolkit
    settings = tmp_path / 'settings.json'
    settings.write_text('{}')
    assert len(list(matrix.cases())) == 480
    cases = list(matrix.cases('Mysticeti'))
    assert len(cases) == 96
    old = {(c['nodes'], c['mode'], c['input_rate_tps']): c for c in matrix.v35_cases('Mysticeti')}
    for case in cases:
        params = validate_benchmark(matrix.effective_settings(settings, case)['benchmark'])
        command = matrix.command(case, settings)
        assert params['warmup'] == 0 and params['duration'] == 60
        assert command[command.index('--warmup') + 1] == '0'
        assert command[command.index('--duration') + 1] == '60'
        assert case['source_commit'] == COMMIT
        assert params['enable_synchronizer'] is True
        assert (params['leaders'], params['wave_length'], params['leader_timeout_ms']) == (2, 3, 1000)
        if case['mode'] in ('A0', 'A1'):
            faults = (case['nodes'] - 1) // 3 if case['mode'] == 'A1' else 0
            assert params['faults'] == faults
            assert params['adversary']['mode'] == 'A0'
            assert params['adversary']['byzantine_authorities'] == []
            assert case['measurement_basis'] == matrix.UPSTREAM_BASIS
            assert load_allocation(params['rate'], params['nodes'], faults)['effective_total_tps'] == case['offered_rate_tps']
        else:
            previous = old[case['nodes'], case['mode'], case['input_rate_tps']]
            assert all(case[key] == value for key, value in previous.items())
            attack = params['adversary']
            assert (attack['start_after_ms'], attack['duration_ms'], attack['seed']) == (20000, 30000, 7)
            assert (attack['delay_ms'], attack['late_delay_ms']) == (200, 1000)
            assert (attack['max_pending_messages'], attack['max_pending_bytes'], attack['max_extra_bytes_per_round']) == (4096, 67108864, 8388608)
            assert case['measurement_basis'] == matrix.WINDOW_BASIS


def archive_fixture(matrix, root, mode, nodes, zero):
    case = next(c for c in matrix.cases('Mysticeti', mode) if c['nodes'] == nodes)
    settings = root / 'settings.json'
    settings.write_text('{}')
    params = validate_benchmark(matrix.effective_settings(settings, case)['benchmark'])
    active = nodes - params['faults']
    native = root / 'native'
    (native / 'logs').mkdir(parents=True)
    for i in range(active):
        (native / 'logs' / f'node-{i}.log').write_text('valid log')
    record = dict(status='complete', commit=COMMIT, parameters=params, adversary=params['adversary'],
                  measurement_basis=case['measurement_basis'], crashed_authorities=list(range(active, nodes)),
                  load=load_allocation(params['rate'], nodes, params['faults']),
                  hosts=[dict(authority=i, started=i < active, byzantine=i in case['byzantine_ids']) for i in range(nodes)])
    matrix.save(native / 'run.json', record)
    samples = []
    for phase, time in [('before', 10), ('after', 70)]:
        samples.append(dict(phase=phase, nodes={str(i): dict(monotonic=time, metrics=dict(
            count=0 if zero else (100 if phase == 'before' else 160),
            sum=0 if zero else (1000 if phase == 'before' else 1120), benchmark_duration=time))
            for i in range(active)}))
    (native / 'samples.jsonl').write_text('\n'.join(map(json.dumps, samples)))
    shutil.copyfile(REPO / 'benchmark/mysticeti_aws/metrics.py', root / 'measurement_parser.py')
    return case, record


@pytest.mark.parametrize('mode', ['A0', 'A1', 'A3', 'A5'])
@pytest.mark.parametrize('nodes', [10, 20, 35])
@pytest.mark.parametrize('zero', [False, True])
def test_archived_results_use_native_basis_for_every_population(toolkit, tmp_path, mode, nodes, zero):
    _, path, matrix = toolkit
    case, record = archive_fixture(matrix, tmp_path, mode, nodes, zero)
    result = matrix.shared_measurement(tmp_path, case)
    if mode in ('A0', 'A1'):
        assert result['latency_ms'] == (0 if zero else 7000)
        assert result['upstream_tps'] == (0 if zero else 2)
        assert result['latency_contributors'] == nodes - case.get('faults', 0)
        assert result['positive_observation_replicas'] == (0 if zero else nodes - case.get('faults', 0))
        assert all(r['completed'] == (0 if zero else 160) for r in result['replicas'])
    else:
        assert result['latency_ms'] == (None if zero else 2000)
        assert all(r['completed'] == (0 if zero else 60) for r in result['replicas'])
    assert result['source_commit'] == COMMIT


@pytest.mark.parametrize('corruption', ['commit', 'basis', 'logs', 'launch', 'counter'])
def test_stale_or_incomplete_runs_are_rejected(toolkit, tmp_path, corruption):
    _, _, matrix = toolkit
    case, record = archive_fixture(matrix, tmp_path, 'A1', 10, False)
    if corruption == 'commit':
        record['commit'] = 'a' * 40
    elif corruption == 'basis':
        record['measurement_basis'] = 'window-delta-v1'
    elif corruption == 'logs':
        (tmp_path / 'native/logs/node-0.log').unlink()
    elif corruption == 'launch':
        record['hosts'][9]['started'] = True
    else:
        samples = tmp_path / 'native/samples.jsonl'
        values = [json.loads(line) for line in samples.read_text().splitlines()]
        values[-1]['nodes']['0']['metrics']['count'] = 90
        samples.write_text('\n'.join(map(json.dumps, values)))
    matrix.save(tmp_path / 'native/run.json', record)
    with pytest.raises(ValueError):
        matrix.shared_measurement(tmp_path, case)


def test_dry_run_writes_all_plan_rows_without_cloud(toolkit, tmp_path):
    _, path, matrix = toolkit
    output = tmp_path / 'dry-output'
    result = subprocess.run([sys.executable, str(path / 'run_matrix.py'), '--protocol', 'Mysticeti',
                             '--root', str(tmp_path), '--output', str(output), '--dry-run'],
                            capture_output=True, text=True, check=True)
    assert result.stdout.count(' remote ') == 96
    assert len((output / 'experiment-matrix.csv').read_text(encoding='utf-8-sig').splitlines()) == 481
    assert not (output / 'lifecycle').exists()


def test_source_preflight_rejects_unpinned_checkout(toolkit, tmp_path, monkeypatch):
    _, path, matrix = toolkit
    monkeypatch.setattr(matrix.subprocess, 'check_output', lambda *a, **kw: 'a' * 40)
    with pytest.raises(ValueError, match='pinned source'):
        matrix.require_prebuilt_remote('Mysticeti', tmp_path)


@pytest.mark.parametrize('protocol', ['Mysticeti', 'Mahi-Mahi'])
def test_preflight_checks_only_selected_protocol_and_detects_tool_edits(toolkit, tmp_path, monkeypatch, protocol):
    _, path, matrix = toolkit
    expected = json.loads((path / 'source-lock.json').read_text())['protocols'][protocol]['commit']
    def git(args, **kwargs):
        assert kwargs['cwd'] == tmp_path
        return '' if 'status' in args else expected
    monkeypatch.setattr(matrix.subprocess, 'check_output', git)
    (tmp_path / 'settings.json').write_text(json.dumps({'repo': {'ref': expected}}))
    matrix.require_prebuilt_remote(protocol, tmp_path)
    (path / 'run_matrix.py').write_text('edited source')
    with pytest.raises(ValueError, match='Toolkit differs'):
        matrix.require_prebuilt_remote(protocol, tmp_path)


def test_mahi_keeps_delivered_case_semantics_and_lifecycle(toolkit, tmp_path):
    import hashlib
    _, path, matrix = toolkit
    cases = list(matrix.cases('Mahi-Mahi'))
    assert len(cases) == 96
    assert len(list(matrix.cases('Mysticeti'))) + len(cases) == 192
    settings = tmp_path / 'settings.json'
    settings.write_text('{}')
    for case in cases:
        assert case['source_commit'] == '3c7d32fa815defca8ab82129a461d5a417082e7b'
        params = matrix.effective_settings(settings, case)['benchmark']
        assert (params['leaders'], params['wave_length'], params['leader_timeout_ms']) == (4, 5, 250)
        assert params['warmup'] == 0 and params['duration'] == 60
        if case['mode'] in ('A0', 'A1'):
            assert params['adversary']['mode'] == 'A0'
            assert params['adversary']['byzantine_authorities'] == []
            assert params['faults'] == ((case['nodes'] - 1) // 3 if case['mode'] == 'A1' else 0)
            assert case['measurement_basis'] == matrix.UPSTREAM_BASIS
        else:
            assert params['adversary']['mode'] == case['mode']
            assert params['faults'] == 0
            assert (params['adversary']['start_after_ms'], params['adversary']['duration_ms']) == (20000, 30000)
            assert case['measurement_basis'] == matrix.WINDOW_BASIS
    assert hashlib.sha256((path / 'lifecycle.py').read_bytes()).hexdigest() == '1b5664d2ef967d3db898ab5281962f54f92a46e8055be3ef2b0d6c2a518035ee'
