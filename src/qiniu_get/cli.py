import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from dotenv import load_dotenv
from .api import QiniuClient
from .config import load_config
from .feishu import FeishuBase, DANMAKU_FIELDS
from .media import MediaProcessor
from .pipeline import CollectorBusyError, run_session, run_lock, select_sessions


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return parsed


def parser():
    root = argparse.ArgumentParser(description='直播 API → FFmpeg 音频切片 + 弹幕 → 飞书多维表格')
    root.add_argument('--config', default='config.toml', help='TOML configuration file')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('check-base', help='只读检查飞书目标表字段和用户权限，不需要七牛凭证')
    run = commands.add_parser('run')
    selector = run.add_mutually_exclusive_group(required=True)
    selector.add_argument('--date', type=date.fromisoformat, help='按 Asia/Shanghai 开播自然日筛选')
    selector.add_argument('--live-id', type=positive_int, action='append', help='直播ID，可重复传入')
    run.add_argument('--only', choices=['both', 'audio', 'danmaku'], default='both')
    run.add_argument('--dry-run', action='store_true', help='只查询直播，不下载、不写飞书')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    api = media = None
    try:
        config = load_config(args.config)
        load_dotenv(Path(args.config).resolve().parent / '.env')
        base = FeishuBase(config.base_token, cli=config.lark_cli, profile=config.lark_profile)
        if args.command == 'check-base':
            if not config.base_token or not config.audio_table or not config.danmaku_tables:
                raise ValueError('Configure feishu.base_token, audio_table and danmaku_tables first')
            base.validate(config.audio_table, {'名称': 'text', '日期': 'text', '音频': 'attachment'})
            for table in set(config.danmaku_tables.values()):
                base.validate(table, dict.fromkeys(DANMAKU_FIELDS, 'text'))
            print('Base schema checked successfully (read-only, user identity).')
            return 0
        credentials = [os.getenv(key) for key in ('QINIU_APP_ID', 'QINIU_APP_SECRET', 'QINIU_ENTERPRISE_ID')]
        if not all(credentials):
            raise ValueError('Set QINIU_APP_ID, QINIU_APP_SECRET and QINIU_ENTERPRISE_ID in .env')
        api = QiniuClient(config.api_base_url, credentials[0], credentials[1], int(credentials[2]))
        if args.live_id:
            sessions = []
            for live_id in dict.fromkeys(args.live_id):
                info = api.live_info(live_id)
                sessions.append({**info, 'id': live_id})
        else:
            # Live verification: endDate is a completion-date lower bound.
            # Using the next day omits sessions ending on the requested day.
            sessions = select_sessions(api.sessions(args.date), args.date)
        if args.dry_run:
            print(json.dumps([{'id': s['id'], 'title': s.get('title'), 'start_time': s.get('start_time')}
                              for s in sessions], ensure_ascii=False, indent=2))
            return 0
        if not config.base_token or (args.only != 'danmaku' and not config.audio_table):
            raise ValueError('Configure the Feishu destination before running')
        media = MediaProcessor() if args.only != 'danmaku' else None
        completed, failed = [], []
        with run_lock(config.output_dir):
            for session in sessions:
                live_id = session['id']
                print(f'Processing live {live_id}', file=sys.stderr, flush=True)
                try:
                    completed.append(run_session(api, base, media, config, session, args.only))
                except Exception as error:
                    # Only known sanitized domain errors are shown; raw network/subprocess tracebacks are not logged.
                    from .api import ApiError
                    from .feishu import FeishuError
                    from .media import MediaError
                    message = str(error) if isinstance(error, (ApiError, FeishuError, MediaError, ValueError)) else type(error).__name__
                    failed.append({'live_id': live_id, 'error': message})
                    print(f'Live {live_id} failed: {message}', file=sys.stderr, flush=True)
        print(json.dumps({'selected': len(sessions), 'completed': completed, 'failed': failed}, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    except Exception as error:
        from .api import ApiError
        from .feishu import FeishuError
        from .media import MediaError
        message = str(error) if isinstance(error, (ApiError, FeishuError, MediaError, CollectorBusyError, ValueError, FileNotFoundError)) else type(error).__name__
        print(f'Error: {message}', file=sys.stderr)
        return 1
    finally:
        if api:
            api.close()
        if media:
            media.close()


if __name__ == '__main__':
    sys.exit(main())
