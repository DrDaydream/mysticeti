"""Replay raw fixed windows and keep the original scrape format readable."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_matrix as matrix
import plot_figures


class ClockWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = next(matrix.cases('Mysticeti', 'A3'))
        self.archive = self.root / matrix.case_key(self.case) / 'attempt-test'
        self.native = self.archive / 'native'
        self.native.mkdir(parents=True)
        shutil.copy2(Path(__file__).parent / 'fixtures/mysticeti_metrics.py',
                     self.archive / 'measurement_parser.py')
        self.plan = dict(schema=2, run_id='run-test', load_ms=100000,
                         start_ms=100000, end_ms=160000)
        self.record = dict(status='complete', run_id='run-test', commit='test-commit',
                           timing_schema=2, timeline=self.plan,
                           parameters=dict(nodes=10, rate=10000, duration=60,
                                           warmup=0, faults=0, runs=1),
                           adversary=dict(mode='A3', byzantine_authorities=[0, 1, 2],
                                          start_after_ms=20000, duration_ms=30000))
        self.windows = {str(i): dict(self.plan, authority=i, complete=True,
                                    count=120, sum_s=240.0, input_lateness_ms=10)
                        for i in range(10)}
        self.receipts = {str(i): dict(self.plan, authority=i) for i in range(10)}
        self.windows['0'].update(count=600, sum_s=60000)  # excluded adversary
        self.windows['9'].update(count=0, sum_s=0)
        self.write()

    def write(self):
        for name, data in [('run.json', self.record), ('windows.json', self.windows),
                           ('armed.json', self.receipts)]:
            matrix.save(self.native / name, data)
        # Progress samples and summary are intentionally irrelevant to final results.
        (self.native / 'samples.jsonl').write_text('{"phase":"sample-0000","nodes":{}}\n')
        matrix.save(self.native / 'summary.json', {'mean_latency_ms': -12345})

    def test_schema2_raw_windows_honest_aggregation_and_fixed_duration(self):
        result = matrix.shared_measurement(self.archive, self.case)
        self.assertEqual(result['latency_ms'], 2000)
        self.assertEqual(result['latency_contributors'], 6)
        self.assertEqual(result['honest_replicas'], 7)
        self.assertEqual(result['zero_commit_replicas'], [9])
        self.assertEqual(result['timing_schema'], 2)
        self.assertIn('common measurement start', result['attack_window'])
        self.assertEqual(result['replicas'][3]['tps'], 2)
        self.assertEqual(result['replicas'][9]['tps'], 0)
        self.assertEqual({r['duration_s'] for r in result['replicas']}, {60})

    def test_all_zero_is_complete_but_latency_is_none(self):
        for row in self.windows.values():
            row.update(count=0, sum_s=0)
        self.write()
        result = matrix.shared_measurement(self.archive, self.case)
        self.assertIsNone(result['latency_ms'])
        self.assertEqual(result['zero_commit_replicas'], list(range(3, 10)))

    def test_bad_snapshots_rejected(self):
        for key, value in [('authority', 1), ('run_id', 'stale'), ('end_ms', 160001),
                           ('schema', True), ('complete', False), ('count', -1),
                           ('count', 1.5), ('count', True), ('sum_s', -1),
                           ('sum_s', float('nan')), ('sum_s', float('inf')),
                           ('input_lateness_ms', None), ('input_lateness_ms', 501)]:
            with self.subTest(key=key, value=value):
                bad = copy.deepcopy(self.windows)
                bad['0'][key] = value
                (self.native / 'windows.json').write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    matrix.shared_measurement(self.archive, self.case)

    def test_missing_window_or_receipt_rejected(self):
        for name, data in [('windows.json', self.windows), ('armed.json', self.receipts)]:
            with self.subTest(name=name):
                self.write()
                matrix.save(self.native / name, {k: v for k, v in data.items() if k != '9'})
                with self.assertRaises(ValueError):
                    matrix.shared_measurement(self.archive, self.case)

    def test_timeline_must_match_run_and_requested_duration(self):
        for key, value in [('run_id', 'stale'), ('end_ms', 159000),
                           ('start_ms', 101000), ('load_ms', -1), ('schema', True)]:
            with self.subTest(key=key):
                self.write()
                record = copy.deepcopy(self.record)
                record['timeline'][key] = value
                matrix.save(self.native / 'run.json', record)
                with self.assertRaises(ValueError):
                    matrix.shared_measurement(self.archive, self.case)

    def test_unknown_schema_and_incomplete_run_rejected(self):
        for field, value in [('timing_schema', 3), ('timing_schema', True),
                             ('status', 'failed'), ('cleanup_errors', ['failed'])]:
            with self.subTest(field=field):
                matrix.save(self.native / 'run.json', dict(self.record, **{field: value}))
                with self.assertRaises(ValueError):
                    matrix.shared_measurement(self.archive, self.case)

    def test_legacy_mysticeti_and_mahi_keep_scrape_deltas(self):
        for protocol in ('Mysticeti', 'Mahi-Mahi'):
            for explicit in (False, True):
                with self.subTest(protocol=protocol, explicit=explicit):
                    record = copy.deepcopy(self.record)
                    record.pop('timing_schema')
                    record.pop('timeline')
                    if explicit:
                        record['timing_schema'] = 1
                    matrix.save(self.native / 'run.json', record)
                    before = {str(i): dict(metrics=dict(count=10, sum=20), monotonic=100)
                              for i in range(10)}
                    after = {str(i): dict(metrics=dict(count=130, sum=260), monotonic=162)
                             for i in range(10)}
                    (self.native / 'samples.jsonl').write_text('\n'.join(json.dumps(
                        dict(phase=phase, nodes=nodes)) for phase, nodes in
                        [('before', before), ('after', after)]))
                    result = matrix.shared_measurement(self.archive, dict(self.case, protocol=protocol))
                    self.assertEqual(result['latency_ms'], 2000)
                    self.assertEqual(result['replicas'][0]['tps'], 120 / 62)
                    self.assertIn('after each node initializes', result['attack_window'])

    def test_evidence_is_archived(self):
        dest = self.root / 'copied'
        matrix.copy_native(self.native, dest)
        self.assertEqual(matrix.read(dest / 'windows.json'), self.windows)
        self.assertEqual(matrix.read(dest / 'armed.json'), self.receipts)

    def test_csv_records_the_timing_basis(self):
        result = matrix.shared_measurement(self.archive, self.case)
        result.update(case=self.case, status='complete')
        matrix.save(self.archive / 'measurement.json', result)
        matrix.save(self.archive.parent / 'selected.json', dict(
            case=self.case, status='complete', attempt=self.archive.name))
        rows = plot_figures.export_data(self.root)
        row = next(row for row in rows if row['status'] == 'complete')
        self.assertEqual(row['timing_schema'], 2)
        self.assertEqual(row['attack_window'], result['attack_window'])
        self.assertEqual(row['window_basis'], result['window_basis'])


if __name__ == '__main__':
    unittest.main()
