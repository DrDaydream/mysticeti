import json

from mysticeti_aws import metrics


def test_report_shows_zero_latency_as_na_and_detailed_configuration(tmp_path):
    from mysticeti_aws.report import format_summary
    record = {'status': 'complete', 'parameters': {'nodes': 2, 'rate': 100},
              'adversary': {'mode': 'A3', 'byzantine_authorities': [0]},
              'hosts': [{'authority': 0, 'byzantine': True}, {'authority': 1, 'byzantine': False}]}
    before = {str(i): {'count': 0, 'sum': 0} for i in range(2)}
    after = {'0': {'count': 0, 'sum': 0}, '1': {'count': 10, 'sum': 1}}
    summary = metrics.summarize(before, after, {'0': 2, '1': 2})
    text = format_summary(record, summary, tmp_path)
    assert 'SUMMARY' in text and '+ CONFIG' in text and '+ RESULTS' in text
    assert 'Latency contributors: 1/2' in text
    assert '100.000 ms' in text and 'N/A' in text
    assert 'Byzantine' in text and 'Honest' in text and 'A3' in text


def test_offline_report_recovers_zero_progress_run_without_rewriting_status(tmp_path, capsys):
    from mysticeti_aws.report import report_run
    record = {'status': 'failed', 'error': 'old zero progress error', 'parameters': {}}
    (tmp_path / 'run.json').write_text(json.dumps(record))
    snapshots = [
        {'phase': 'before', 'nodes': {'0': {'metrics': {'count': 0, 'sum': 0}, 'monotonic': 1}}},
        {'phase': 'after', 'nodes': {'0': {'metrics': {'count': 0, 'sum': 0}, 'monotonic': 3}}},
    ]
    (tmp_path / 'samples.jsonl').write_text('\n'.join(map(json.dumps, snapshots)))
    report_run(tmp_path)
    output = capsys.readouterr().out
    assert 'SUMMARY' in output and '0.000 tx/s' in output and 'N/A' in output
    assert 'failed' in output and 'old zero progress error' in output
    assert json.loads((tmp_path / 'run.json').read_text()) == record
    assert (tmp_path / 'summary.txt').read_text(encoding='utf-8').strip() in output


def test_failed_run_without_samples_still_prints_diagnostic_summary(tmp_path, capsys):
    from mysticeti_aws.report import report_run
    (tmp_path / 'run.json').write_text(json.dumps({'status': 'failed', 'error': 'node died'}))
    report_run(tmp_path)
    output = capsys.readouterr().out
    assert 'SUMMARY' in output and 'node died' in output and 'N/A' in output
