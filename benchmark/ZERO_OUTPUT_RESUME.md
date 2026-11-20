# Original-window resume with zero output

This branch starts from `b813f80705c3fb9ef240e2b319fd1675ed0be91a`.
It does not include the later common-start, arming or schema-2 window patch.
The only validator change initializes the `latency_s{workload="shared"}`
histogram child. Prometheus then exports real zero count/sum values before the
first shared commit. Initialization does not add a zero-latency observation.
Consensus, transactions, adversary behavior and timing remain as in the base.

The original native runner already accepts zero counters. Every active replica
still needs a live tmux session and successful, valid metrics scrapes. A valid
zero-commit window contributes 0 TPS and has undefined (`N/A`) latency; it is
recorded as a completed experiment, not as a successful-progress claim. Missing
metrics, bad counters and failed scrapes are not converted to zero.

`scripts/restore_original_v35.py` restores the original five runtime tool files
from the pinned, SHA256-checked v3.5 package and finds the exact original settings
backup using the old campaign hash. It retains copies of the replaced files.
It performs no AWS work and does not modify Git history or result archives.

`scripts/resume_shared.py` calls the original v3.5 `run_protocol` and `run_case`
functions. The parameters remain **60s duration, no warmup, attack offset 20s and
attack duration 30s**. In this version each validator's initialization is the
attack clock origin; the 60s measurement starts after readiness. A common
measurement-relative attack origin is deliberately not reintroduced.

The previous output defaults to
`/home/ubuntu/experiments/paper-results-v3.5-60s-h20-crash-a1`.
Completed Mysticeti/Mahi-Mahi cases, including valid zero-output cases, are
referenced and skipped. Incomplete, failed and unstarted cases are measured
again from scratch. Old measurements are not rewritten or relabeled with the
new source commit. The new output defaults to
`/home/ubuntu/experiments/paper-results-v3.5-60s-zero-output-resume` and records
the original-completion references, source/settings/tool identities and phase.
The generated plots contain new results; prior measurements remain in their
original directory.

Before allocating resources, the continuation verifies the original campaign
hash, original settings and native Fab entry points. Mahi-Mahi must retain its
original source commit. Mysticeti must be based on b813f80 with exactly the
reviewed zero-export validator change and supporting resume files. Its configured
source directory must be the controller checkout, so the original install task
builds the selected commit rather than a separate checkout at an old repo.ref.

Execution is strictly serial under the existing controller-wide matrix lock:

1. Run all pending Mysticeti cases until complete or the first error.
2. After successful pool cleanup, run all pending Mahi-Mahi cases.
3. If Mysticeti previously failed, return to its pending cases, retrying the
   failed case and continuing the rest. A further error stops this invocation.

Completed new cases are skipped by the original lifecycle runner on every
retry. Repeating the same command resumes the saved phase. A Mahi-Mahi failure
stops at that phase; it must complete before deferred Mysticeti work resumes.
Ctrl+C/SIGTERM interrupts without advancing to another protocol. Cleanup failure
also stops, because another pool must not run before the previous pool is clear.

After stopping the old foreground matrix and allowing its cleanup to finish,
use that version's `--cleanup-only` for any recorded old/clock campaign pools.
Then fetch and select this branch, restore the original toolkit and run:

```bash
cd /home/ubuntu/experiments/mysticeti
python3 benchmark/scripts/restore_original_v35.py
python3 benchmark/scripts/resume_shared.py --dry-run
python3 benchmark/scripts/resume_shared.py
```

The original lifecycle creates each exact-size pool, installs the checked-out
source on the controller and those hosts, measures pending cases, archives
evidence and destroys the pool. A separate manual `fab install` is unnecessary.
No EC2 experiment has been performed by the local regression suite.
