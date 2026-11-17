"""Independent examples for the shared-workload benchmark measurement contract."""

import pytest

from mysticeti_aws import metrics


def test_parse_shared_workload_ignores_owned_transactions():
    text = '''
# HELP latency_s End-to-end latency in seconds
# TYPE latency_s histogram
latency_s_count{workload="owned"} 9000
latency_s_sum{workload="owned"} 1800
latency_s_bucket{workload="shared",le="+Inf"} 100
latency_s_count{workload="shared"} 100
latency_s_sum{workload="shared"} 10
'''
    assert metrics.parse_snapshot(text) == {
        "count": 100.0,
        "sum": 10.0,
        "committed_leaders": 0.0,
    }


def test_parse_accepts_reordered_extra_labels_exponents_and_timestamps():
    text = r'''
latency_s_count{zone="a,b",workload="shared",note="brace}quote\"slash\\"} 2e2 1700000000000
latency_s_sum{note="brace}quote\"slash\\", workload="shared", zone="a,b"} +2.5E1 1700000000000
latency_s_sum{workload="owned"} NaN
latency_s_count{workload="owned"} -20
'''
    assert metrics.parse_snapshot(text) == {
        "count": 200.0,
        "sum": 25.0,
        "committed_leaders": 0.0,
    }


def test_parse_counts_committed_leaders_and_excludes_skipped_leaders():
    text = '''
latency_s_count{workload="shared"} 100
latency_s_sum{workload="shared"} 10
committed_leaders_total{authority="0",commit_type="direct-commit"} 10
committed_leaders_total{commit_type="indirect-commit",authority="1"} 2
committed_leaders_total{authority="0",commit_type="direct-skip"} 90
committed_leaders_total{authority="1",commit_type="indirect-skip"} 30
'''
    assert metrics.parse_snapshot(text)["committed_leaders"] == 12.0


@pytest.mark.parametrize("metric", ["count", "sum"])
@pytest.mark.parametrize(
    "value", ["-1", "NaN", "+Inf", "-Inf", "1e309", "garbage", "1_00"]
)
def test_parse_rejects_invalid_shared_counters(metric, value):
    rows = {"count": "100", "sum": "10"}
    rows[metric] = value
    text = "\n".join(
        f'latency_s_{key}{{workload="shared"}} {sample}'
        for key, sample in rows.items()
    )
    with pytest.raises(ValueError):
        metrics.parse_snapshot(text)


@pytest.mark.parametrize("missing", ["count", "sum"])
def test_parse_requires_both_shared_counters(missing):
    present = "sum" if missing == "count" else "count"
    text = f'latency_s_{present}{{workload="shared"}} 10\n'
    text += f'latency_s_{missing}{{workload="owned"}} 200\n'
    with pytest.raises(ValueError, match="Missing"):
        metrics.parse_snapshot(text)


@pytest.mark.parametrize(
    "extra_row",
    [
        'latency_s_count{workload="shared"} 100',
        'latency_s_sum{workload="shared"} 10',
        'latency_s_count{workload="shared",replica="other"} 300',
    ],
)
def test_parse_rejects_duplicate_or_ambiguous_shared_series(extra_row):
    text = '''
latency_s_count{workload="shared"} 100
latency_s_sum{workload="shared"} 10
'''
    with pytest.raises(ValueError, match="[Dd]uplicate|[Aa]mbiguous"):
        metrics.parse_snapshot(text + extra_row)


def test_parse_rejects_mismatched_count_and_sum_label_sets():
    text = '''
latency_s_count{workload="shared",replica="a"} 100
latency_s_sum{workload="shared",replica="b"} 10
'''
    with pytest.raises(ValueError, match="labels"):
        metrics.parse_snapshot(text)


@pytest.mark.parametrize(
    "broken_labels",
    [
        'workload="shared",workload="shared"',
        'workload="shared" junk="x"',
        'workload="shared",broken',
    ],
)
def test_parse_rejects_malformed_shared_labels(broken_labels):
    text = f'latency_s_count{{{broken_labels}}} 100\n'
    text += 'latency_s_sum{workload="shared"} 10\n'
    with pytest.raises(ValueError):
        metrics.parse_snapshot(text)


def test_parse_rejects_duplicate_committed_leader_series():
    text = '''
latency_s_count{workload="shared"} 100
latency_s_sum{workload="shared"} 10
committed_leaders_total{authority="0",commit_type="direct-commit"} 10
committed_leaders_total{commit_type="direct-commit",authority="0"} 10
'''
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        metrics.parse_snapshot(text)


