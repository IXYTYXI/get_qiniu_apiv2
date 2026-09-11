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
    def run(*args, **kwargs):
        live_id = args[4]['id']
        seen.append(live_id)
        if live_id == 1:
            raise ValueError('missing media')
        return {'live_id': live_id}
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    monkeypatch.setattr(cli, 'run_session', run)
    assert cli.main(['--config', str(config), 'run', '--live-id', '1', '--live-id', '2', '--only', 'danmaku', '--workers', '1']) == 1
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


def test_three_workers_overlap_with_isolated_clients_and_failure_cleanup(tmp_path, monkeypatch, capsys):
    import threading
    from qiniu_get import cli
    config = tmp_path / 'config.toml'
    config.write_text('[feishu]\nbase_token="test"\naudio_table="tbl"\n')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    created, closed, clients = [], [], []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)
    class Client:
        def __init__(self, *args):
            with lock: created.append(self)
        def live_info(self, live_id): return {'title': 'test', 'start_time': '2026-09-09T08:00:00'}
        def close(self):
            with lock: closed.append(self)
    class Base(Client):
        @classmethod
        def from_config(cls, config): return cls()
    monkeypatch.setattr(cli, 'QiniuClient', Client)
    monkeypatch.setattr(cli, 'MediaProcessor', Client)
    monkeypatch.setattr(cli, 'FeishuBase', Base)
    def run(api, base, media, config, session, only, **kwargs):
        with lock: clients.append((api, base, media))
        if session['id'] <= 3:
            barrier.wait()
        if session['id'] == 2:
            raise ValueError('test failure')
        return {'live_id': session['id']}
    monkeypatch.setattr(cli, 'run_session', run)
    args = ['--config', str(config), 'run', '--only', 'audio', '--workers', '3']
    for live_id in (1, 2, 3, 4): args += ['--live-id', str(live_id)]
    assert cli.main(args) == 1
    import json
    result = json.loads(capsys.readouterr().out)
    assert [r['live_id'] for r in result['completed']] == [1, 3, 4]
    assert result['failed'] == [{'live_id': 2, 'error': 'test failure'}]
    assert len(clients) == 4
    for index in range(3): assert len({id(c[index]) for c in clients}) == 4
    assert {id(c) for c in created} == {id(c) for c in closed}
    assert len(closed) == len(created)


def test_workers_default_and_bounds():
    assert parser().parse_args(['run', '--live-id', '1']).workers == 3
    for workers in ('0', '-1', '4'):
        with pytest.raises(SystemExit):
            parser().parse_args(['run', '--live-id', '1', '--workers', workers])


def test_three_downloads_transcodes_and_uploads_overlap(tmp_path, monkeypatch, capsys):
    import threading
    import time
    from qiniu_get import cli
    config = tmp_path / 'config.toml'
    config.write_text('[feishu]\nbase_token="test"\naudio_table="tbl"\n')
    for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID'):
        monkeypatch.setenv(key, '1')
    downloads = threading.Barrier(3, timeout=5)
    transcodes = threading.Barrier(3, timeout=5)
    uploads = threading.Barrier(3, timeout=5)
    active = [0]
    maximum = [0]
    lock = threading.Lock()
    class Api:
        def __init__(self, *args): pass
        def live_info(self, live_id): return {'title': 'test', 'start_time': '2026-09-09T08:00:00'}
        def recording(self, live_id): return {'file_url': 'https://example.com/video.mp4'}
        def close(self): pass
    class Base:
        @classmethod
        def from_config(cls, config): return cls()
        def validate(self, *args): pass
        def close(self): pass
        def sync_audio(self, *args):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            uploads.wait()
            with lock: active[0] -= 1
            return 1
    class Media:
        def download(self, url, target, **kwargs):
            downloads.wait()
            return target
        def segment(self, source, directory, live_id, seconds):
            transcodes.wait()
            return [directory / 'part.mp3']
        def close(self): pass
    monkeypatch.setattr(cli, 'QiniuClient', Api)
    monkeypatch.setattr(cli, 'FeishuBase', Base)
    monkeypatch.setattr(cli, 'MediaProcessor', Media)
    assert cli.main(['--config', str(config), 'run', '--only', 'audio',
                     '--live-id', '1', '--live-id', '2', '--live-id', '3']) == 0
    assert maximum[0] == 3
    import json
    assert len(json.loads(capsys.readouterr().out)['completed']) == 3
