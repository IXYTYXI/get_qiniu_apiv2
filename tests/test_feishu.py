import json
from pathlib import Path
import subprocess
from collections import Counter
import httpx
import pytest
from qiniu_get.feishu import FeishuBase, FeishuError, decode_records, missing_rows


def test_decode_actual_cli_column_response():
    data = {'fields': ['名称', '音频'], 'data': [['a', [{'name': 'x.mp3'}]]], 'record_id_list': ['rec1']}
    assert decode_records(data) == [{'id': 'rec1', 'fields': {'名称': 'a', '音频': [{'name': 'x.mp3'}]}}]


def test_dedup_preserves_identical_messages_occurrences():
    assert missing_rows([['x'], ['x'], ['y']], [['x']]) == [['x'], ['y']]


def test_batch_is_200_rows_and_user_identity(tmp_path):
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if '+record-batch-create' in args:
            body = json.loads((Path(kwargs['cwd']) / args[args.index('--json') + 1][1:]).read_text(encoding='utf-8'))
            assert len(body['rows']) <= 200
        return subprocess.CompletedProcess(args, 0, json.dumps({'ok': True, 'data': {}}), '')
    base = FeishuBase('base', runner=runner)
    base.batch_create('tbl1', ['内容'], [[str(i)] for i in range(401)])
    assert len(calls) == 3
    assert all(a[a.index('--as') + 1] == 'user' for a in calls)


def test_attachment_resume_skips_already_uploaded_file(tmp_path):
    a, b = tmp_path / '1_s3600_part0000.mp3', tmp_path / '1_s3600_part0001.mp3'
    a.write_bytes(b'1'); b.write_bytes(b'2')
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if '+record-list' in args:
            data = {'fields': ['名称', '日期', '音频'], 'data': [['1 title', '2026-09-09', [{'name': a.name}]]], 'record_id_list': ['rec1'], 'has_more': False}
        else:
            data = {}
        return subprocess.CompletedProcess(args, 0, json.dumps({'ok': True, 'data': data}), '')
    base = FeishuBase('base', runner=runner)
    assert base.sync_audio('tbl1', 1, 'title', '2026-09-09', [a, b]) == 1
    uploads = [x for x in calls if '+record-upload-attachment' in x]
    assert len(uploads) == 1 and b.name in uploads[0] and a.name not in uploads[0]


def test_failed_cli_is_not_retried_blindly():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', 'secret-bearing response')
    with pytest.raises(FeishuError) as error:
        FeishuBase('base', runner=runner).batch_create('tbl1', ['内容'], [['x']])
    assert len(calls) == 1
    assert 'secret-bearing' not in str(error.value)


def test_schema_mismatch_blocks_writes():
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps({'ok': True, 'data': {'fields': [{'name': '音频', 'type': 'text'}], 'total': 1}}), '')
    with pytest.raises(FeishuError, match='音频'):
        FeishuBase('base', runner=runner).validate('tbl1', {'音频': 'attachment'})


def test_cli_file_arguments_are_relative_to_subprocess_cwd(tmp_path):
    def runner(args, **kwargs):
        cwd = Path(kwargs['cwd'])
        if '--json' in args:
            value = args[args.index('--json') + 1]
            assert value.startswith('@') and not Path(value[1:]).is_absolute()
            assert json.loads((cwd / value[1:]).read_text(encoding='utf-8')) == {'名称': 'test'}
        if '--file' in args:
            value = args[args.index('--file') + 1]
            assert not Path(value).is_absolute()
            assert (cwd / value).read_bytes() == b'audio'
        return subprocess.CompletedProcess(args, 0, json.dumps({'ok': True, 'data': {}}), '')
    from pathlib import Path
    base = FeishuBase('base', runner=runner)
    base._call('+record-upsert', 'tbl1', payload={'名称': 'test'})
    audio = tmp_path / 'audio.mp3'; audio.write_bytes(b'audio')
    base._call('+record-upload-attachment', 'tbl1', '--file', str(audio))


def _response(method, url, status, body):
    return httpx.Response(status, json=body, request=httpx.Request(method, url))


class FakeHTTP:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        status, body = self.handler(method, url, kwargs)
        return _response(method, url, status, body)

    def close(self):
        pass


def test_open_api_maps_field_types_and_uses_tenant_token():
    def handler(method, url, kwargs):
        if url.endswith('/tenant_access_token/internal'):
            assert kwargs['json']['app_id'] == 'cli_test'
            return 200, {'code': 0, 'tenant_access_token': 't-app', 'expire': 7200}
        assert kwargs['headers']['Authorization'] == 'Bearer t-app'
        assert url.endswith('/fields')
        return 200, {'code': 0, 'data': {'items': [
            {'field_name': '名称', 'type': 1},
            {'field_name': '日期', 'type': 1},
            {'field_name': '音频', 'type': 17},
        ], 'has_more': False}}
    http = FakeHTTP(handler)
    base = FeishuBase('basetoken', auth='app', app_id='cli_test', app_secret='secret', http=http)
    base.validate('tbl1', {'名称': 'text', '日期': 'text', '音频': 'attachment'})


