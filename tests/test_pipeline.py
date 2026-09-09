from datetime import date
from types import SimpleNamespace
import pytest
from qiniu_get.pipeline import select_sessions, message_rows, stage_for_title, run_session, run_lock


def test_shanghai_day_filter_and_live_id_dedup():
    rows = [{'id': 1, 'start_time': '2026-09-08T16:01:00Z'},
            {'id': 2, 'start_time': '2026-09-09T16:01:00Z'},
            {'id': 1, 'start_time': '2026-09-08T16:01:00Z'}]
    assert [r['id'] for r in select_sessions(rows, date(2026, 9, 9))] == [1]


def test_message_id_dedup_and_user_fallback():
    messages = [{'msg_id': 'a', 'timestamp': 0, 'text': 'hi', 'youin_user_id': None, 'oath_user_id': '7'},
                {'msg_id': 'a', 'timestamp': 0, 'text': 'hi'},
                {'msg_id': 'b', 'timestamp': 0, 'text': 'hi'}]
    rows = message_rows(messages, 1)
    assert len(rows) == 2
    assert rows[0] == ['hi', '1970-01-01 08:00:00', '', '7', '1']


def test_ambiguous_stage_rejected():
    assert stage_for_title('【初高】课程') == '初高'
    with pytest.raises(ValueError):
        stage_for_title('小学和高中')


def test_danmaku_only_never_fetches_recording(tmp_path):
    api = SimpleNamespace(danmaku=lambda live_id: iter([{'msg_id': 'a', 'timestamp': 0, 'text': 'x'}]))
    class Base:
        def validate(self, *args): pass
        def sync_danmaku(self, table, live, rows): return len(rows)
    config = SimpleNamespace(output_dir=tmp_path, danmaku_tables={'小学': 'tbl1'})
    result = run_session(api, Base(), None, config,
                         {'id': 1, 'title': '小学课程', 'start_time': '2026-09-09T08:00:00'}, 'danmaku')
    assert result['danmaku_added'] == 1
    assert (tmp_path / '1' / 'danmaku.json').exists()


def test_lock_prevents_two_writers_on_same_machine(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(RuntimeError, match='already running'):
            with run_lock(tmp_path):
                pass


def test_bad_cached_source_is_invalidated_for_next_run(tmp_path):
    from qiniu_get.media import MediaError
    class Media:
        def download(self, url, target, **kwargs):
            target.write_bytes(b'truncated')
            return target
        def segment(self, *args):
            raise MediaError('Audio segments do not cover source')
    class Base:
        def validate(self, *args): pass
    api = SimpleNamespace(recording=lambda live_id: {'file_url': 'https://example.com/video.mp4'})
    config = SimpleNamespace(output_dir=tmp_path, audio_table='tbl1', segment_seconds=3600)
    with pytest.raises(MediaError):
        run_session(api, Base(), Media(), config,
                    {'id': 1, 'title': 'title', 'start_time': '2026-09-09T08:00:00'}, 'audio')
    assert not (tmp_path / '1' / 'video.mp4').exists()