def test_summary_uses_window_deltas_and_averages_replicas():
    before = {
        "authority-0": {"count": 100, "sum": 10},
        "authority-1": {"count": 50, "sum": 5},
    }
    after = {
        "authority-0": {"count": 300, "sum": 50},
        "authority-1": {"count": 450, "sum": 125},
    }
    result = metrics.summarize(before, after, {"authority-0": 2, "authority-1": 2})
    assert result["nodes"] == [
        {
            "authority": "authority-0",
            "committed_transactions": 200,
            "tps": 100,
            "mean_latency_ms": 200,
            "duration_s": 2,
        },
        {
            "authority": "authority-1",
            "committed_transactions": 400,
            "tps": 200,
            "mean_latency_ms": 300,
            "duration_s": 2,
        },
    ]
    assert result["replica_tps_mean"] == 150
    assert result["mean_latency_ms"] == 250
    assert result["all_nodes_progress"] is True


def test_summary_uses_each_nodes_measured_elapsed_time():
    before = {"a": {"count": 100, "sum": 10}, "b": {"count": 100, "sum": 10}}
    after = {"a": {"count": 300, "sum": 50}, "b": {"count": 300, "sum": 50}}
    result = metrics.summarize(before, after, {"a": 2, "b": 4})
    assert [row["tps"] for row in result["nodes"]] == [100, 50]
    assert [row["duration_s"] for row in result["nodes"]] == [2, 4]
    assert result["replica_tps_mean"] == 75


@pytest.mark.parametrize(
    "before,after,elapsed",
    [
        ({}, {}, {}),
        ({"a": {"count": 0, "sum": 0}}, {}, {"a": 2}),
        ({}, {"a": {"count": 1, "sum": 1}}, {"a": 2}),
        ({"a": {"count": 0, "sum": 0}}, {"b": {"count": 1, "sum": 1}}, {"a": 2}),
        ({"a": {"count": 0, "sum": 0}}, {"a": {"count": 1, "sum": 1}}, {}),
        (
            {"a": {"count": 0, "sum": 0}},
            {"a": {"count": 1, "sum": 1}},
            {"a": 2, "extra": 2},
        ),
    ],
)
def test_summary_rejects_empty_or_mismatched_node_sets(before, after, elapsed):
    with pytest.raises(ValueError, match="[Nn]ode"):
        metrics.summarize(before, after, elapsed)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_summary_rejects_invalid_elapsed_time(duration):
    with pytest.raises(ValueError):
        metrics.summarize(
            {"a": {"count": 100, "sum": 10}},
            {"a": {"count": 300, "sum": 50}},
            {"a": duration},
        )


@pytest.mark.parametrize(
    "end",
    [
        {"count": 99, "sum": 20, "committed_leaders": 10},
        {"count": 300, "sum": 9, "committed_leaders": 10},
        {"count": 300, "sum": 50, "committed_leaders": 9},
    ],
)
def test_summary_rejects_resets_in_any_counter(end):
    with pytest.raises(ValueError, match="reset"):
        metrics.summarize(
            {"a": {"count": 100, "sum": 10, "committed_leaders": 10}},
            {"a": end},
            {"a": 2},
        )


def test_summary_rejects_one_stalled_replica_even_when_another_progresses():
    before = {"a": {"count": 100, "sum": 10}, "stalled": {"count": 100, "sum": 10}}
    after = {"a": {"count": 300, "sum": 50}, "stalled": {"count": 100, "sum": 10}}
    with pytest.raises(ValueError, match="stalled"):
        metrics.summarize(before, after, {"a": 2, "stalled": 2})


@pytest.mark.parametrize("key", ["count", "sum"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_summary_rejects_missing_snapshot_counters(key, boundary):
    before = {"a": {"count": 100, "sum": 10}}
    after = {"a": {"count": 300, "sum": 50}}
    target = before if boundary == "before" else after
    del target["a"][key]
    with pytest.raises(ValueError):
        metrics.summarize(before, after, {"a": 2})


@pytest.mark.parametrize("bad_value", [-1, float("nan"), float("inf")])
def test_summary_validates_raw_input_counters(bad_value):
    with pytest.raises(ValueError):
        metrics.summarize(
            {"a": {"count": 100, "sum": bad_value}},
            {"a": {"count": 300, "sum": 50}},
            {"a": 2},
        )
