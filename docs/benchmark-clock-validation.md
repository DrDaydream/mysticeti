# Benchmark clock and matrix validation

Source baseline: `b813f80705c3fb9ef240e2b319fd1675ed0be91a`.

The common-clock change initializes shared metrics without fake observations,
waits for validator runtime readiness, arms a shared input/measurement timeline,
and freezes per-node commit-window counts and E2E sums. The matrix adapter reads
these windows and retains the older scrape-delta format for schema 1.

Checks completed before publication:

- Native Fabric adapter: `python -m pytest tests -q` — 180 passed.
- Bundled matrix: `python -m unittest discover -s tests -v` — 28 passed.
- Ubuntu WSL2, Rust 1.72.1: `cargo test --locked -p mysticeti-core` — 69 passed,
  one pre-existing ignored test; doc tests passed.
- Ubuntu WSL2, Rust 1.72.1: `cargo build --locked -p mysticeti` — passed (debug
  build; this is not a release-build or AWS performance result).
- Previously executed local four-process checks: one validator starts 25 seconds
  before the other three; all wait for the common plan, produce complete frozen
  windows, and have input start lateness within 500 ms. A separate zero-input
  run produces zero counts and sums. Frozen files remain unchanged after closure.
- Replaying both saved four-process window sets through the matrix adapter
  matches the native window summarizer exactly, including zero TPS / E2E N/A.
- The original-toolkit updater preserves all five original runtime files in a
  backup, updates the two compatibility files, accepts an identical repeated
  update, and rejects unknown local edits before writing.
- Mysticeti A3 dry-run enumerates 24 cases without contacting AWS.
- Independent reviews covered the native clock, matrix adapter and packaging.

The simulator feature build has a baseline failure involving a module gated by
`cfg(test)`; the same failure was reproduced on the clean baseline. This feature
was not repaired as part of the benchmark-clock work. Default-feature core tests
and the validator binary build above passed.

No EC2 update, 35-validator distributed run, or AWS result was produced by these
publication checks. Use a new campaign directory: schema 2 changes the attack
origin and measured window, so it must not silently continue a schema 1 batch.
