"""Exercise real lifecycle grouping with an in-memory replacement for AWS calls."""
from collections import Counter
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lifecycle
import run_matrix as matrix


class NodePoolTests(unittest.TestCase):
    def setup_bench(self, temporary, region_count=5):
        bench = Path(temporary) / 'benchmark'
        bench.mkdir(exist_ok=True)
        matrix.save(bench / 'settings.json', {'instances': {'regions': [f'region-{i}' for i in range(region_count)]}})
        return bench, Path(temporary) / 'results'

    def emulator(self, events, fail=None):
        """Replace only Fabric/AWS task boundaries; keep state and grouping real."""
        pools = {}
        def task(phase, name, *args):
            scope = phase.record['scope']
            pools.setdefault(scope, [])
            events.append((name, phase.record['total_instances'], scope))
            if name == 'inventory':
                matrix.save(Path(args[1]), {'scope': scope, 'instances': pools[scope], 'security_groups': []})
            elif name == 'create':
                self.assertEqual(args[0], '--nodes')
                count = int(args[1])
                pools[scope] = [dict(region=region, instance_id=f'{scope}-{region}-{i}', state='running', ip=f'192.0.2.{i+1}')
                                for region in phase.record['regions'] for i in range(count)]
                if fail == ('create', phase.record['total_instances']):
                    raise RuntimeError('simulated partial create failure')
            elif name == 'install':
                if fail == ('install', phase.record['total_instances']):
                    raise RuntimeError('simulated install failure')
            elif name == 'destroy':
                if fail == ('destroy', phase.record['total_instances']):
                    raise RuntimeError('simulated destroy failure')
                pools[scope] = []
        def run(case, bench, output, retry_failed=False, lifecycle=None):
            active = pools[lifecycle.record['scope']]
            self.assertEqual(len(active), case['nodes'], 'Pool must contain exactly the committee size')
            events.append(('remote', case['nodes'], lifecycle.record['scope']))
            if fail == ('remote', case['nodes']):
                raise RuntimeError('simulated remote failure')
        return task, run

    def test_each_size_creates_installs_runs_32_and_destroys_for_every_protocol(self):
        for name in matrix.PROTOCOLS:
            with self.subTest(protocol=name), tempfile.TemporaryDirectory() as temporary:
                bench, output = self.setup_bench(temporary)
                events = []
                task, run = self.emulator(events)
                with patch.object(lifecycle.ProtocolLifecycle, '_task', task):
                    lifecycle.run_protocol(list(matrix.cases(name)), bench, output, run)
                significant = [(kind, count) for kind, count, scope in events if kind != 'inventory']
                expected = []
                for size in (10, 20, 35):
                    expected += [('create', size)]
                    if name not in ('Mysticeti', 'Mahi-Mahi'):
                        expected += [('ready', size)]
                    expected += [('install', size)] + [('remote', size)] * 32 + [('destroy', size)]
                self.assertEqual(significant, expected)
                scopes = [scope for kind, _, scope in events if kind == 'create']
                self.assertEqual(len(set(scopes)), 3)
                self.assertEqual(matrix.read(output / 'lifecycle' / name / 'state.json')['status'], 'terminated')

    def test_completed_size_is_skipped_and_partial_size_runs_only_remaining_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            bench, output = self.setup_bench(temporary)
            plan = list(matrix.cases('Orca-A'))
            done = [case for case in plan if case['nodes'] == 10]
            done += [case for case in plan if case['nodes'] == 20][:5]
            for case in done:
                target = output / matrix.case_key(case) / 'selected.json'
                target.parent.mkdir(parents=True)
                matrix.save(target, {'case': case, 'status': 'complete'})
            events = []
            task, run = self.emulator(events)
            with patch.object(lifecycle.ProtocolLifecycle, '_task', task):
                lifecycle.run_protocol(plan, bench, output, run)
            self.assertEqual([n for kind, n, _ in events if kind == 'create'], [20, 35])
            self.assertEqual(Counter(n for kind, n, _ in events if kind == 'remote'), {20: 27, 35: 32})

    def test_failed_second_size_cleans_up_and_never_starts_third(self):
        for failure in ('create', 'install', 'remote', 'destroy'):
            with self.subTest(stage=failure), tempfile.TemporaryDirectory() as temporary:
                bench, output = self.setup_bench(temporary)
                events = []
                task, run = self.emulator(events, fail=(failure, 20))
                with patch.object(lifecycle.ProtocolLifecycle, '_task', task):
                    with self.assertRaisesRegex(RuntimeError, 'simulated'):
                        lifecycle.run_protocol(list(matrix.cases('Orca-A')), bench, output, run)
                self.assertEqual([n for kind, n, _ in events if kind == 'create'], [10, 20])
                self.assertIn(('destroy', 20), [(kind, n) for kind, n, _ in events])
                record = matrix.read(output / 'lifecycle/Orca-A/state.json')
                self.assertEqual(record['status'], 'cleanup_failed' if failure == 'destroy' else 'terminated')

    def test_inexact_region_layout_is_rejected_before_any_pool_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            bench, output = self.setup_bench(temporary, region_count=2)
            events = []
            task, run = self.emulator(events)
            with patch.object(lifecycle.ProtocolLifecycle, '_task', task):
                with self.assertRaisesRegex(ValueError, 'exact|Exact|divis'):
                    lifecycle.run_protocol(list(matrix.cases('Orca-A')), bench, output, run)
            self.assertFalse(events, 'Invalid 35-node layout must fail before creating the 10-node pool')

    def test_exact_inventory_is_required_and_oversized_pool_cannot_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            bench, output = self.setup_bench(temporary)
            phase = lifecycle.ProtocolLifecycle('Orca-A', bench, output)
            phase.begin(10)
            self.assertEqual(phase.record['total_instances'], 10)
            self.assertEqual(phase.record['nodes_per_region'], 2)
            extra = [dict(region=f'region-{r}', state='running', ip='192.0.2.1') for r in range(5) for _ in range(7)]
            inventories = iter([{'instances': [], 'security_groups': []}] + [{'instances': extra}] * 12)
            with patch.object(phase, '_inventory', side_effect=lambda _: next(inventories)), patch.object(phase, '_task'), patch.object(lifecycle.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'incomplete|not ready'):
                    phase.create()

    def test_plan_orders_node_counts_before_modes_without_changing_case_definitions(self):
        cases = list(matrix.cases('Orca-A'))
        self.assertEqual([case['nodes'] for case in cases], [10]*32 + [20]*32 + [35]*32)
        self.assertEqual(Counter((c['nodes'], c['mode']) for c in cases), {(n, a): 8 for n in (10, 20, 35) for a in ('A0', 'A1', 'A3', 'A5')})
        self.assertEqual(len(list(matrix.cases())), 480)


if __name__ == '__main__':
    unittest.main(verbosity=2)
