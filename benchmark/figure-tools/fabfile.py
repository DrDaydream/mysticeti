"""Separate serial Fabric entry point; the five repositories' fabfiles stay intact."""
from pathlib import Path
import subprocess
import sys

from fabric import task
from run_matrix import DEFAULT_OUTPUT

TOOLS = Path(__file__).resolve().parent


@task(help={
    'protocol': 'all, Orca-A, Orca-B, Bullshark, Mysticeti or Mahi-Mahi',
    'mode': 'all, A0, A1, A3 or A5',
    'dry-run': 'Print the plan without contacting AWS or running benchmarks.',
    'retry-failed': 'Retain the previous failed attempt and retry its configuration.',
    'upgrade-pool-policy': 'Compatibility check only; changed experiment parameters require a new output directory.',
})
def matrix(ctx, protocol='all', mode='all', root='/home/ubuntu/experiments',
           output=DEFAULT_OUTPUT, dry_run=False,
           retry_failed=False, upgrade_pool_policy=False):
    """Per protocol and node count: exact-size create -> install -> experiments -> destroy."""
    command = [sys.executable, str(TOOLS / 'run_matrix.py'),
               '--protocol', protocol, '--mode', mode, '--root', root,
               '--output', output]
    if dry_run:
        command.append('--dry-run')
    if retry_failed:
        command.append('--retry-failed')
    if upgrade_pool_policy:
        command.append('--upgrade-pool-policy')
    subprocess.run(command, check=True)


@task
def plot(ctx, output=DEFAULT_OUTPUT):
    """Rebuild CSV and figures from saved measurements; no AWS access."""
    subprocess.run([sys.executable, str(TOOLS / 'plot_figures.py'), output], check=True)


@task
def cleanup(ctx, protocol='all', root='/home/ubuntu/experiments',
            output=DEFAULT_OUTPUT):
    """Clean recorded unfinished batch pools; no instances are created."""
    subprocess.run([sys.executable, str(TOOLS / 'run_matrix.py'), '--cleanup-only',
                    '--protocol', protocol, '--root', root, '--output', output], check=True)
