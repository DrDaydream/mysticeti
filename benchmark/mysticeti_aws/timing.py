"""Versioned, node-local measurement windows; SSH timing is not a boundary."""
import math
from .metrics import summarize


def make_plan(run_id, load_ms, warmup, duration):
    if not isinstance(run_id, str) or not run_id:
        raise ValueError('Invalid run_id')
    for value in (load_ms, warmup, duration):
        if type(value) is not int or value < 0:
            raise ValueError('Times must be nonnegative integers')
    if duration == 0:
        raise ValueError('Duration must be positive')
    start = load_ms + warmup * 1000
    end = start + duration * 1000
    if end >= 2**53:
        raise ValueError('Timestamp is outside supported range')
    return dict(schema=2, run_id=run_id, load_ms=load_ms, start_ms=start, end_ms=end)


def _identities(plan, records, expected):
    if not expected or set(records) != set(expected):
        raise ValueError('Missing or unexpected authority')
    for key, record in records.items():
        if not isinstance(record, dict) or type(record.get('authority')) is not int or record['authority'] != int(key):
            raise ValueError('Authority mismatch')
        if any(type(record.get(k)) is not type(v) or record[k] != v for k, v in plan.items()):
            raise ValueError('Run or timeline mismatch')


def validate_receipts(plan, receipts, expected, now_ms):
    _identities(plan, receipts, expected)
    if now_ms >= plan['load_ms'] - 5000:
        raise ValueError('Start acknowledgements missed the 5s guard')


def summarize_windows(plan, windows, expected):
    if plan != make_plan(plan['run_id'], plan['load_ms'],
                         (plan['start_ms'] - plan['load_ms']) // 1000,
                         (plan['end_ms'] - plan['start_ms']) // 1000):
        raise ValueError('Invalid timeline')
    _identities(plan, windows, expected)
    before, after, elapsed = {}, {}, {}
    for key, w in windows.items():
        count, total, late = w.get('count'), w.get('sum_s'), w.get('input_lateness_ms')
        if w.get('complete') is not True or type(count) is not int or count < 0:
            raise ValueError('Incomplete or invalid transaction count')
        if type(total) not in (int, float) or not math.isfinite(total) or total < 0:
            raise ValueError('Invalid E2E sum')
        if type(late) not in (int, float) or not math.isfinite(late) or not 0 <= late <= 500:
            raise ValueError('Input start is absent or more than 500ms late')
        before[key] = dict(count=0, sum=0)
        after[key] = dict(count=count, sum=total)
        elapsed[key] = (plan['end_ms'] - plan['start_ms']) / 1000
    return summarize(before, after, elapsed)
