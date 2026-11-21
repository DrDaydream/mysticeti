# Validation of Mysticeti upstream alignment

Validated locally on 2026-11-21. No EC2, SSH, or AWS experiment was run.

- Baseline Python suite at `a80b5ed`: 180 passed.
- New cumulative-summary and upstream launch expectations first failed against the old implementation (34 failures, 2 passed); after implementation the focused suite passed 36 tests.
- Complete benchmark Python suite after implementation: **237 passed**. This includes actual generated Bash environment checks with inherited USE_SYNCER unset/0/1; mocked live launch/readiness/scrape/cleanup/log download for A0, crash A1, A3, A5; mixed and total zero observations; cumulative vs delta and old archive handling; corrupted metric/source/basis/log/launch rejection; independent selected-protocol source guards; and idempotent toolkit preparation.
- The toolkit covers 96 Mysticeti and 96 Mahi cases, with 480 total rows. Dry-run produces 96 Mysticeti remote commands and no cloud resources. Mahi remains pinned to `3c7d32fa815defca8ab82129a461d5a417082e7b` and retains its already delivered parameters.
- Original lifecycle.py is byte-identical, SHA-256 `1b5664d2ef967d3db898ab5281962f54f92a46e8055be3ef2b0d6c2a518035ee`; cleanup-on-first-failure behavior is retained.
- WSL Ubuntu, Rust **1.72.1**: `cargo check --locked -p mysticeti` passed with `RUSTFLAGS=--cfg tokio_unstable`.
- `bash -n benchmark/scripts/run_mysticeti_upstream.sh` passed.
- Twenty-four synthetic offline archive fixtures exercise all four modes, all three sizes, and zero/nonzero results. CSV exports contain 480 rows and 24 completed fixtures; four PNGs and four PDFs rendered. The A1 PNG was visually checked for labels, cumulative statistics, crash semantics, and zero-observation annotations. These fixtures are test data, not benchmark measurements.
- `git diff a80b5ed -- mysticeti-core mysticeti orchestrator` is empty. Rust protocol, adversary source, zero-metric initialization, and E2E timestamps have not been edited in this delivery. The generator and handler source match upstream `68d9038f4055e67ced4ae96c1de46b9b0239413b`.

The prior paused CI/check.sh work is outside this branch. Passing the focused Rust build and benchmark tests is not a claim that the unrelated historical full check.sh workflow is repaired, nor that EC2 E2E performance has been measured.
