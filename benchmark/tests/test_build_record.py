import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from test_provenance import checkout_fixture
from mysticeti_aws.settings import ADVERSARY_DEFAULTS, REPOSITORY


def writer_module():
    path = Path(__file__).parents[1] / 'scripts/record_build.py'
    spec = importlib.util.spec_from_file_location('record_build', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_writer_probes_binary_schema_before_recording_capability(tmp_path, monkeypatch):
    writer = writer_module()
    source = checkout_fixture(tmp_path / 'source')
    binary = source / 'target/release/mysticeti'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'new attack binary')
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    supported = [False]
    def binary_command(args, **kwargs):
        assert args[0] == str(binary)
        if args[1] == '--help': return
        output = Path(args[args.index('--working-directory') + 1])
        config = {'adversary': dict(ADVERSARY_DEFAULTS, start_after_ms=0)} if supported[0] else {}
        payload = config if 'mysticeti_aws' == 'mysticeti_aws' else {'parameters': config}
        filename = 'parameters.yaml' if 'mysticeti_aws' == 'mysticeti_aws' else 'public-config.yaml'
        (output / filename).write_text(yaml.safe_dump(payload))
    monkeypatch.setattr(writer, 'run_binary', binary_command)
    with pytest.raises(RuntimeError, match='adversary'):
        writer.write_build_record(source, revision, REPOSITORY)
    assert not (source / '.mysticeti-build.json').exists()
    supported[0] = True
    writer.write_build_record(source, revision, REPOSITORY)
    record = json.loads((source / '.mysticeti-build.json').read_text())
    assert record['commit'] == revision
    assert record['repository'] == REPOSITORY
    assert record['adversary_schema'] == 1
    assert record['sha256'] == hashlib.sha256(b'new attack binary').hexdigest()
