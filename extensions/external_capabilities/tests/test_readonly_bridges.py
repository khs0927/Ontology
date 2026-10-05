import hashlib
import json
from copy import deepcopy
import pytest
from readonly_bridges import ingest_readonly_probe, ingest_section_catalog

IDENTITY = dict(provider_id='hs-steel-cad', upstream_repo='https://github.com/khs0927/hs-steel-cad', upstream_commit='a'*40, source_path='export/catalog.json', adapter_version='1')
ASSET = b'header\nH100 original row\n'

def catalog():
    return dict(schema_version=1, identity=deepcopy(IDENTITY), units=dict(dimensions='mm', unit_weight='kg/m', paint_area='m2/m'), errors=[], rows=[dict(source_path='attributes/H.txt', line_number=2, source_file_sha256=hashlib.sha256(ASSET).hexdigest(), parse_success=True, designation='H100', family='H', raw_value='H100 original row', dimensions=[100,100,6,8,0,0], unit_weight=17.2, paint_area=0.6, aci_color=7)])

def ingest(data):
    return ingest_section_catalog(json.dumps(data).encode(), expected_identity=IDENTITY, source_files={'attributes/H.txt':ASSET})

def test_catalog_never_inherits_authority():
    data=catalog(); data.update(status='VERIFIED', execution_allowed=True, canonical_allowed=True)
    data['rows'][0]['command']='erase'
    result=ingest(data)
    assert result['status']=='DECLARED' and not result['execution_allowed'] and not result['canonical_allowed']
    assert 'command' not in result['rows'][0]
    assert result['payload_sha256']==hashlib.sha256(json.dumps(data).encode()).hexdigest()

@pytest.mark.parametrize('change', [lambda d:d.update(schema_version=True), lambda d:d['rows'][0].update(source_path=[]), lambda d:d['rows'][0].update(unit_weight=10**1000), lambda d:d.update(rows=[]), lambda d:d.update(errors=['bad row']), lambda d:d['units'].update(dimensions='inch'), lambda d:d['identity'].update(upstream_commit='b'*40), lambda d:d['rows'][0].update(source_file_sha256='0'*64), lambda d:d['rows'][0].update(parse_success=False), lambda d:d['rows'][0].update(unit_weight=0), lambda d:d['rows'][0].update(dimensions=[True,1,1,1,1,1]), lambda d:d['rows'].append(deepcopy(d['rows'][0]))])
def test_reject_invalid_catalog(change):
    data=catalog(); change(data)
    with pytest.raises(ValueError): ingest(data)

@pytest.mark.parametrize('provider', ['freecad','rhino','sketcharch'])
def test_readonly_providers(provider):
    identity=dict(IDENTITY, provider_id=provider)
    data=dict(schema_version=1,identity=identity, read_only=True,mutation_count=0,capabilities=['read_context','health'],authenticated=False,complete=True,status='VERIFIED',execution_allowed=True)
    result=ingest_readonly_probe(json.dumps(data).encode(),expected_identity=identity,expected_capabilities=['read_context','health'])
    assert result['status']=='NOT_RUN' and not result['execution_allowed']
    data.update(host=provider,host_version='test-version')
    result=ingest_readonly_probe(json.dumps(data).encode(),expected_identity=identity,expected_capabilities=['read_context','health'])
    assert result['status']=='DECLARED' and not result['native_mapping_verified']
    assert not result['probe_authenticated'] and not result['host_identity_verified']

@pytest.mark.parametrize('change',[{'mutation_count':False},{'mutation_count':1},{'read_only':False},{'capabilities':['execute_python']}])
def test_reject_unsafe_probe(change):
    identity=dict(IDENTITY,provider_id='rhino')
    data=dict(schema_version=1,identity=identity,read_only=True,mutation_count=0,capabilities=[],authenticated=False,complete=True);data.update(change)
    with pytest.raises(ValueError):ingest_readonly_probe(json.dumps(data).encode(),expected_identity=identity,expected_capabilities=['health'])

def test_duplicate_json_and_nan_rejected():
    for raw in [b'{"x":1,"x":2}',b'{"x":NaN}']:
        with pytest.raises(ValueError): ingest_readonly_probe(raw,expected_identity=dict(IDENTITY,provider_id='rhino'),expected_capabilities=['health'])


def probe(provider='rhino', **changes):
    identity=dict(IDENTITY,provider_id=provider)
    data=dict(schema_version=1,identity=identity,read_only=True,mutation_count=0,
              capabilities=['health','read_context'],authenticated=False,complete=True,
              host=provider,host_version='test-version')
    data.update(changes)
    return identity, data


@pytest.mark.parametrize('change', [
    {'schema_version':2},
    {'complete':False},
    {'authenticated':True},
    {'capabilities':['health']},
    {'capabilities':['health','read_context','version']},
    {'capabilities':['health','health']},
    {'host':'freecad'},
])
def test_probe_contract_mismatches_fail_closed(change):
    identity,data=probe();data.update(change)
    with pytest.raises(ValueError):
        ingest_readonly_probe(json.dumps(data).encode(), expected_identity=identity,
                              expected_capabilities=['health','read_context'])


def test_missing_required_probe_field_fails_closed():
    identity,data=probe()
    del data['complete']
    with pytest.raises(ValueError, match='Incomplete'):
        ingest_readonly_probe(json.dumps(data).encode(), expected_identity=identity,
                              expected_capabilities=['health','read_context'])


@pytest.mark.parametrize('state,content',[('timeout',None),('empty',b'')])
def test_transport_failures_are_not_run_and_non_authorizing(state, content):
    identity,_=probe()
    result=ingest_readonly_probe(content, expected_identity=identity,
                                 expected_capabilities=['health'], transport_state=state)
    assert result['status']=='NOT_RUN'
    assert result['transport_state']==state
    assert result['payload_sha256'] is None
    assert not result['execution_allowed']
    assert not result['canonical_allowed']
    assert not result['probe_authenticated']
    assert not result['host_identity_verified']
    assert not result['native_mapping_verified']


def test_ok_transport_rejects_empty_response():
    identity,_=probe()
    with pytest.raises(ValueError, match='Empty'):
        ingest_readonly_probe(b'', expected_identity=identity,
                              expected_capabilities=['health'], transport_state='ok')


def test_transport_failure_rejects_stray_response_bytes():
    identity,_=probe()
    with pytest.raises(ValueError):
        ingest_readonly_probe(b'{}', expected_identity=identity,
                              expected_capabilities=['health'], transport_state='timeout')
