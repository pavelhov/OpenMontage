"""Synthetic local-edit provenance tests; never actual media quality evidence."""
import copy
import json
from pathlib import Path

import pytest

from lib.production_execution import (ProductionGovernanceError, _digest,
    record_derived_edit, record_selection)
from lib.production_provenance import validate_attempt_provenance
from lib.shot_contract import file_sha256, selection_digest, UPSTREAM_PREDICATES
from tests.integration.test_first_pass_workflow import attestation
from tests.lib.test_production_provenance import production, attempt, read, save


@pytest.fixture
def derived(production, monkeypatch):
    monkeypatch.setattr('lib.production_provenance._aac_sha256', lambda path: 'a'*64)
    aid, directory, parent = attempt(production)
    root = production.root
    def binding(path):
        return {'path': str(path), 'sha256': file_sha256(path)}
    output = root / 'assets/video/edited.mp4'
    output.write_bytes(b'SYNTHETIC LOCAL EDIT NOT GENERATED MEDIA')
    outgoing = root / 'assets/images/actual-edited-outgoing.png'
    outgoing.write_bytes(b'SYNTHETIC OUTGOING FRAME ATTESTATION')
    mask = root / 'artifacts/mask.bin'; mask.write_bytes(b'SYNTHETIC MASK')
    implementation = root / 'artifacts/build_edit.py'; implementation.write_text('# SYNTHETIC local pixel editor\n')
    recipe = root / 'artifacts/local-recipe.json'
    save(recipe, {'operation':'local_existing_footage_edit', 'input':parent,
        'output_path':str(output), 'time_window_seconds':[3, 5],
        'mask':binding(mask), 'displacement':{'amplitude_pixels':1.7},
        'command_argv':['ffmpeg','-i','pipe:0','-i',parent['path'],'-c:a','copy',str(output)],
        'implementation':'build_edit.py','implementation_sha256':file_sha256(implementation),
        'ffmpeg_version':'synthetic ffmpeg fixture', 'audio_mode':'stream_copy'})
    receipt = root / 'artifacts/local-receipt.json'
    save(receipt, {'tool':'local_numpy_masked_edit','provider':'local','encoder':'ffmpeg',
        'receipt_author':'build_edit.py','canonical_registry_used':False,
        'success':True,'generation':False,'input':parent,'recipe':binding(recipe),
        'output':binding(output), 'audio':{'mode':'stream_copy','input_sha256':'a'*64,'output_sha256':'a'*64},
        'tool_result':{'success':True,'command_exit_code':0,'artifacts':[binding(output)]}})
    sampled = root / 'artifacts/sampled-edit.json'
    save(sampled, {'tool':'frame_sampler','input':binding(output),'outgoing_frame':binding(outgoing),
        'tool_result':{'success':True,'data':{'frames':[{'path':str(outgoing),'timestamp_seconds':8}]}}})
    record = {'version':'1.0','project_id':root.name,'story_revision':production.story['story_revision'],
        'shot_id':'entry','parent_attempt_id':aid,'parent_output':parent,
        'recipe':binding(recipe),'execution_receipt':binding(receipt),'output':binding(output),
        'preserved_output':{'path':str(root/'production_derived_edits'/file_sha256(output)/'output.mp4'),
                            'sha256':file_sha256(output)},
        'outgoing_frame':binding(outgoing),'outgoing_receipt':binding(sampled)}
    approval = root / 'artifacts/root-edit-approval.json'
    save(approval, {'status':'approved','approved_by':'synthetic root explicit edit approval',
        'derived_edit_sha256':_digest(record),'authorization':production.scope['evidence']})
    record['approval']={'approved_by':'synthetic root explicit edit approval','evidence':binding(approval)}
    return production, aid, directory, record


def check(value, record):
    return validate_attempt_provenance(value.root, record['parent_attempt_id'], shot_id='entry',
        story_revision=value.story['story_revision'],expected_output=record['output'])


