"""Parse cumulative Mysticeti shared-workload metrics and measure their deltas."""

import math
import re
from statistics import fmean


_METRICS = {"latency_s_count": "count", "latency_s_sum": "sum"}
_LEADERS = "committed_leaders_total"
_DURATION = 'benchmark_duration'
WINDOW_BASIS = 'window-delta-v1'
UPSTREAM_BASIS = 'upstream-terminal-cumulative-v1'
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_SAMPLE = re.compile(
    r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?[ \t]+"
    r"(\S+)(?:[ \t]+[+-]?[0-9]+)?[ \t]*"
)
_LABEL = re.compile(
    r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:\\[\\n"]|[^"\\])*)"\s*(?:,|$)'
)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _labels(text: str) -> dict[str, str]:
    labels = {}
    position = 0
    while text[position:].strip():
        match = _LABEL.match(text, position)
        if match is None:
            raise ValueError(f"Malformed metric labels: {text!r}")
        name, value = match.groups()
        if name in labels:
            raise ValueError(f"Duplicate metric label: {name}")
        labels[name] = re.sub(
            r'\\([\\n"])',
            lambda escaped: {"n": "\n", "\\": "\\", '"': '"'}[escaped.group(1)],
            value,
        )
        position = match.end()
    return labels


def _nonnegative(value, context: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{context} must be a finite nonnegative number") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{context} must be a finite nonnegative number")
    return number


def parse_snapshot(text: str) -> dict:
    """Read one validator's cumulative counters from Prometheus text.

    ``count`` and ``sum`` (seconds) must describe one unambiguous histogram
    series with ``workload="shared"``. Their complete label sets must match;
    label order and extra labels are accepted. Owned workload rows, buckets,
    and unrelated metric families are ignored. Missing shared metrics raise
    ValueError, including during startup; the caller decides when to retry.

    ``committed_leaders`` sums the direct-commit and indirect-commit series
    across authorities in this validator's scrape. Skip statuses are excluded,
    and an absent committed-leader metric defaults to zero.
    """
    result = {"committed_leaders": 0.0}
    shared_labels = {}
    leader_series = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        name_match = _METRIC_NAME.match(line)
        if name_match is None:
            continue
        name = name_match.group()
        if name not in _METRICS and name not in (_LEADERS, _DURATION):
            continue
        sample = _SAMPLE.fullmatch(line)
        if sample is None:
            raise ValueError(f"Malformed {name} sample on line {line_number}")
        labels = _labels(sample.group(2) or "")
        if name in _METRICS and labels.get("workload") != "shared":
            continue
        if name == _LEADERS and labels.get("commit_type") not in {
            "direct-commit", "indirect-commit"
        }:
            continue
        raw_value = sample.group(3)
        if _NUMBER.fullmatch(raw_value) is None:
            raise ValueError(f"Malformed {name} value on line {line_number}")
        value = _nonnegative(raw_value, f"{name} on line {line_number}")
        if name == _DURATION:
            if labels or name in result or not value.is_integer():
                raise ValueError('Invalid or duplicate benchmark_duration counter')
            result[name] = value
        elif name == _LEADERS:
            series = tuple(sorted(labels.items()))
            if series in leader_series:
                raise ValueError("Duplicate committed_leaders_total series")
            leader_series.add(series)
            result["committed_leaders"] += value
        else:
            key = _METRICS[name]
            if key in result:
                raise ValueError(f"Duplicate or ambiguous shared {name} series")
            result[key] = value
            shared_labels[key] = labels
    for key in _METRICS.values():
        if key not in result:
            raise ValueError(f"Missing shared latency_s_{key}")
    if shared_labels["count"] != shared_labels["sum"]:
        raise ValueError("Shared latency_s_count and latency_s_sum labels differ")
    return result


