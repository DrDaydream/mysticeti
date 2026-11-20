"""Validate experiment inputs before any AWS or SSH side effects."""
import copy
import ipaddress
import json
import re
from pathlib import Path, PurePosixPath

REPOSITORY = 'https://github.com/DrDaydream/mysticeti.git'
RUST_TOOLCHAIN = '1.72.1'
RUSTFLAGS = '-C target-cpu=native'
DEFAULTS = {
    'testbed_id': 'mysticeti-ubuntu24',
    'key': {'name': 'orca-amazon', 'path': '/home/ubuntu/.ssh/orca-amazon.pem'},
    'repo': {'url': REPOSITORY, 'ref': 'HEAD'},
    'instances': {'type': 'm5.8xlarge', 'regions': [
        'us-east-1', 'eu-north-1', 'ap-southeast-2', 'us-west-1', 'ap-northeast-1'
    ], 'volume_size': 200},
    'remote': {'source_dir': '/home/ubuntu/experiments/mysticeti',
               'run_dir': '/home/ubuntu/experiments/mysticeti-runs',
               'build_jobs': 4, 'parallelism': 5},
    'ssh_cidrs': [],
    'benchmark': {'nodes': 10, 'faults': 0, 'rate': 5000, 'tx_size': 512,
                  'duration': 120, 'warmup': 20, 'runs': 1, 'leaders': 2, 'wave_length': 3,
                  'enable_synchronizer': True, 'leader_schedule': 'round_robin', 'adversary': {},
                  'leader_timeout_ms': 1000, 'scrape_interval': 5},
}


def integer(value, name, lower=1, upper=None):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be an integer')
    if not isinstance(value, int):
        if not isinstance(value, str) or not re.fullmatch(r'-?\d+', value):
            raise ValueError(f'{name} must be an integer')
        value = int(value)
    if value < lower or (upper is not None and value > upper):
        raise ValueError(f'{name} is outside the supported range [{lower}, {upper}]')
    return value


ADVERSARY_DEFAULTS = {
    'mode': 'A0', 'byzantine_authorities': [], 'seed': 1,
    'start_after_ms': None, 'duration_ms': 60000, 'delay_ms': 200,
    'late_delay_ms': 1000, 'max_pending_messages': 4096,
    'max_pending_bytes': 67108864, 'max_extra_bytes_per_round': 8388608,
    'target_authority': None, 'target_round': None,
}


