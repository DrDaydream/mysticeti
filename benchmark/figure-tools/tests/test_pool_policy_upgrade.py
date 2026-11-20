"""Changed measurement duration/header threshold must start a separate campaign."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_matrix as matrix


class CampaignCompatibilityTests(unittest.TestCase):
    def test_current_unchanged_campaign_can_resume_without_rewriting_archives(self):
        current = {'Orca-A': dict(commit='current', wrapper_sha256='v33', protocol_tasks_sha256='v33-tasks')}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / 'old-result.txt'
            sentinel.write_text('retained')
            self.assertEqual(matrix.upgrade_pool_policy(root, current, current), current)
            self.assertEqual(sentinel.read_text(), 'retained')
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_old_or_modified_campaign_is_rejected_even_with_upgrade_flag(self):
        current = {'Orca-A': dict(commit='current', settings_sha256='settings', wrapper_sha256='v33',
                                 protocol_tasks_sha256='v33-tasks')}
        for field in current['Orca-A']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                old = copy.deepcopy(current)
                old['Orca-A'][field] = 'old'
                matrix.save(root / 'campaign.json', old)
                original = (root / 'campaign.json').read_bytes()
                with self.assertRaisesRegex(ValueError, 'new --output'):
                    matrix.upgrade_pool_policy(root, old, current)
                self.assertEqual((root / 'campaign.json').read_bytes(), original)
                self.assertEqual(len(list(root.iterdir())), 1)

    def test_new_campaign_records_all_requested_protocols(self):
        current = {name: dict(commit='current') for name in matrix.PROTOCOLS}
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(matrix.upgrade_pool_policy(Path(temporary), {}, current), current)

    def test_unknown_remote_runner_is_rejected_before_allocating_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            bench = Path(temporary)
            (bench / 'benchmark').mkdir()
            (bench / 'benchmark/remote.py').write_text('def run(): install()\n')
            with self.assertRaises(ValueError):
                matrix.require_prebuilt_remote('Orca-A', bench)
            matrix.require_prebuilt_remote('Mysticeti', bench)


if __name__ == '__main__':
    unittest.main()