def summarize(
    before: dict[str, dict], after: dict[str, dict], elapsed: dict[str, float]
) -> dict:
    """Summarize one measurement window using each replica's counter deltas.

    The three mappings must contain identical, nonempty authority key sets.
    ``elapsed`` contains per-node seconds between scrape midpoint timestamps
    measured by the caller's monotonic clock. Returned ``nodes`` preserve the
    input order and contain ``authority``, ``committed_transactions``, ``tps``,
    ``mean_latency_ms``, and ``duration_s``.

    ``replica_tps_mean`` is the arithmetic mean of replica TPS, never their
    sum. ``mean_latency_ms`` is the arithmetic mean of per-replica latency
    means, not a count-weighted mean. All counters are finite and nonnegative;
    resets and missing counters raise ValueError. Zero-progress replicas count
    as zero TPS and are excluded from latency; all-zero latency is None.
    Optional ``committed_leaders`` defaults to zero and is checked for
    resets; it does not substitute for committed transaction progress.
    """
    if not before or set(before) != set(after) or set(before) != set(elapsed):
        raise ValueError("Node sets in before, after, and elapsed must be identical and nonempty")
    nodes = []
    for authority, start in before.items():
        duration = _nonnegative(elapsed[authority], f"Node {authority} duration_s")
        if duration == 0:
            raise ValueError(f"Node {authority} duration_s must be positive")
        end = after[authority]
        deltas = {}
        for key in ("count", "sum", "committed_leaders"):
            if key != "committed_leaders" and (key not in start or key not in end):
                raise ValueError(f"Node {authority} is missing {key}")
            first = _nonnegative(start.get(key, 0), f"Node {authority} before {key}")
            last = _nonnegative(end.get(key, 0), f"Node {authority} after {key}")
            if last < first:
                raise ValueError(f"Node {authority} counter reset: {key}")
            deltas[key] = last - first
        count = deltas["count"]
        if count == 0 and deltas['sum'] != 0:
            raise ValueError(f"Node {authority} latency sum changed with zero committed transactions")
        nodes.append(
            {
                "authority": authority,
                "committed_transactions": count,
                "tps": count / duration,
                "mean_latency_ms": deltas["sum"] / count * 1000 if count else None,
                "duration_s": duration,
            }
        )
    latencies = [node['mean_latency_ms'] for node in nodes if node['committed_transactions'] > 0]
    return {
        "nodes": nodes,
        "replica_tps_mean": fmean(node["tps"] for node in nodes),
        "mean_latency_ms": fmean(latencies) if latencies else None,
        "latency_contributors": len(latencies),
        "zero_commit_authorities": [node['authority'] for node in nodes if node['committed_transactions'] == 0],
        "all_nodes_progress": len(latencies) == len(nodes),
    }


def summarize_run(before, after, elapsed, basis=WINDOW_BASIS):
    """Reproduce the selected, recorded statistics basis.

    The upstream terminal averages each replica's last cumulative sum/count,
    including startup. A recorded zero-count replica contributes zero. Its TPS
    is max(last count) // max(last benchmark_duration), not a replica TPS mean.
    Keep the before/after validation even for cumulative reports so resets or
    incomplete runs cannot be silently accepted. Old archives use window deltas.
    """
    if basis not in (WINDOW_BASIS, UPSTREAM_BASIS):
        raise ValueError(f'Unknown measurement basis: {basis}')
    result = summarize(before, after, elapsed)
    if basis == UPSTREAM_BASIS:
        durations = {}
        zero = {}
        for authority, end in after.items():
            for snapshot in (before[authority], end):
                for key in ('count', _DURATION):
                    if key not in snapshot:
                        raise ValueError(f'Node {authority} is missing {key}')
                    value = _nonnegative(snapshot[key], f'Node {authority} {key}')
                    if not value.is_integer():
                        raise ValueError(f'Node {authority} {key} must be an integer')
                if snapshot['count'] == 0 and snapshot['sum'] != 0:
                    raise ValueError(f'Node {authority} has latency sum without commits')
            if end[_DURATION] < before[authority][_DURATION]:
                raise ValueError(f'Node {authority} counter reset: {_DURATION}')
            durations[authority] = int(end[_DURATION])
            zero[authority] = {key: 0 for key in end}
        # Reuse counter validation with an explicit zero origin.
        result = summarize(zero, after, {i: max(1, d) for i, d in durations.items()})
        latency_ns = []
        for row in result['nodes']:
            authority = row['authority']
            count = int(row['committed_transactions'])
            duration = durations[authority]
            mean_ns = round(after[authority]['sum'] * 1_000_000_000) // count if count else 0
            latency_ns.append(mean_ns)
            row.update(mean_latency_ms=mean_ns / 1_000_000,
                       duration_s=duration, tps=count / duration if duration else 0,
                       has_latency_observations=bool(count))
        duration = max(durations.values())
        result.update(
            mean_latency_ms=(sum(latency_ns) // len(latency_ns)) / 1_000_000,
            replica_tps_mean=fmean(row['tps'] for row in result['nodes']),
            upstream_tps=max(int(end['count']) for end in after.values()) // duration if duration else 0,
            latency_contributors=len(after),
            positive_observation_replicas=sum(end['count'] > 0 for end in after.values()),
        )
    result['measurement_basis'] = basis
    return result
