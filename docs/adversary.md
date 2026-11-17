# Reproducible A0–A5 block scheduling experiments

The strategy implementation lives in `mysticeti-core/src/adversary.rs`. Core calls it when creating a local proposal, the committer exposes its final local leader statuses for observation, and the synchronizer passes outgoing blocks through it. Quorum, signature verification, direct/indirect decision rules and leader election are unchanged.

This is an experimental local-observation scheduler. It does not promise to prevent a chosen leader from committing: available DAG support, delivery races, honest forwarding and the original skip/indirect rules determine the outcome. An attempted split, a failed direct commit, an undecided slot and stalled global progress are different measurements.

| Mode | Behavior |
| --- | --- |
| A0 | Original proposal and block-send path. No Byzantine identities or delayed-send worker. |
| A1 | Fixed Byzantine authors withhold their proposals during a finite attack window. |
| A2 | Up to two valid versions of each Byzantine proposal, with seeded nondirected selective delivery. |
| A3 | Select and construct versions using locally observed DAG support for the scheduled target leader. |
| A4 | Continue from locally observed unresolved slots and relevant later Byzantine leaders; release alternate/critical evidence later. |
| A5 | A4 plus bounded scheduling of honest-to-honest block delivery, including forwarding and digest fetch responses. |

The sender signs only its own configured Byzantine author. Alternatives keep the same transaction statements, creation timestamp and epoch marker, while changing parent references/order. Every generated block must pass the regular signature and previous-round quorum verification; infeasible attempts fall back with an event. Multiple digests do not create additional voting identities.

Two versions are retained by digest in the WAL and published together to the in-memory index, so dissemination cannot pass a half-published pair. The ordinary own-round cursor still points to the primary proposal; the adversary module enumerates the versions for selective dissemination. Fetch responses always contain the requested digest, including after WAL replay/cache unloading. An additional WAL record type (`6`) stores local alternatives without replaying their identical payload as another local application proposal. Attack WALs therefore require the modified binary to reopen.

## Configuration

Fabric uses `fab remote --adversary A3 --byzantine 3` or an explicit comma-separated `--byzantine-ids`. See `benchmark/README.md` for controller setup and build provenance. `faults` continues to mean omitted crash processes; it is separate from Byzantine identities. The two sets must be disjoint and their total must stay within the fault threshold. An explicit target never changes the fixed Byzantine set.

The generated `parameters.yaml` contains:

```yaml
adversary:
  mode: A3
  byzantine_authorities: [0, 1, 2]
  seed: 1
  start_after_ms: 20000
  duration_ms: 60000
  delay_ms: 200
  late_delay_ms: 1000
  max_pending_messages: 4096
  max_pending_bytes: 67108864
  max_extra_bytes_per_round: 8388608
  target_authority: null
  target_round: null
```

The Rust default is A0. Fabric resolves a missing attack start to the configured warmup and saves the effective configuration, exact source revision and binary checksum with the run. Keep a recovery interval after the attack. Timing is monotonic relative to each process's initialization; process startup skew must be considered when comparing replica traces. This is not a globally synchronized external controller.

The queue enforces message and serialized-block byte limits across a node's connections. Overflow releases traffic early and records the event, reducing attack strength instead of creating unbounded memory use or artificial permanent isolation. Timed releases do not require another proposal to arrive. Connection loss can abandon queued deliveries; normal reconnection/fetch applies. A0–A4 do not control honest forwarding, so honest dissemination can reveal an alternate version early. A5 controls outgoing honest block paths too. Finite scheduler delay is separate from ordinary network backpressure.

Mysticeti uses its existing deterministic round-robin leader schedule and three-round waves. Attack runs require `enable_synchronizer=true`. The launcher removes `USE_SYNCER` from the node environment because this prototype's `BlockFetcherWorker` skips missing-block fetching whenever that variable exists, even if its value is `0`. Setting `USE_SYNCER=1` is reserved for explicitly disabling fetching in A0. Earlier adapter revisions reversed this mapping, so their recorded `enable_synchronizer=true` does not establish that fetching ran. The launcher correction works with existing binaries; experiments must be rerun to assess progress. Changing these experiments to paper variants or a different leader protocol would be a separate change.

## Interpreting results

Retain per-replica scheduling and local decision events. For each chosen slot, distinguish actual support for each digest, direct/indirect commit, skip and the duration of being unresolved. Measure recovery after the configured attack window. Report queue overflow, failed/infeasible fork generation, additional fork bytes and configuration alongside throughput and latency.

Use identical Byzantine identities, seed, load, duration, hardware and recovery windows across modes. A2–A5 repeat transaction statements in alternate proposals: report unique committed transaction identifiers where available, and do not interpret extra DAG blocks as additional useful client throughput. A5 belongs to a stronger network-adversary model than A0–A4 and should be labeled separately in paper figures.

See [validation record](adversary-validation.md) for the completed checks. Rust regressions include an opt-in real-crypto scope because legacy DAG unit fixtures use zero digests. Compilation checks include test targets, but do not execute those tests or demonstrate distributed attack outcomes. No cloud experiment or deployment was performed.