def test_derived_registration_and_selection_preserve_native(derived):
    value, aid, directory, record = derived
    before = {n:(directory/n).read_bytes() for n in ['request.json','result.json','raw_result.json']}
    record_derived_edit(value.root, record)
    assert check(value,record)['selected_output'] == record['output']
    selection={'attempt_id':aid,'output':record['output'],'outgoing_frame':record['outgoing_frame']}
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES)
    record_selection(value.root,'entry',selection)
    assert all((directory/n).read_bytes()==b for n,b in before.items())
    assert len(list((value.root/'production_attempts').glob('*/request.json'))) == 1
    assert check(value,{**record,'output':record['parent_output']})['result']['output']==record['parent_output']


@pytest.mark.parametrize('target', ['parent_output','recipe','execution_receipt','output','preserved_output','outgoing_frame','outgoing_receipt','approval'])
def test_changed_derived_evidence_blocks(derived,target):
    value,_,_,record=derived
    record_derived_edit(value.root,record)
    binding=record[target]['evidence'] if target=='approval' else record[target]
    path=Path(binding['path']);path.chmod(0o644);path.write_bytes(b'TAMPERED')
    with pytest.raises(ProductionGovernanceError):check(value,record)


def test_unregistered_output_and_failed_native_parent_block(derived):
    value,_,directory,record=derived
    with pytest.raises(ProductionGovernanceError):check(value,record)
    native=read(directory/'result.json');native['status']='failed';save(directory/'result.json',native)
    with pytest.raises(ProductionGovernanceError):record_derived_edit(value.root,record)


def test_stale_exact_approval_blocks(derived):
    value,_,_,record=derived
    record['outgoing_frame'] = copy.deepcopy(record['parent_output'])
    with pytest.raises(ProductionGovernanceError):record_derived_edit(value.root,record)


def test_selected_outgoing_must_equal_reviewed_derivation(derived):
    value,aid,_,record=derived;record_derived_edit(value.root,record)
    selection={'attempt_id':aid,'output':record['output'],'outgoing_frame':record['parent_output']}
    selection['review']=attestation(selection_digest(selection),UPSTREAM_PREDICATES)
    with pytest.raises(ProductionGovernanceError,match='outgoing frame differs'):
        record_selection(value.root,'entry',selection)


@pytest.mark.parametrize('change', ['script','missing_approval','missing_authorization','generation','window','native_journal','audio','record_at_native_sha'])
def test_forged_or_incomplete_edit_cannot_bypass_parent(derived, change, monkeypatch):
    value,_,directory,record=derived
    root=value.root
    if change=='script':
        (root/'artifacts/build_edit.py').write_text('# changed script')
    elif change=='missing_approval':
        Path(record['approval']['evidence']['path']).unlink()
    elif change=='missing_authorization':
        Path(value.scope['evidence']['path']).unlink()
    elif change in ('generation','window'):
        key='execution_receipt' if change=='generation' else 'recipe'
        path=Path(record[key]['path']);data=read(path)
        if change=='generation':data['generation']=True
        else:data['time_window_seconds']=[3,900]
        save(path,data);record[key]['sha256']=file_sha256(path)
        # Even a freshly resealed exact root binding cannot relax factual rules.
        approval=Path(record['approval']['evidence']['path']);accepted=read(approval)
        accepted['derived_edit_sha256']=_digest({k:v for k,v in record.items() if k!='approval'})
        save(approval,accepted);record['approval']['evidence']['sha256']=file_sha256(approval)
    elif change=='native_journal':
        data=read(directory/'request.json');data['request_sha256']='0'*64;save(directory/'request.json',data)
    elif change=='audio':
        monkeypatch.setattr('lib.production_provenance._aac_sha256',lambda path:'b'*64)
    else:
        record['output']=record['parent_output']
        record['preserved_output']={'path':str(root/'production_derived_edits'/record['output']['sha256']/'output.mp4'),'sha256':record['output']['sha256']}
    with pytest.raises(ProductionGovernanceError):record_derived_edit(root,record)
