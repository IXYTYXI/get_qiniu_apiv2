import pytest
from qiniu_get.cli import parser
from qiniu_get.config import load_config


def test_run_requires_explicit_date_or_live_id():
    with pytest.raises(SystemExit):
        parser().parse_args(['run'])
    args = parser().parse_args(['run', '--live-id', '123', '--only', 'danmaku', '--dry-run'])
    assert args.live_id == [123] and args.dry_run


def test_config_rejects_invalid_feishu_auth(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[feishu]\nauth = "bot"\n')
    with pytest.raises(ValueError, match='feishu.auth'):
        load_config(path)


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


def test_date_discovery_does_not_skip_same_day_completions(tmp_path, monkeypatch, capsys):
    from qiniu_get import cli
    from datetime import date
    config = tmp_path / 'config.toml'; config.write_text('')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    class Api:
        def __init__(self, *args): pass
        def sessions(self, end_date):
            assert end_date == date(2026, 8, 24)
            return [{'id': 1, 'title': 'test', 'start_time': '2026-08-24T08:00:00'}]
        def close(self): pass
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    assert cli.main(['--config', str(config), 'run', '--date', '2026-08-24', '--dry-run']) == 0


def test_busy_collector_explains_lock_without_running_session(tmp_path, monkeypatch, capsys):
    from qiniu_get import cli
    from qiniu_get.pipeline import run_lock
    config = tmp_path / 'config.toml'
    config.write_text('[feishu]\nbase_token="test"\n')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    class Api:
        def __init__(self, *args): pass
        def live_info(self, live_id): return {'title': 'test', 'start_time': '2026-09-09T08:00:00'}
        def close(self): pass
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    def forbidden(*args):
        pytest.fail('Busy collector started work')
    monkeypatch.setattr(cli, 'run_session', forbidden)
    with run_lock(tmp_path / 'data'):
        assert cli.main(['--config', str(config), 'run', '--live-id', '1', '--only', 'danmaku']) == 1
    assert 'Collector already running with this output directory' in capsys.readouterr().err