def test_open_api_batch_create_is_200_rows_and_write_is_not_retried():
    writes = []
    def handler(method, url, kwargs):
        if url.endswith('/tenant_access_token/internal'):
            return 200, {'code': 0, 'tenant_access_token': 't-app', 'expire': 7200}
        writes.append(url)
        assert len(kwargs['json']['records']) <= 200
        return 500, {'code': 1, 'msg': 'secret-bearing response'}
    http = FakeHTTP(handler)
    base = FeishuBase('basetoken', auth='app', app_id='id', app_secret='secret-value', http=http)
    with pytest.raises(FeishuError) as error:
        base.batch_create('tbl1', ['内容'], [[str(i)] for i in range(201)])
    assert len(writes) == 1
    assert 'secret-value' not in str(error.value)
    assert 'secret-bearing' not in str(error.value)


def test_open_api_attachment_resume_skips_and_appends_file_token(tmp_path):
    a, b = tmp_path / '1_s3600_part0000.mp3', tmp_path / '1_s3600_part0001.mp3'
    a.write_bytes(b'1'); b.write_bytes(b'2')
    puts, uploads = [], []
    def handler(method, url, kwargs):
        if url.endswith('/tenant_access_token/internal'):
            return 200, {'code': 0, 'tenant_access_token': 't-app', 'expire': 7200}
        if url.endswith('/records/search'):
            return 200, {'code': 0, 'data': {'items': [{
                'record_id': 'rec1',
                'fields': {'名称': '1 title', '日期': '2026-09-09',
                           '音频': [{'name': a.name, 'file_token': 'tok-a'}]},
            }], 'has_more': False}}
        if url.endswith('/medias/upload_all'):
            uploads.append(kwargs['data']['file_name'])
            return 200, {'code': 0, 'data': {'file_token': 'tok-b'}}
        if '/records/rec1' in url and method == 'PUT':
            puts.append(kwargs['json']['fields']['音频'])
            return 200, {'code': 0, 'data': {}}
        raise AssertionError(url)
    http = FakeHTTP(handler)
    base = FeishuBase('basetoken', auth='app', app_id='id', app_secret='secret', http=http)
    assert base.sync_audio('tbl1', 1, 'title', '2026-09-09', [a, b]) == 1
    assert uploads == [b.name]
    assert puts == [[{'file_token': 'tok-a'}, {'file_token': 'tok-b'}]]


def test_open_api_401_refreshes_tenant_token_once():
    tokens, fields = [], []
    def handler(method, url, kwargs):
        if url.endswith('/tenant_access_token/internal'):
            tokens.append(1)
            return 200, {'code': 0, 'tenant_access_token': f't{len(tokens)}', 'expire': 7200}
        fields.append(kwargs['headers']['Authorization'])
        if len(fields) == 1:
            return 401, {'code': 99991663, 'msg': 'token invalid'}
        return 200, {'code': 0, 'data': {'items': [{'field_name': '音频', 'type': 17}], 'has_more': False}}
    http = FakeHTTP(handler)
    base = FeishuBase('basetoken', auth='app', app_id='id', app_secret='secret', http=http)
    base.validate('tbl1', {'音频': 'attachment'})
    assert len(tokens) == 2
    assert fields == ['Bearer t1', 'Bearer t2']


def test_open_record_search_paginates_with_url_query_parameters():
    queries = []
    def handler(request):
        if request.url.path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200, json={
                'code': 0, 'tenant_access_token': 'test-token', 'expire': 7200})
        assert request.method == 'POST'
        assert request.url.path.endswith('/records/search')
        body = json.loads(request.content)
        assert body == {
            'field_names': ['内容'],
            'filter': {'conjunction': 'and', 'conditions': [
                {'field_name': '直播ID', 'operator': 'is', 'value': ['617691']}]}}
        query = dict(request.url.params)
        assert query.get('page_size') == '200'
        queries.append(query)
        token = query.get('page_token')
        assert token in (None, 'next/page+2')
        second = token is not None
        return httpx.Response(200, json={'code': 0, 'data': {
            'items': [{'record_id': 'rec2' if second else 'rec1',
                       'fields': {'内容': 'second' if second else 'first'}}],
            'has_more': not second, 'page_token': '' if second else 'next/page+2'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        base = FeishuBase('base', auth='app', app_id='id', app_secret='secret', http=client)
        records = list(base.records('table', ['内容'], [['直播ID', '==', '617691']]))
    assert [r['id'] for r in records] == ['rec1', 'rec2']
    assert queries == [{'page_size': '200'}, {'page_size': '200', 'page_token': 'next/page+2'}]


def test_app_rich_text_existing_audio_row_is_reused(tmp_path):
    audio = tmp_path / '1_part.mp3'
    audio.write_bytes(b'audio')
    def handler(method, url, kwargs):
        if url.endswith('/tenant_access_token/internal'):
            return 200, {'code': 0, 'tenant_access_token': 'token', 'expire': 7200}
        if url.endswith('/records/search'):
            return 200, {'code': 0, 'data': {'items': [{
                'record_id': 'rec-existing', 'fields': {
                    '名称': [{'type': 'text', 'text': '1 '}, {'type': 'text', 'text': 'title'}],
                    '日期': [{'type': 'text', 'text': '2026-09-09'}],
                    '音频': [{'name': audio.name, 'file_token': 'existing-token'}]
                }}], 'has_more': False}}
        pytest.fail('Existing rich-text row must not be recreated or uploaded: ' + url)
    base = FeishuBase('base', auth='app', app_id='id', app_secret='secret', http=FakeHTTP(handler))
    assert base.sync_audio('tbl', 1, 'title', '2026-09-09', [audio]) == 0
