import copy
import pytest


def fixture():
    from mysticeti_aws.timing import make_plan
    plan = make_plan('run-test', 100000, 0, 60)
    windows = {str(i): dict(plan, authority=i, complete=True, count=120 if i == 0 else 0,
                           sum_s=240.0 if i == 0 else 0.0, input_lateness_ms=0) for i in range(2)}
    return plan, windows


def test_zero_progress_is_valid_and_replica_average_is_not_sum():
    from mysticeti_aws.timing import summarize_windows
    plan, windows = fixture()
    result = summarize_windows(plan, windows, ['0', '1'])
    assert result['replica_tps_mean'] == 1
    assert result['mean_latency_ms'] == 2000
    assert result['zero_commit_authorities'] == ['1']
    for value in windows.values():
        value.update(count=0, sum_s=0)
    result = summarize_windows(plan, windows, ['0', '1'])
    assert result['replica_tps_mean'] == 0
    assert result['mean_latency_ms'] is None


@pytest.mark.parametrize('field,value', [('run_id', 'other'), ('complete', False),
    ('count', -1), ('count', 1.5), ('sum_s', float('nan')), ('input_lateness_ms', 501),
    ('end_ms', 160001), ('authority', 4)])
def test_invalid_windows_are_rejected(field, value):
    from mysticeti_aws.timing import summarize_windows
    plan, windows = fixture()
    windows['0'][field] = value
    with pytest.raises(ValueError):
        summarize_windows(plan, windows, ['0', '1'])


def test_missing_replica_and_unmatched_receipt_are_rejected():
    from mysticeti_aws.timing import summarize_windows, validate_receipts
    plan, windows = fixture()
    with pytest.raises(ValueError):
        summarize_windows(plan, {'0': windows['0']}, ['0', '1'])
    receipts = {str(i): dict(plan, authority=i) for i in range(2)}
    validate_receipts(plan, receipts, ['0', '1'], 90000)
    with pytest.raises(ValueError):
        validate_receipts(plan, receipts, ['0', '1'], 96000)
    receipts['1']['start_ms'] += 1
    with pytest.raises(ValueError):
        validate_receipts(plan, receipts, ['0', '1'], 90000)
