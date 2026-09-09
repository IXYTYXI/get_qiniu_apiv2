"""User-identity Base adapter using documented lark-cli shortcuts."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile

DANMAKU_FIELDS = ['内容', '时间', '用户', '用户ID', '直播ID']


class FeishuError(RuntimeError):
    pass


def decode_records(data):
    fields, rows, ids = data.get('fields'), data.get('data'), data.get('record_id_list')
    if not isinstance(fields, list) or not isinstance(rows, list) or not isinstance(ids, list):
        raise FeishuError('Unsupported lark-cli record response; expected fields/data/record_id_list')
    if len(rows) != len(ids) or any(len(row) != len(fields) for row in rows):
        raise FeishuError('Inconsistent lark-cli record response')
    return [{'id': rid, 'fields': dict(zip(fields, row))} for rid, row in zip(ids, rows)]


def missing_rows(rows, existing):
    counts = Counter(tuple('' if v is None else v for v in row) for row in existing)
    missing = []
    for row in rows:
        key = tuple('' if v is None else v for v in row)
        if counts[key]:
            counts[key] -= 1
        else:
            missing.append(row)
    return missing


class FeishuBase:
    def __init__(self, base_token, *, cli='lark-cli', profile=None, runner=subprocess.run):
        self.base_token, self.cli, self.profile, self.runner = base_token, cli, profile, runner

    def _call(self, command, table, *args, payload=None):
        argv = [self.cli]
        if self.profile:
            argv += ['--profile', self.profile]
        argv += ['base', command, '--base-token', self.base_token, '--table-id', table,
                 '--as', 'user', '--format', 'json', *map(str, args)]
        # JSON via private temp file avoids ARG_MAX for batches and exposes no text in process argv.
        with tempfile.TemporaryDirectory(prefix='qiniu-lark-') as directory:
            cwd = None
            if '--file' in argv:
                file_index = argv.index('--file') + 1
                file_path = Path(argv[file_index]).resolve()
                cwd = str(file_path.parent)
                argv[file_index] = file_path.name
            if payload is not None:
                path = Path(directory) / 'request.json'
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                path.chmod(0o600)
                argv += ['--json', '@' + path.name]
                cwd = directory
            try:
                result = self.runner(argv, capture_output=True, text=True, encoding="utf-8", timeout=7200, cwd=cwd)
            except (OSError, subprocess.SubprocessError):
                raise FeishuError(f'{command} could not complete; check lark-cli installation/auth and rerun') from None
            if result.returncode:
                raise FeishuError(f'{command} failed (exit {result.returncode}); check user authorization and table access')
            try:
                response = json.loads(result.stdout)
            except ValueError:
                raise FeishuError(f'{command} returned invalid JSON') from None
            if response.get('ok') is not True:
                raise FeishuError(f'{command} returned unsuccessful response')
            return response.get('data', {})

    def validate(self, table, required):
        fields, offset = {}, 0
        while True:
            data = self._call('+field-list', table, '--offset', offset, '--limit', 200)
            page = data.get('fields', [])
            fields.update({f['name']: f['type'] for f in page})
            offset += len(page)
            if offset >= data.get('total', offset):
                break
            if not page:
                raise FeishuError('Field pagination stopped before total')
        for name, expected in required.items():
            if fields.get(name) != expected:
                raise FeishuError(f'Table {table}: {name} must be {expected}; got {fields.get(name)}')

    def records(self, table, fields, conditions):
        offset, seen = 0, set()
        while True:
            args = ['--offset', offset, '--limit', 200,
                    '--filter-json', json.dumps({'logic': 'and', 'conditions': conditions}, ensure_ascii=False)]
            for field in fields:
                args += ['--field-id', field]
            data = self._call('+record-list', table, *args)
            page = decode_records(data)
            for row in page:
                if row['id'] in seen:
                    raise FeishuError('Base pagination repeated a record')
                seen.add(row['id'])
                yield row
            if not data.get('has_more'):
                return
            if not page:
                raise FeishuError('Base pagination empty with has_more=true')
            offset += len(page)

    def batch_create(self, table, fields, rows):
        for start in range(0, len(rows), 200):
            self._call('+record-batch-create', table,
                       payload={'fields': fields, 'rows': rows[start:start + 200]})

    def sync_danmaku(self, table, live_id, rows):
        existing = [list(record['fields'].get(k) for k in DANMAKU_FIELDS)
                    for record in self.records(table, DANMAKU_FIELDS, [['直播ID', '==', str(live_id)]])]
        pending = missing_rows(rows, existing)
        self.batch_create(table, DANMAKU_FIELDS, pending)
        return len(pending)

    def sync_audio(self, table, live_id, title, day, paths):
        fields = ['名称', '日期', '音频']
        records = list(self.records(table, fields, [['名称', 'intersects', f'{live_id} '], ['日期', '==', day]]))
        records = [r for r in records if str(r['fields'].get('名称') or '').split(' ', 1)[0] == str(live_id)]
        if len(records) > 1:
            raise FeishuError(f'Multiple audio rows for live {live_id}; resolve duplicate rows before retry')
        if not records:
            self._call('+record-upsert', table, payload={'名称': f'{live_id} {title}', '日期': day})
            records = list(self.records(table, fields, [['名称', '==', f'{live_id} {title}'], ['日期', '==', day]]))
            if len(records) != 1:
                raise FeishuError('Created audio row could not be uniquely read back; rerun after checking Base')
        record = records[0]
        attachments = record['fields'].get('音频') or []
        existing = {a['name'] for a in attachments}
        names = {p.name for p in paths}
        if existing - names:
            raise FeishuError(f'Live {live_id} already has audio with a different naming/segment scheme; refusing to duplicate it')
        count = 0
        for path in paths:
            if path.name in existing:
                continue
            # One attachment per call: next run reconciles remote filenames after an uncertain upload.
            self._call('+record-upload-attachment', table, '--record-id', record['id'],
                       '--field-id', '音频', '--file', str(path.resolve()))
            count += 1
        return count
