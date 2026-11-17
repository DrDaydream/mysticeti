# A0–A5 validation record

Validation date: 2026-11-17. Source baseline: `68d9038f4055e67ced4ae96c1de46b9b0239413b`.

The user authorized one compilation check after implementation. In a temporary Ubuntu 24.04 amd64 WSL2 environment, using the repository's Rust 1.72.1 toolchain, the following check passed with exit code 0:

```bash
RUSTFLAGS='-C target-cpu=native' \
CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 \
cargo +1.72.1 check --locked -p mysticeti-core -p mysticeti --tests
```

Cargo reported `Finished dev [unoptimized] target(s) in 2m 38s`. The check includes normal library/binary code and test-target type checking. It does not execute Rust tests, link a release executable, run the simulator feature or validate multi-node attack results. `Cargo.lock` was preserved.

Other completed checks:

- Fabric adapter Python suite: **130 passed**.
- Shell syntax checks for install, source sync and build scripts: passed.
- Python syntax and public example settings validation: passed.
- Direct Rust formatting/parser checks and Git whitespace checks: passed.
- Independent source review and scoped re-review: completed. Corrected WAL tag-6 lazy loading, atomic fork-pair publication, active vote-target priority and the earliest eligible future-anchor round.

The Rust regressions cover byte-identical baseline proposals, valid distinct signatures/digests with identical payload, distinct-author quorum accounting, DAG-directed parent/recipient choices, consecutive leader targets, rolling/fixed targets, exact-digest fetching, finite alternate release, A5 own/forward/fetch delivery, queue bounds and cancellation cleanup. A storage regression covers fork persistence, the primary cursor and WAL replay. Legacy test fixtures retain their default zero-digest behavior; these regressions explicitly opt into production crypto in a synchronous thread-local scope.

No cloud instance, distributed experiment or deployment was started. Throughput, latency, commit/skip outcomes and recovery measurements must come from subsequent runs; the presence of a scheduling mode is not evidence that it prevents commitment.
