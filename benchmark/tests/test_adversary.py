import copy
import json

import pytest

from mysticeti_aws.settings import DEFAULTS, load_settings, validate_benchmark


def params(**changes):
    value = copy.deepcopy(DEFAULTS['benchmark'])
    value.update(changes)
    return value


def test_active_mode_selects_fixed_byzantine_ids_and_measurement_start():
    actual = validate_benchmark(params(adversary={'mode': 'A3'}))
    assert actual['adversary'] == {
        'mode': 'A3', 'byzantine_authorities': [0, 1, 2], 'seed': 1,
        'start_after_ms': 0, 'duration_ms': 60000, 'delay_ms': 200,
        'late_delay_ms': 1000, 'max_pending_messages': 4096,
        'max_pending_bytes': 67108864, 'max_extra_bytes_per_round': 8388608,
        'target_authority': None, 'target_round': None,
    }
    assert actual['enable_synchronizer'] is True


def test_a0_is_a_no_attack_baseline():
    assert validate_benchmark(params())['adversary']['byzantine_authorities'] == []


@pytest.mark.parametrize('field,value', [
    ('mode', 'A6'), ('seed', -1), ('duration_ms', 0), ('delay_ms', -1),
    ('max_pending_messages', 0), ('max_pending_bytes', True),
    ('max_extra_bytes_per_round', 0), ('target_round', -1),
    ('byzantine_authorities', [0, 0]), ('byzantine_authorities', [10]),
    ('byzantine_authorities', [False]), ('fork_count', 2),
])
def test_invalid_attack_is_rejected(field, value):
    with pytest.raises(ValueError):
        validate_benchmark(params(adversary={'mode': 'A3', field: value}))


def test_crashed_and_byzantine_sets_must_be_disjoint_and_within_fault_budget():
    for ids in ([9], [0, 1, 2]):
        with pytest.raises(ValueError):
            validate_benchmark(params(faults=1, adversary={'mode': 'A2', 'byzantine_authorities': ids}))
    actual = validate_benchmark(params(faults=1, adversary={'mode': 'A2', 'byzantine_authorities': [0, 1]}))
    assert actual['adversary']['byzantine_authorities'] == [0, 1]


def test_attack_must_leave_late_evidence_recovery_inside_run():
    with pytest.raises(ValueError, match='recovery'):
        validate_benchmark(params(duration=60, adversary={'mode': 'A4'}))
    actual = validate_benchmark(params(duration=61, adversary={'mode': 'A4'}))
    assert actual['adversary']['duration_ms'] == 60000


def test_target_leader_must_be_in_fixed_byzantine_set():
    with pytest.raises(ValueError, match='target_authority'):
        validate_benchmark(params(adversary={'mode': 'A3', 'target_authority': 5}))


def test_attack_requires_synchronizer():
    with pytest.raises(ValueError, match='synchronizer'):
        validate_benchmark(params(enable_synchronizer=False, adversary={'mode': 'A1'}))


def test_settings_accept_user_repository_and_merge_attack_fields(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'repo': {'url': 'https://github.com/DrDaydream/mysticeti.git', 'ref': 'HEAD'},
                                'benchmark': {'adversary': {'mode': 'A5', 'seed': 42}}}))
    result = load_settings(path)
    actual = validate_benchmark(result['benchmark'])
    assert actual['adversary']['mode'] == 'A5'
    assert actual['adversary']['seed'] == 42


def test_fabric_selector_count_and_explicit_ids_produce_same_fixed_set():
    from mysticeti_aws.settings import select_adversary
    a = select_adversary(params(), mode='A3', byzantine='2')
    b = select_adversary(params(), mode='A3', byzantine_ids='0,1')
    assert validate_benchmark(a)['adversary'] == validate_benchmark(b)['adversary']
    with pytest.raises(ValueError):
        select_adversary(params(), mode='A3', byzantine='2', byzantine_ids='0,1')


@pytest.mark.parametrize('attack', [
    {'target_round': 0}, {'target_round': 2**64 - 1}, {'delay_ms': 0},
    {'delay_ms': 1001, 'late_delay_ms': 1000},
    {'start_after_ms': 2**64 - 1, 'duration_ms': 2},
    {'target_authority': 0, 'target_round': 5},
])
def test_rejects_invalid_delays_and_unscheduled_target(attack):
    with pytest.raises(ValueError):
        validate_benchmark(params(leaders=1, adversary={'mode': 'A3', **attack}))


def test_target_round_can_select_second_leader_offset():
    actual = validate_benchmark(params(leaders=2, adversary={'mode': 'A4', 'target_authority': 0, 'target_round': 9}))
    assert actual['adversary']['target_round'] == 9
    with pytest.raises(ValueError, match='target_round'):
        validate_benchmark(params(leaders=1, adversary={'mode': 'A4', 'target_authority': 0, 'target_round': 9}))


def test_a0_selector_clears_configured_byzantine_targets():
    from mysticeti_aws.settings import select_adversary
    configured = params(adversary={'mode': 'A3', 'byzantine_authorities': [0], 'target_authority': 0, 'target_round': 10})
    actual = validate_benchmark(select_adversary(configured, mode='A0'))
    assert actual['adversary']['mode'] == 'A0'
    assert actual['adversary']['byzantine_authorities'] == []
    assert actual['adversary']['target_authority'] is None
    assert actual['adversary']['target_round'] is None