def select_adversary(parameters, mode=None, byzantine=None, byzantine_ids=None):
    """Apply Fabric selectors before validation fixes the identities for the run."""
    result = copy.deepcopy(parameters)
    attack = result.setdefault('adversary', {})
    if mode is not None:
        attack['mode'] = mode
        if mode == 'A0' and byzantine is None and byzantine_ids is None:
            attack.update(byzantine_authorities=[], target_authority=None, target_round=None)
    if byzantine is not None and byzantine_ids is not None:
        raise ValueError('Use either byzantine count or byzantine_ids, not both')
    if byzantine is not None:
        count = integer(byzantine, 'byzantine', 1, (integer(result['nodes'], 'nodes') - 1) // 3)
        attack['byzantine_authorities'] = list(range(count))
    if byzantine_ids is not None:
        if not isinstance(byzantine_ids, str) or not byzantine_ids.strip():
            raise ValueError('byzantine_ids must be comma-separated integer IDs')
        attack['byzantine_authorities'] = [integer(i.strip(), 'byzantine_ids', 0) for i in byzantine_ids.split(',')]
    return result


def validate_adversary(value, parameters):
    if not isinstance(value, dict) or set(value) - set(ADVERSARY_DEFAULTS):
        raise ValueError('Unknown or invalid adversary fields')
    result = copy.deepcopy(ADVERSARY_DEFAULTS)
    result.update(value)
    if result['mode'] not in ('A0', 'A1', 'A2', 'A3', 'A4', 'A5'):
        raise ValueError('adversary.mode must be A0..A5')
    active = result['mode'] != 'A0'
    n, faults = parameters['nodes'], parameters['faults']
    f = (n - 1) // 3
    ids = result['byzantine_authorities']
    if not isinstance(ids, list) or any(type(i) is not int or not 0 <= i < n for i in ids):
        raise ValueError('byzantine_authorities must contain integer IDs in the committee')
    if len(ids) != len(set(ids)):
        raise ValueError('byzantine_authorities must be unique')
    if active and not ids:
        ids = list(range(f))
    if not active and ids:
        raise ValueError('A0 must have no byzantine_authorities')
    if set(ids).intersection(range(n - faults, n)):
        raise ValueError('Byzantine identities overlap the crashed tail identities')
    if len(ids) + faults > f:
        raise ValueError('Byzantine identities plus crashed faults exceed floor((nodes-1)/3)')
    result['byzantine_authorities'] = sorted(ids)
    if result['start_after_ms'] is None:
        result['start_after_ms'] = 0
    for key in ('seed', 'start_after_ms', 'duration_ms', 'delay_ms', 'late_delay_ms',
                'max_pending_messages', 'max_pending_bytes', 'max_extra_bytes_per_round'):
        lower = 0 if key in ('seed', 'start_after_ms') else 1
        result[key] = integer(result[key], 'adversary.' + key, lower, 2**64 - 1)
    for key in ('target_authority', 'target_round'):
        if result[key] is not None:
            upper = n - 1 if key == 'target_authority' else 2**64 - 1 - parameters['wave_length']
            lower = 0 if key == 'target_authority' else 1
            result[key] = integer(result[key], 'adversary.' + key, lower, upper)
    if result['target_authority'] is not None and result['target_authority'] not in ids:
        raise ValueError('adversary.target_authority must be a fixed Byzantine identity')
    if result['start_after_ms'] + result['duration_ms'] > 2**64 - 1:
        raise ValueError('Attack start plus duration overflows milliseconds')
    if active:
        if result['delay_ms'] > result['late_delay_ms']:
            raise ValueError('adversary.delay_ms must be <= late_delay_ms')
        target_round = result['target_round']
        if target_round is not None:
            scheduled = {(target_round + offset) % n for offset in range(parameters['leaders'])}
            candidates = {result['target_authority']} if result['target_authority'] is not None else set(ids)
            if not scheduled.intersection(candidates):
                raise ValueError('adversary.target_round does not elect the fixed Byzantine target with these leaders')
        if not parameters['enable_synchronizer']:
            raise ValueError('Active adversary experiments require enable_synchronizer=true')
        recovery = max(result['late_delay_ms'], result['delay_ms'])
        experiment_ms = parameters['duration'] * 1000
        if result['start_after_ms'] + result['duration_ms'] + recovery > experiment_ms:
            raise ValueError('Attack duration must leave recovery >= late_delay_ms inside measurement duration')
    return result


def validate_benchmark(parameters):
    result = copy.deepcopy(parameters)
    result['nodes'] = integer(result['nodes'], 'nodes', 4, 128)
    n = result['nodes']
    for key in ('rate', 'tx_size', 'duration', 'runs', 'leaders', 'leader_timeout_ms', 'scrape_interval'):
        result[key] = integer(result[key], key)
    result['warmup'] = integer(result['warmup'], 'warmup', 0)
    result['faults'] = integer(result['faults'], 'faults', 0, (n - 1) // 3)
    if type(result.get('enable_synchronizer', True)) is not bool:
        raise ValueError('enable_synchronizer must be a boolean')
    result.setdefault('enable_synchronizer', True)
    result.setdefault('leader_schedule', 'round_robin')
    result['wave_length'] = integer(result.get('wave_length', 3), 'wave_length', 3, 3)
    if result['rate'] < n or result['tx_size'] <= 16 or result['leaders'] > n:
        raise ValueError('Require rate >= nodes, tx_size > 16 and leaders <= nodes')
    result['adversary'] = validate_adversary(result.get('adversary', {}), result)
    if result['adversary']['mode'] != 'A0' and result['leader_schedule'] != 'round_robin':
        raise ValueError('Active adversary experiments require leader_schedule=round_robin')
    if result['leader_timeout_ms'] != 1000:
        raise ValueError('This paper runtime hardcodes leader timeout to 1000 ms; other values have no effect')
    return result


def load_allocation(rate, nodes, faults):
    per_node = ((rate // nodes + 9) // 10) * 10
    return {'per_node_tps': per_node, 'effective_total_tps': per_node * (nodes - faults),
            'requested_total_tps': rate}


def _remote_path(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'/home/ubuntu/experiments/[A-Za-z0-9_./-]+', value):
        raise ValueError(f'{field} must be a dedicated path below /home/ubuntu/experiments/')
    if '..' in value.split('/') or str(PurePosixPath(value)) != value:
        raise ValueError(f'Unsafe or noncanonical {field}: {value}')
    return value


def load_settings(path='settings.json'):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    settings = copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict) or set(data) - set(settings):
        raise ValueError('settings.json has unknown top-level fields')
    for key, value in data.items():
        if isinstance(settings[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f'{key} must be an object')
            unknown = set(value) - set(settings[key])
            if unknown:
                raise ValueError(f'Unknown settings fields in {key}: {sorted(unknown)}')
            settings[key].update(value)
        else:
            settings[key] = value
    if settings['repo']['url'] != REPOSITORY:
        raise ValueError(f'repo.url must select the modified user repository {REPOSITORY}')
    ref = settings['repo']['ref']
    if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', ref):
        raise ValueError('repo.ref must be HEAD, a branch/tag, or a full commit hash')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,47}', settings['testbed_id']):
        raise ValueError('testbed_id must contain 3..48 lowercase letters, digits or hyphens')
    regions = settings['instances']['regions']
    if not isinstance(regions, list) or not regions or len(regions) != len(set(regions)):
        raise ValueError('regions must be a nonempty unique list')
    if any(not isinstance(r, str) or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', r) for r in regions):
        raise ValueError('Invalid AWS region')
    settings['instances']['volume_size'] = integer(settings['instances']['volume_size'], 'volume_size', 30, 16384)
    if not isinstance(settings['key']['name'], str) or not settings['key']['name']:
        raise ValueError('key.name must be nonempty')
    settings['key']['path'] = str(Path(settings['key']['path']).expanduser())
    for field in ('source_dir', 'run_dir'):
        _remote_path(settings['remote'][field], field)
    source, runs = map(PurePosixPath, (settings['remote']['source_dir'], settings['remote']['run_dir']))
    if source == runs or source in runs.parents or runs in source.parents:
        raise ValueError('source_dir and run_dir must be separate directories')
    for field in ('build_jobs', 'parallelism'):
        settings['remote'][field] = integer(settings['remote'][field], field, 1, 32)
    if not isinstance(settings['ssh_cidrs'], list):
        raise ValueError('ssh_cidrs must be a list; [] detects the controller IPv4')
    settings['ssh_cidrs'] = [str(ipaddress.IPv4Network(c, strict=True)) for c in settings['ssh_cidrs']]
    # Keep omission/null defaults unresolved until Fabric overrides are applied.
    validate_benchmark(settings['benchmark'])
    return settings
