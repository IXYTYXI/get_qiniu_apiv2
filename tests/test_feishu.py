import json
from pathlib import Path
import subprocess
from collections import Counter
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
