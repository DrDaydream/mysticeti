"""The runnable plan and archived observations must use the requested experiment."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_matrix as matrix
import plot_figures


class ExperimentParameterTests(unittest.TestCase):
    def test_all_remote_commands_use_sixty_seconds_and_record_header_sizes(self):
        plan = list(matrix.cases())
        self.assertEqual(len(plan), 480)
        for case in plan:
            command = matrix.command(case, 'effective-settings.json')
            self.assertEqual(command[command.index('--duration') + 1], '60')
            self.assertEqual(case['duration_s'], 60)
            self.assertEqual(case['header_size_bytes'], None if matrix.shared(case) else 20)

    def test_shared_protocol_settings_use_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'settings.json'
            matrix.save(source, {'benchmark': {'duration': 100, 'adversary': {
                'start_after_ms': 20000, 'duration_ms': 60000}}})
            for name in ('Mysticeti', 'Mahi-Mahi'):
                case = next(matrix.cases(name, 'A1'))
                params = matrix.effective_settings(source, case)['benchmark']
                self.assertEqual(params['duration'], 60)
                self.assertNotIn('header_size', params)
                self.assertEqual(params['adversary']['start_after_ms'], 20000)
                self.assertEqual(params['adversary']['duration_ms'], 30000)
                self.assertEqual(case['attack_start_after_ms'], 20000)
                self.assertEqual(case['attack_duration_ms'], 30000)

    def test_archived_shared_window_must_match_the_plan(self):
        for name in ('Mysticeti', 'Mahi-Mahi'):
            case = next(matrix.cases(name, 'A1'))
            with self.subTest(protocol=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary)
                (archive / 'native').mkdir()
                (archive / 'native/samples.jsonl').write_text('', encoding='utf-8')
                record = dict(status='complete', parameters=dict(
                    nodes=10, rate=10000, duration=60, warmup=0, faults=0, runs=1),
                    adversary=dict(mode='A1', byzantine_authorities=[0, 1, 2],
                                   start_after_ms=20000, duration_ms=30000))
                for field, wrong in (('duration_ms', 60000), ('start_after_ms', 0)):
                    changed = copy.deepcopy(record)
                    changed['adversary'][field] = wrong
                    matrix.save(archive / 'native/run.json', changed)
                    with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'adversary window'):
                        matrix.shared_measurement(archive, case)

    def test_old_completed_case_is_not_skipped_as_a_new_experiment(self):
        case = next(matrix.cases('Orca-A', 'A1'))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / matrix.case_key(case)
            directory.mkdir(parents=True)
            old_case = dict(case, duration_s=100)
            old_case.pop('header_size_bytes', None)
            matrix.save(directory / 'selected.json', dict(case=old_case, status='complete'))
            with patch.object(matrix.subprocess, 'Popen', side_effect=AssertionError('must reject before running')):
                with self.assertRaisesRegex(ValueError, 'configuration differs'):
                    matrix.run_case(case, Path('unused'), root)
            with self.assertRaisesRegex(ValueError, 'Configuration mismatch'):
                plot_figures.export_data(root)

    def test_archived_native_settings_must_match_duration_and_header_size(self):
        case = next(matrix.cases('Orca-A', 'A1'))
        snapshot = dict(nodes=10, rate=10000, adversary=dict(mode='silent', fault_model='crash-stop', byzantine_ids=[7, 8, 9], bullshark_alternating=False),
                        fault_model='crash-stop', active_launch_ids=list(range(7)),
                        inactive_launch_ids=[7, 8, 9], offered_rate_tps=7000,
                        benchmark_parameters=dict(duration=60), node_parameters=dict(header_size=20))
        matrix.validate_legacy_snapshot(snapshot, case)
        for key, field, wrong in (('benchmark_parameters', 'duration', 100),
                                  ('node_parameters', 'header_size', 1000)):
            changed = copy.deepcopy(snapshot)
            changed[key][field] = wrong
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'snapshot'):
                matrix.validate_legacy_snapshot(changed, case)


if __name__ == '__main__':
    unittest.main()
