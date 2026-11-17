import copy
import json

import pytest


def valid():
    from mysticeti_aws.settings import DEFAULTS
    return copy.deepcopy(DEFAULTS)


def test_rejects_production_or_unreviewed_revision(tmp_path):
    from mysticeti_aws.settings import load_settings
    data = valid()
    data['repo']['commit'] = 'main'
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='commit'):
        load_settings(path)


@pytest.mark.parametrize('field,value', [('nodes', 3), ('nodes',129),('faults',4),('rate',0),('tx_size',16),('duration',0),('warmup',-1),('runs',0),('leaders',0)])
def test_invalid_experiment_fails_before_cloud_access(field, value):
    from mysticeti_aws.settings import validate_benchmark
    data = valid()['benchmark']
    data[field] = value
    with pytest.raises(ValueError):
        validate_benchmark(data)


def test_effective_load_quantizes_and_reports_offline_generators():
    from mysticeti_aws.settings import load_allocation
    assert load_allocation(5000,10,0) == {'per_node_tps':500,'effective_total_tps':5000,'requested_total_tps':5000}
    assert load_allocation(5001,10,3) == {'per_node_tps':500,'effective_total_tps':3500,'requested_total_tps':5001}
    assert load_allocation(5010,10,0)['effective_total_tps'] == 5100


def test_remote_paths_cannot_point_at_home_or_parent(tmp_path):
    from mysticeti_aws.settings import load_settings
    for unsafe in ['/home/ubuntu','/home/ubuntu/experiments/../.ssh','/','relative']:
        data=valid()
        data['remote']['source_dir']=unsafe
        path=tmp_path/'settings.json'
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_settings(path)


def test_default_configuration_loads_and_preserves_regions(tmp_path):
    from mysticeti_aws.settings import load_settings
    path=tmp_path/'settings.json'
    path.write_text(json.dumps(valid()))
    loaded=load_settings(path)
    assert loaded['instances']['regions']==['us-east-1','eu-north-1','ap-southeast-2','us-west-1','ap-northeast-1']
    assert loaded['benchmark']['nodes']==10


def test_rejects_timeout_that_unmodified_paper_runtime_does_not_use():
    from mysticeti_aws.settings import validate_benchmark
    data=valid()['benchmark']
    data['leader_timeout_ms']=2000
    with pytest.raises(ValueError,match='1000'):
        validate_benchmark(data)
