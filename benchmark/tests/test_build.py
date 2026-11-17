import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_missing_manifest_demands_install(tmp_path):
    from mysticeti_aws.build import verify_build
    with pytest.raises(RuntimeError, match='fab install'):
        verify_build(tmp_path)


def test_changed_binary_is_rejected(tmp_path):
    from mysticeti_aws.build import verify_build
    from mysticeti_aws.settings import REPOSITORY, RUSTFLAGS, RUST_TOOLCHAIN
    provenance={'commit':'a'*40,'repository':REPOSITORY}
    binary=tmp_path/'target/release/mysticeti'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'original')
    binary.chmod(0o755)
    record={**provenance,'adversary_schema':1,'rust_toolchain':RUST_TOOLCHAIN,'rustflags':RUSTFLAGS,
            'sha256':hashlib.sha256(b'original').hexdigest()}
    (tmp_path/'.mysticeti-build.json').write_text(json.dumps(record))
    assert verify_build(tmp_path, provenance)==record
    binary.write_bytes(b'modified')
    with pytest.raises(RuntimeError,match='fab install'):
        verify_build(tmp_path, provenance)


def test_build_failure_clears_previous_manifest(tmp_path):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists():
        pytest.skip('bash unavailable')
    source=tmp_path/'source'
    source.mkdir()
    subprocess.run(['git','init',str(source)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'-c','user.name=test','-c','user.email=test@example.invalid',
                    'commit','--allow-empty','-m','fixture'],check=True,capture_output=True)
    sha=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    module=source/'mysticeti-core/src/adversary.rs'
    module.parent.mkdir(parents=True)
    module.write_text('// attack fixture')
    subprocess.run(['git','-C',str(source),'add','.'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'-c','user.name=test','-c','user.email=test@example.invalid','commit','-m','attack'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(source),'remote','add','origin',source.as_posix()],check=True,capture_output=True)
    sha=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
    record=source/'.mysticeti-build.json'
    record.write_text('previous successful build')
    fakebin=tmp_path/'bin'; fakebin.mkdir()
    cargo=fakebin/'cargo'; cargo.write_text('#!/bin/bash\necho COMPILER_FAILED >&2\nexit 42\n',newline='\n')
    cargo.chmod(0o755)
    script=Path(__file__).parents[1]/'scripts/build_node.sh'
    env=dict(os.environ,PATH=f'{fakebin.as_posix()}{os.pathsep}{os.environ["PATH"]}')
    result=subprocess.run([str(bash),script.as_posix(),source.as_posix(),sha,'2',source.as_posix()],env=env,capture_output=True,text=True)
    assert 'COMPILER_FAILED' in result.stderr, result.stderr
    assert result.returncode != 0
    assert not record.exists()


def test_fresh_source_sync_materializes_checkout_and_preserves_user_edits(tmp_path):
    bash = Path('C:/Program Files/Git/bin/bash.exe') if os.name == 'nt' else Path('/bin/bash')
    if not bash.exists(): pytest.skip('bash unavailable')
    origin=tmp_path/'origin'; origin.mkdir()
    subprocess.run(['git','init','-b','attack-branch',str(origin)],check=True,capture_output=True)
    (origin/'tracked.txt').write_text('original')
    subprocess.run(['git','-C',str(origin),'add','tracked.txt'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(origin),'-c','user.name=test','-c','user.email=test@example.invalid',
                    'commit','-m','fixture'],check=True,capture_output=True)
    sha=subprocess.check_output(['git','-C',str(origin),'rev-parse','HEAD'],text=True).strip()
    target=tmp_path/'fresh'
    script=Path(__file__).parents[1]/'scripts/sync_source.sh'
    args=[str(bash),script.as_posix(),target.as_posix(),sha,origin.as_posix()]
    result=subprocess.run(args,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (target/'tracked.txt').read_text()=='original'
    assert subprocess.run(args,capture_output=True).returncode==0
    (target/'tracked.txt').write_text('user change')
    result=subprocess.run(args,capture_output=True,text=True)
    assert result.returncode!=0
    assert (target/'tracked.txt').read_text()=='user change'
