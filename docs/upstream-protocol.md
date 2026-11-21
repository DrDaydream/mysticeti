# Mysticeti upstream protocol experiment

This branch starts from `a80b5ed72d0ddfb73c102071e0a9aded0261427a` and aligns normal execution with the upstream baseline `MystenLabs/mysticeti@68d9038f4055e67ced4ae96c1de46b9b0239413b`. It retains the existing adversary overlay and genuine zero-valued shared metrics. It is not a byte-pristine upstream binary.

## Protocol and fault behavior

All experiment modes use the upstream launcher mapping: `enable_synchronizer=true` exports `USE_SYNCER=1`. Despite that flag's name, upstream `BlockFetcherWorker::sync_strategy` returns early when the variable exists, skipping active missing-parent fetch. Normal streaming, proposal, and commit rules remain in the upstream core. A0's adversary sender uses its direct fast path. The new run metadata records `missing_parent_fetch_enabled=false` explicitly.

The previous adapter unset this variable and activated extra fetching. This branch changes the Python launch environment, not Rust consensus or adversary code. A3/A5 retain their adversary code and parameters, while their normal synchronization path also follows upstream. Their measured performance may therefore change.

| Figure label | Native adversary | Faults | Started processes | Statistics |
|---|---|---|---|---|
| A0 | A0, empty Byzantine identities | 0 | All n | Upstream cumulative |
| A1 | A0, empty Byzantine identities | floor((n-1)/3) | First n-f; tail f never start | Upstream cumulative |
| A3 | A3, first f identities | 0 | All n | Existing window delta |
| A5 | A5, first f identities | 0 | All n | Existing window delta |

A1 uses 3/6/11 crashed identities and 7/14/24 started processes for committees of 10/20/35. The committee still contains n identities. Per-node input remains the configured total divided by n, with the existing generator quantum; absent nodes generate no input. The toolkit chooses divisible rates and plots the offered load of started generators.

This is the campaign's requested permanent process-fault model. Upstream's original permanent-fault orchestrator starts processes and kills selected nodes at its first scheduled fault tick. Never starting the tail processes matches Orca/Mahi campaign semantics, not that exact kill timing. The low-level `fab remote --adversary A1` still selects the old temporary withholding attack. To run the new A1 manually, use `--adversary A0 --faults f`.

## Workload, E2E, and timing

The existing benchmark deliberately exports `CONSENSUS_ONLY=1` and `INITIAL_DELAY=0`. Upstream orchestrator defaults consensus-only to false, and upstream's validator defaults initial delay to 10 seconds. These are explicit campaign workload/timing choices, not claims to use every upstream default. Each case measures for 60 seconds after readiness, with no extra warmup. Launch/readiness and final scrape overhead can extend the lifetime of a process beyond 60 seconds.

`transactions_generator.rs` and `block_handler.rs` are unchanged from the upstream baseline. Shared E2E starts at the transaction's embedded timestamp, before the internal input channel, and ends at its commit callback. Subsequent input queueing is included. Scheduling before timestamp creation and a response to an external client are not included.

A0/A1 reproduce upstream terminal arithmetic from the last cumulative sample of every active replica: each replica's `latency_s_sum / latency_s_count`, then equal averaging of these means. Startup commits are included, with no before-snapshot subtraction. A recorded zero-count replica contributes arithmetic 0 as upstream does, and is explicitly marked as having no observations; an all-zero result is not evidence of fast E2E. Crashed, unstarted processes do not participate. The shared histogram represents committed transaction entries, not block means.

Upstream terminal TPS is `max(last cumulative count) // max(last benchmark_duration)`. The duration metric is updated on commits, so it represents exported benchmark time at the latest commit rather than final scrape uptime. The report also retains a separately labeled mean of replica TPS for diagnosis.

A3/A5 retain before/after histogram differences, positive-observation replica latency means, and their existing honest-replica figure aggregation. Their attack starts 20 seconds after each local Core initializes and lasts 30 seconds. These clocks are not synchronized to the controller measurement start.

Missing metrics, counter resets, invalid samples, process/collection failures and incomplete windows remain errors. Genuine zero observations remain valid. Historical records without `measurement_basis` keep the old window interpretation when re-reported.

## Independent toolkit

Run `python3 benchmark/scripts/prepare_shared_upstream.py --root /home/ubuntu/experiments` from a clean committed checkout. It creates `figure-tools-shared-upstream`, leaving the original `figure-tools` intact. The source lock pins this Mysticeti delivery and Mahi-Mahi `3c7d32fa815defca8ab82129a461d5a417082e7b`. Running Mysticeti checks only Mysticeti's checkout; it does not require a Mahi checkout to be installed or modified.

`bash benchmark/scripts/run_mysticeti_upstream.sh` runs only Mysticeti's 96 cases in `paper-results-v3.5-60s-mysticeti-upstream`. It first recovers only recorded Mysticeti pools from the two previous campaigns. It does not operate Mahi pools. The original lifecycle is preserved: create an exact-size pool, install, run sequential cases, then destroy and verify cleanup. The first failure destroys the pool and exits. Rerunning skips completed cases in this new campaign and retries failed/pending cases. The earlier special Mysticeti-to-Mahi fallback wrapper is not used by this single-protocol launcher.

Old results cannot be relabeled as this campaign. Source, settings, toolkit, native source identity, measurement basis, host identity, load allocation, and collected logs are validated. A source/settings change requires a new output directory. A controller-wide lock rejects concurrent matrix runs or updates.
