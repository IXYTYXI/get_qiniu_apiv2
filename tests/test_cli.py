import pytest
from qiniu_get.cli import parser
from qiniu_get.config import load_config


def test_run_requires_explicit_date_or_live_id():
    with pytest.raises(SystemExit):
        parser().parse_args(['run'])
    args = parser().parse_args(['run', '--live-id', '123', '--only', 'danmaku', '--dry-run'])
    assert args.live_id == [123] and args.dry_run


def test_config_rejects_invalid_chunk_size(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[media]\nsegment_seconds = 0\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_dry_run_never_constructs_media_or_writes_base(tmp_path, monkeypatch, capsys):
    from qiniu_get import cli
    config = tmp_path / 'config.toml'
    config.write_text('')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    class Api:
        def __init__(self, *args): pass
        def live_info(self, live_id):
            return {'title': '小学课程', 'start_time': '2026-09-09T08:00:00'}
        def close(self): pass
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    def forbidden(*args, **kwargs):
        raise AssertionError('dry-run caused a side effect')
    monkeypatch.setattr(cli, 'MediaProcessor', forbidden)
    monkeypatch.setattr(cli.FeishuBase, '_call', forbidden)
    assert cli.main(['--config', str(config), 'run', '--live-id', '1', '--dry-run']) == 0
    assert '小学课程' in capsys.readouterr().out
    assert not (tmp_path / 'data').exists()


def test_partial_failure_continues_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    from qiniu_get import cli
    config = tmp_path / 'config.toml'
    config.write_text('[feishu]\nbase_token="test"\n')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    class Api:
        def __init__(self, *args): pass
        def live_info(self, live_id): return {'title': '小学', 'start_time': '2026-09-09T08:00:00'}
        def close(self): pass
    seen = []
    def run(*args):
        live_id = args[4]['id']
        seen.append(live_id)
        if live_id == 1:
            raise ValueError('missing media')
        return {'live_id': live_id}
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    monkeypatch.setattr(cli, 'run_session', run)
    assert cli.main(['--config', str(config), 'run', '--live-id', '1', '--live-id', '2', '--only', 'danmaku']) == 1
    assert seen == [1, 2]
