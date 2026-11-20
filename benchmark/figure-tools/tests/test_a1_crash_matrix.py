"""A1 process crashes must not be confused with the earlier runtime silence."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_matrix as matrix


class CrashMatrixTests(unittest.TestCase):
    def test_source_check_rejects_old_or_partial_updates_before_aws(self):
        import hashlib
        for name in ('Orca-A', 'Orca-B', 'Bullshark'):
            self.assertIn('benchmark/benchmark/remote.py', matrix.NATIVE_SOURCE_SHA256[name])
        self.assertIn('primary/src/adversary.rs', matrix.NATIVE_SOURCE_SHA256['Orca-B'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bench = root / 'benchmark'
            path = bench / 'benchmark/remote.py'
            path.parent.mkdir(parents=True)
            path.write_text('old runtime silence\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                matrix.require_prebuilt_remote('Orca-A', bench)
            reviewed = 'reviewed runner\n'
            expected = {'Orca-A': {'benchmark/benchmark/remote.py': hashlib.sha256(reviewed.encode()).hexdigest()}}
            path.write_bytes(reviewed.replace('\n', '\r\n').encode())
            with patch.object(matrix, 'NATIVE_SOURCE_SHA256', expected):
                matrix.require_prebuilt_remote('Orca-A', bench)

    def test_three_legacy_a1_plans_omit_last_ids_and_plot_actual_configured_load(self):
        for protocol in ('Orca-A', 'Orca-B', 'Bullshark'):
            case = next(matrix.cases(protocol, 'A1'))
            self.assertEqual(case['fault_model'], 'crash-stop')
            self.assertEqual(case['byzantine_ids'], [7, 8, 9])
            self.assertEqual(case['input_rate_tps'], 10000)
            self.assertEqual(case['offered_rate_tps'], 7000)
            if protocol == 'Bullshark':
                command = matrix.command(case)
                self.assertEqual(command[command.index('--bullshark-alternating') + 1], 'false')

    def test_a3_a5_keep_all_clients_and_bullshark_alternation(self):
        for mode in ('A3', 'A5'):
            case = next(matrix.cases('Bullshark', mode))
            self.assertEqual(case['byzantine_ids'], [0, 1, 2])
            self.assertEqual(case['offered_rate_tps'], 10000)
            command = matrix.command(case)
            self.assertEqual(command[command.index('--bullshark-alternating') + 1], 'true')
        self.assertEqual(next(matrix.cases('Orca-B', 'A3'))['aba_scheduling'], 'A5-only')

    def test_previous_a1_results_and_snapshots_cannot_be_reused(self):
        case = next(matrix.cases('Orca-A', 'A1'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / matrix.case_key(case)
            target.mkdir(parents=True)
            old = dict(case)
            old.pop('fault_model', None)
            matrix.save(target / 'selected.json', dict(case=old, status='complete'))
            with patch.object(matrix.subprocess, 'Popen', side_effect=AssertionError('must reject')):
                with self.assertRaises(ValueError):
                    matrix.run_case(case, Path('unused'), root)
        snapshot = dict(nodes=10, rate=10000, adversary=dict(mode='silent'),
                        benchmark_parameters=dict(duration=60), node_parameters=dict(header_size=20))
        with self.assertRaises(ValueError):
            matrix.validate_legacy_snapshot(snapshot, case)


if __name__ == '__main__':
    unittest.main()
