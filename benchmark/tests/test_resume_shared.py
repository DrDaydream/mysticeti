import importlib.util
import json
from pathlib import Path
import pytest


def load():
    path = Path(__file__).resolve().parents[1] / 'scripts/resume_shared.py'
    spec = importlib.util.spec_from_file_location('resume_shared', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mysticeti_error_defers_remaining_work_until_after_mahi(tmp_path):
    module = load()
    events = []
    attempts = {}

    def run(protocol):
        attempts[protocol] = attempts.get(protocol, 0) + 1
        events.append(protocol)
        if protocol == 'Mysticeti' and attempts[protocol] == 1:
            raise RuntimeError('failed n35/r90300')

    assert module.schedule(tmp_path / 'schedule.json', run, lambda _: True) == 0
    assert events == ['Mysticeti', 'Mahi-Mahi', 'Mysticeti']
    state = json.loads((tmp_path / 'schedule.json').read_text())
    assert state['phase'] == 'done'
    assert state['first_mysticeti_error'] == 'failed n35/r90300'


def test_success_has_no_extra_retry(tmp_path):
    events = []
    assert load().schedule(tmp_path / 'schedule.json', events.append, lambda _: True) == 0
    assert events == ['Mysticeti', 'Mahi-Mahi']


def test_failed_cleanup_blocks_next_pool(tmp_path):
    events = []
    def run(protocol):
        events.append(protocol)
        raise RuntimeError('cleanup failed')
    with pytest.raises(RuntimeError):
        load().schedule(tmp_path / 'schedule.json', run, lambda _: False)
    assert events == ['Mysticeti']


def test_interrupt_never_starts_mahi(tmp_path):
    events = []
    def run(protocol):
        events.append(protocol)
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        load().schedule(tmp_path / 'schedule.json', run, lambda _: True)
    assert events == ['Mysticeti']


def test_mahi_error_resumes_mahi_before_returning_to_mysticeti(tmp_path):
    events = []
    def fail(protocol):
        events.append(protocol)
        raise RuntimeError(protocol + ' failed')
    module = load()
    state = tmp_path / 'schedule.json'
    with pytest.raises(RuntimeError, match='Mahi-Mahi'):
        module.schedule(state, fail, lambda _: True)
    assert json.loads(state.read_text())['phase'] == 'mahi'
    assert module.schedule(state, events.append, lambda _: True) == 0
    assert events == ['Mysticeti', 'Mahi-Mahi', 'Mahi-Mahi', 'Mysticeti']


def test_second_mysticeti_failure_stops_instead_of_looping_forever(tmp_path):
    events = []
    def run(protocol):
        events.append(protocol)
        if protocol == 'Mysticeti':
            raise RuntimeError('still failed')
    with pytest.raises(RuntimeError, match='still failed'):
        load().schedule(tmp_path / 'schedule.json', run, lambda _: True)
    assert events == ['Mysticeti', 'Mahi-Mahi', 'Mysticeti']


def test_empty_error_message_still_requires_mysticeti_retry(tmp_path):
    events = []
    def run(protocol):
        events.append(protocol)
        if len(events) == 1:
            raise RuntimeError()
    assert load().schedule(tmp_path / 'schedule.json', run, lambda _: True) == 0
    assert events == ['Mysticeti', 'Mahi-Mahi', 'Mysticeti']


@pytest.mark.parametrize('field', ['commit', 'settings_sha256', 'fabfile_sha256'])
def test_changed_mahi_identity_cannot_silently_continue_old_cases(field):
    module = load()
    old = dict(commit='original-mahi', settings_sha256='settings', fabfile_sha256='fabfile',
               wrapper_sha256=module.ORIGINAL_TOOLS['run_matrix.py'],
               lifecycle_sha256=module.ORIGINAL_TOOLS['lifecycle.py'],
               protocol_tasks_sha256=module.ORIGINAL_TOOLS['protocol_tasks.py'])
    module.validate_identity('Mahi-Mahi', old, old)
    changed = dict(old, **{field: 'modified'})
    with pytest.raises(ValueError):
        module.validate_identity('Mahi-Mahi', changed, old)
