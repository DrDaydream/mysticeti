use mysticeti_core::metrics::Metrics;
use prometheus::{Encoder, Registry, TextEncoder};

#[test]
fn shared_metrics_exist_before_any_commit_without_adding_a_sample() {
    let registry = Registry::new();
    let (metrics, _reporter) = Metrics::new(&registry, None);
    let render = || {
        let mut bytes = Vec::new();
        TextEncoder::new().encode(&registry.gather(), &mut bytes).unwrap();
        String::from_utf8(bytes).unwrap()
    };
    let empty = render();
    assert!(empty.contains("latency_s_count{workload=\"shared\"} 0\n"));
    assert!(empty.contains("latency_s_sum{workload=\"shared\"} 0\n"));
    metrics.latency_s.with_label_values(&["shared"]).observe(1.25);
    let committed = render();
    assert!(committed.contains("latency_s_count{workload=\"shared\"} 1\n"));
    assert!(committed.contains("latency_s_sum{workload=\"shared\"} 1.25\n"));
}
