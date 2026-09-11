from contextlib import contextmanager, nullcontext
import sys
from datetime import datetime
import csv
from filelock import FileLock, Timeout
import json
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from .feishu import DANMAKU_FIELDS
from .media import AudioCoverageError, MediaError

TZ = ZoneInfo('Asia/Shanghai')


def local_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def select_sessions(sessions, day):
    result, seen = [], set()
    for session in sessions:
        if local_time(session['start_time']).date() == day and session['id'] not in seen:
            seen.add(session['id'])
            result.append(session)
    return result


def stage_for_title(title):
    matches = [stage for stage in ('小学', '初中', '高中', '初高') if stage in title]
    if len(matches) != 1:
        raise ValueError('直播标题必须含且仅含一种分类：小学/初中/高中/初高')
    return matches[0]


def message_rows(messages, live_id):
    rows, seen = [], set()
    for item in messages:
        message_id = item.get('msg_id')
        if message_id is not None:
            if str(message_id) in seen:
                continue
            seen.add(str(message_id))
        timestamp = float(item['timestamp']) / 1000
        when = datetime.fromtimestamp(timestamp, TZ).strftime('%Y-%m-%d %H:%M:%S')
        rows.append([str(item.get('text') or ''), when, str(item.get('from_user_name') or ''),
                     str(item.get('youin_user_id') or item.get('oath_user_id') or ''), str(live_id)])
    return rows


class CollectorBusyError(RuntimeError):
    """Another local collector owns the output directory."""


@contextmanager
def run_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / '.collector.lock')
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise CollectorBusyError('Collector already running with this output directory') from None
    try:
        yield
    finally:
        lock.release()



def run_session(api, base, media, config, session, only='both', *, stage_locks=None):
    stage_locks = stage_locks or {}
    live_id = int(session['id'])
    title = session.get('title') or str(live_id)
    day = local_time(session['start_time']).date().isoformat()
    directory = config.output_dir / str(live_id)
    directory.mkdir(parents=True, exist_ok=True)
    result = {'live_id': live_id, 'danmaku_added': 0, 'audio_added': 0}
    # Validate the target before downloading large files or writing any rows.
    if only in ('both', 'danmaku'):
        stage = stage_for_title(title)
        table = config.danmaku_tables.get(stage)
        if not table:
            raise ValueError(f'Missing danmaku table for {stage}')
        base.validate(table, dict.fromkeys(DANMAKU_FIELDS, 'text'))
    if only in ('both', 'audio'):
        base.validate(config.audio_table, {'名称': 'text', '日期': 'text', '音频': 'attachment'})
    if only in ('both', 'danmaku'):
        messages = list(api.danmaku(live_id))
        rows = message_rows(messages, live_id)
        temporary = directory / 'danmaku.json.tmp'
        temporary.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'danmaku.json')
        with (directory / 'danmaku.csv').open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(DANMAKU_FIELDS)
            writer.writerows(rows)
        with stage_locks.get('upload', nullcontext()):
            result['danmaku_added'] = base.sync_danmaku(table, live_id, rows)
    if only in ('both', 'audio'):
        recording = api.recording(live_id)
        url = recording.get('file_url') or recording['play_url']
        hls = not recording.get('file_url') or urlparse(url).path.lower().endswith('.m3u8')
        print(f'Live {live_id}: downloading or reusing video', file=sys.stderr, flush=True)
        source = media.download(url, directory / 'video.mp4', hls=hls)
        try:
            with stage_locks.get('segment', nullcontext()):
                print(f'Live {live_id}: checking or splitting audio', file=sys.stderr, flush=True)
                parts = media.segment(source, directory / f'audio_{config.segment_seconds}', live_id, config.segment_seconds)
        except AudioCoverageError:
            # Keep evidence: timestamp gaps or shorter audio can also cause mismatch.
            raise
        except MediaError:
            # A readable MP4 header does not prove the entire download is decodable.
            # Invalidate only our own cache so the next run can fetch fresh bytes.
            source.unlink(missing_ok=True)
            raise
        with stage_locks.get('upload', nullcontext()):
            print(f'Live {live_id}: syncing audio attachments', file=sys.stderr, flush=True)
            result['audio_added'] = base.sync_audio(config.audio_table, live_id, title, day, parts)
    return result
