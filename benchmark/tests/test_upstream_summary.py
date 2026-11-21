"""Upstream terminal arithmetic, distinct from commit-window differences."""
import json

import pytest

from mysticeti_aws import metrics
from mysticeti_aws.report import format_summary, report_run


BASIS = 'upstream-terminal-cumulative-v1'


def snapshots():
    before = {'0': dict(count=100, sum=1000, benchmark_duration=10),
              '1': dict(count=0, sum=0, benchmark_duration=12)}
    after = {'0': dict(count=160, sum=1120, benchmark_duration=70),
             '1': dict(count=20, sum=20, benchmark_duration=100)}
    return before, after, {'0': 60, '1': 60}


def test_cumulative_keeps_startup_commits_and_matches_upstream_tps():
    before, after, elapsed = snapshots()
    result = metrics.summarize_run(before, after, elapsed, BASIS)
    # Replica means: 1120/160=7s and 20/20=1s, equally averaged.
    assert result['mean_latency_ms'] == 4000
    assert result['nodes'][0]['committed_transactions'] == 160
    # Upstream uses max(count)//max(duration), NOT max(count/duration).
    assert result['upstream_tps'] == 1
    assert result['measurement_basis'] == BASIS
    assert result['latency_contributors'] == 2
    window = metrics.summarize_run(before, after, elapsed, 'window-delta-v1')
    assert window['mean_latency_ms'] == 1500
    assert window['nodes'][0]['committed_transactions'] == 60
    assert 'upstream_tps' not in window


@pytest.mark.parametrize('all_zero', [False, True])
def test_recorded_zero_replica_matches_upstream_arithmetic_and_is_identified(all_zero):
    before, after, elapsed = snapshots()
    before['1'].update(count=0, sum=0)
    after['1'].update(count=0, sum=0)
    if all_zero:
        before['0'].update(count=0, sum=0)
        after['0'].update(count=0, sum=0)
    result = metrics.summarize_run(before, after, elapsed, BASIS)
    assert result['mean_latency_ms'] == (0 if all_zero else 3500)
    assert result['nodes'][1]['mean_latency_ms'] == 0
    assert result['latency_contributors'] == 2
    assert result['positive_observation_replicas'] == (0 if all_zero else 1)
    text = format_summary({'status': 'complete'}, result, '/run')
    assert 'Upstream terminal TPS' in text
    assert 'Zero-count means 0 in upstream arithmetic; it is not a measured zero E2E.' in text
    assert 'positive-commit replica means' not in text


@pytest.mark.parametrize('problem', ['missing', 'reset', 'negative', 'fractional', 'count', 'sum'])
def test_invalid_cumulative_metrics_do_not_become_zero_results(problem):
    before, after, elapsed = snapshots()
    if problem == 'missing':
        del after['0']['benchmark_duration']
    elif problem == 'reset':
        after['0']['benchmark_duration'] = 9
    elif problem == 'negative':
        after['0']['benchmark_duration'] = -1
    elif problem == 'fractional':
        after['0']['benchmark_duration'] = 70.5
    elif problem == 'count':
        after['0']['count'] = 160.5
    else:
        after['0']['sum'] = 999
    with pytest.raises(ValueError):
        metrics.summarize_run(before, after, elapsed, BASIS)


def test_duration_parser_is_optional_for_old_window_archives_but_strict_when_present():
    text = 'latency_s_count{workload="shared"} 0\nlatency_s_sum{workload="shared"} 0\n'
    assert 'benchmark_duration' not in metrics.parse_snapshot(text)
    assert metrics.parse_snapshot(text + 'benchmark_duration 60\n')['benchmark_duration'] == 60
    for value in ('-1', 'NaN', '1.5'):
        with pytest.raises(ValueError):
            metrics.parse_snapshot(text + f'benchmark_duration {value}\n')
    with pytest.raises(ValueError):
        metrics.parse_snapshot(text + 'benchmark_duration 60\nbenchmark_duration 60\n')


def test_offline_report_uses_recorded_basis_and_preserves_old_archives(tmp_path):
    before, after, elapsed = snapshots()
    record = {'status': 'complete', 'measurement_basis': BASIS, 'parameters': {}}
    samples = [{'phase': phase, 'nodes': {
        key: dict(metrics=value, monotonic=(10 if phase == 'before' else 70))
        for key, value in snapshot.items()}}
        for phase, snapshot in [('before', before), ('after', after)]]
    (tmp_path / 'samples.jsonl').write_text('\n'.join(map(json.dumps, samples)))
    (tmp_path / 'run.json').write_text(json.dumps(record))
    assert '4,000.000 ms' in report_run(tmp_path)
    del record['measurement_basis']
    (tmp_path / 'run.json').write_text(json.dumps(record))
    text = report_run(tmp_path)
    assert '1,500.000 ms' in text
    assert 'Upstream terminal TPS' not in text


def test_unknown_basis_is_rejected():
    with pytest.raises(ValueError, match='basis'):
        metrics.summarize_run(*snapshots(), 'unknown')
