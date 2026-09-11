"""Feishu Base adapter: lark-cli user identity, or Open API tenant_access_token."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import tempfile
import time
from threading import Lock
import os
import httpx

DANMAKU_FIELDS = ['内容', '时间', '用户', '用户ID', '直播ID']
OPEN_FIELD_TYPES = {1: 'text', 17: 'attachment'}
SIMPLE_UPLOAD_LIMIT = 20 * 1024 * 1024
FILTER_OPS = {'==': 'is', 'intersects': 'contains'}


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


def _normalize_open_fields(fields):
    result = dict(fields)
    # Search returns multi-line text as rich-text runs, unlike CLI shortcuts.
    # Only normalize the collector's text columns; keep attachments intact.
    for name in {'名称', '日期', *DANMAKU_FIELDS}:
        value = result.get(name)
        if isinstance(value, list):
            if not all(isinstance(run, dict) and isinstance(run.get('text'), str) for run in value):
                raise FeishuError(f'Unsupported Open API text value for field {name}')
            result[name] = ''.join(run['text'] for run in value)
    return result


def _attachment_tokens(attachments):
    tokens = []
    for item in attachments or []:
        token = item.get('file_token') if isinstance(item, dict) else None
        if not token:
            raise FeishuError('Remote attachment missing file_token; cannot safely append')
        tokens.append({'file_token': token})
    return tokens


class UploadPacer:
    """Space this process's application media requests by at least 250 ms."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = Lock()
        self.next_request = 0.0

    def wait(self):
        with self.lock:
            delay = self.next_request - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.next_request = self.clock() + .25


class FeishuBase:
    def __init__(self, base_token, *, cli='lark-cli', profile=None, runner=subprocess.run,
                 auth='cli', app_id=None, app_secret=None, open_base='https://open.feishu.cn',
                 http=None, sleep=time.sleep):
        self.base_token, self.cli, self.profile, self.runner = base_token, cli, profile, runner
        self.auth = auth
        self.app_id, self.app_secret = app_id, app_secret
        self.open_base = (open_base or 'https://open.feishu.cn').rstrip('/')
        self.sleep = sleep
        self.upload_pacer = UploadPacer()
        self.token, self.expires = None, 0
        self.http = http
        self._owns_http = False
        if auth not in ('cli', 'app'):
            raise FeishuError('feishu.auth must be "cli" or "app"')
        if auth == 'app':
            if not (app_id and app_secret):
                raise FeishuError('Set FEISHU_APP_ID and FEISHU_APP_SECRET in .env')
            self._owns_http = http is None
            self.http = http or httpx.Client(timeout=httpx.Timeout(7200.0, connect=60.0))

    @classmethod
    def from_config(cls, config):
        if config.auth == 'app':
            return cls(config.base_token, auth='app',
                       app_id=os.getenv('FEISHU_APP_ID'), app_secret=os.getenv('FEISHU_APP_SECRET'),
                       open_base=config.open_base)
        return cls(config.base_token, cli=config.lark_cli, profile=config.lark_profile)

    def close(self):
        if self._owns_http:
            self.http.close()

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

    def _authenticate(self):
        try:
            response = self.http.request(
                'POST', f'{self.open_base}/open-apis/auth/v3/tenant_access_token/internal',
                json={'app_id': self.app_id, 'app_secret': self.app_secret})
        except httpx.TransportError:
            raise FeishuError('Open API token request failed') from None
        if response.status_code != 200:
            raise FeishuError(f'Open API token HTTP {response.status_code}')
        try:
            payload = response.json()
        except ValueError:
            raise FeishuError('Open API token returned invalid JSON') from None
        token = payload.get('tenant_access_token') if isinstance(payload, dict) else None
        if payload.get('code') != 0 or not token:
            raise FeishuError('Open API token request unsuccessful')
        self.token = token
        self.expires = time.time() + float(payload.get('expire') or 7200)

    def _open(self, method, path, *, write=False, **kwargs):
        extra_headers = kwargs.pop('headers', None) or {}
        refreshed = False
        attempts = 4 if not write else 1
        attempt = 0
        rate_retries = 0
        file_positions = []
        for value in (kwargs.get('files') or {}).values():
            handle = value[1] if isinstance(value, tuple) else value
            if hasattr(handle, 'seek') and hasattr(handle, 'tell'):
                file_positions.append((handle, handle.tell()))
        while True:
            if not self.token or time.time() >= self.expires - 60:
                self._authenticate()
            headers = dict(extra_headers)
            headers['Authorization'] = f'Bearer {self.token}'
            if path.startswith('/open-apis/drive/v1/medias/upload_'):
                self.upload_pacer.wait()
            for handle, position in file_positions:
                handle.seek(position)
            try:
                response = self.http.request(method, self.open_base + path, headers=headers, **kwargs)
            except httpx.TransportError:
                if write or attempt >= attempts - 1:
                    raise FeishuError('Open API transport failed') from None
                self.sleep(0.5 * 2 ** attempt)
                attempt += 1
                continue
            if response.status_code == 401 and not refreshed:
                self.token = None
                refreshed = True
                continue
            # Explicit rate rejection is safe to retry; ambiguous write failures are not.
            if response.status_code == 429 and rate_retries < 3:
                delay = 2 ** rate_retries
                try:
                    delay = max(delay, float(response.headers.get('Retry-After', delay)))
                except ValueError:
                    pass
                self.sleep(min(60, delay))
                rate_retries += 1
                continue
            if not write and response.status_code >= 500 and attempt < attempts - 1:
                self.sleep(min(30, 0.5 * 2 ** attempt))
                attempt += 1
                continue
            if not response.is_success:
                raise FeishuError(f'Open API HTTP {response.status_code}')
            try:
                payload = response.json()
            except ValueError:
                raise FeishuError('Open API returned invalid JSON') from None
            if not isinstance(payload, dict) or payload.get('code') != 0:
                raise FeishuError('Open API returned unsuccessful business response')
            return payload.get('data') or {}

    def _table(self, table, suffix):
        return f'/open-apis/bitable/v1/apps/{self.base_token}/tables/{table}/{suffix}'

    def validate(self, table, required):
        if self.auth == 'app':
            fields, page_token = {}, None
            while True:
                params = {'page_size': 200}
                if page_token:
                    params['page_token'] = page_token
                data = self._open('GET', self._table(table, 'fields'), params=params)
                page = data.get('items') or []
                for item in page:
                    name = item.get('field_name')
                    mapped = OPEN_FIELD_TYPES.get(item.get('type'))
                    if mapped is None:
                        mapped = str(item.get('ui_type') or item.get('type') or '').lower()
                    fields[name] = mapped
                if not data.get('has_more'):
                    break
                page_token = data.get('page_token')
                if not page or not page_token:
                    raise FeishuError('Field pagination stopped before total')
        else:
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
        if self.auth == 'app':
            yield from self._open_records(table, fields, conditions)
            return
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

    def _open_records(self, table, fields, conditions):
        seen, page_token = set(), None
        try:
            filter_body = {
                'conjunction': 'and',
                'conditions': [{'field_name': name, 'operator': FILTER_OPS[op], 'value': [value]}
                               for name, op, value in conditions],
            }
        except KeyError:
            raise FeishuError('Unsupported record filter operator') from None
        while True:
            body = {'field_names': fields, 'filter': filter_body}
            params = {'page_size': 200}
            if page_token:
                params['page_token'] = page_token
            data = self._open('POST', self._table(table, 'records/search'), params=params, json=body)
            page = data.get('items') or []
            for item in page:
                record_id = item.get('record_id')
                if not record_id:
                    raise FeishuError('Open API record missing record_id')
                if record_id in seen:
                    raise FeishuError('Base pagination repeated a record')
                seen.add(record_id)
                yield {'id': record_id, 'fields': _normalize_open_fields(item.get('fields') or {})}
            if not data.get('has_more'):
                return
            if not page:
                raise FeishuError('Base pagination empty with has_more=true')
            page_token = data.get('page_token')
            if not page_token:
                raise FeishuError('Base pagination missing page_token')

    def batch_create(self, table, fields, rows):
        for start in range(0, len(rows), 200):
            chunk = rows[start:start + 200]
            if self.auth == 'app':
                self._open('POST', self._table(table, 'records/batch_create'), write=True,
                           json={'records': [{'fields': dict(zip(fields, row))} for row in chunk]})
            else:
                self._call('+record-batch-create', table, payload={'fields': fields, 'rows': chunk})

    def _create_audio_row(self, table, title, day):
        if self.auth == 'app':
            self._open('POST', self._table(table, 'records'), write=True,
                       json={'fields': {'名称': title, '日期': day}})
        else:
            self._call('+record-upsert', table, payload={'名称': title, '日期': day})

    def _upload_attachment(self, table, record_id, path, attachments):
        if self.auth == 'app':
            token = self._upload_media(path)
            merged = _attachment_tokens(attachments) + [{'file_token': token}]
            self._open('PUT', f'{self._table(table, "records")}/{record_id}', write=True,
                       json={'fields': {'音频': merged}})
            attachments.append({'file_token': token, 'name': path.name})
            return
        self._call('+record-upload-attachment', table, '--record-id', record_id,
                   '--field-id', '音频', '--file', str(path.resolve()))

    def _upload_media(self, path):
        path = Path(path)
        size = path.stat().st_size
        extra = json.dumps({'drive_route_token': self.base_token}, separators=(',', ':'))
        if size <= SIMPLE_UPLOAD_LIMIT:
            with path.open('rb') as handle:
                data = self._open(
                    'POST', '/open-apis/drive/v1/medias/upload_all', write=True,
                    data={'file_name': path.name, 'parent_type': 'bitable_file',
                          'parent_node': self.base_token, 'size': str(size), 'extra': extra},
                    files={'file': (path.name, handle, 'application/octet-stream')})
            token = data.get('file_token')
            if not token:
                raise FeishuError('Open API upload returned no file_token')
            return token
        prepared = self._open(
            'POST', '/open-apis/drive/v1/medias/upload_prepare', write=True,
            json={'file_name': path.name, 'parent_type': 'bitable_file', 'parent_node': self.base_token,
                  'size': size, 'extra': extra})
        upload_id, block_size = prepared.get('upload_id'), prepared.get('block_size')
        if not upload_id or not block_size:
            raise FeishuError('Open API upload_prepare missing upload_id')
        sent = 0
        with path.open('rb') as handle:
            seq = 0
            while sent < size:
                chunk = handle.read(int(block_size))
                if not chunk:
                    raise FeishuError('Open API upload_part read ended early')
                self._open('POST', '/open-apis/drive/v1/medias/upload_part', write=True,
                           data={'upload_id': upload_id, 'seq': str(seq), 'size': str(len(chunk))},
                           files={'file': (path.name, chunk, 'application/octet-stream')})
                sent += len(chunk)
                seq += 1
        finished = self._open('POST', '/open-apis/drive/v1/medias/upload_finish', write=True,
                              json={'upload_id': upload_id, 'block_num': seq})
        token = finished.get('file_token')
        if not token:
            raise FeishuError('Open API upload_finish returned no file_token')
        return token

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
            self._create_audio_row(table, f'{live_id} {title}', day)
            records = list(self.records(table, fields, [['名称', '==', f'{live_id} {title}'], ['日期', '==', day]]))
            if len(records) != 1:
                raise FeishuError('Created audio row could not be uniquely read back; rerun after checking Base')
        record = records[0]
        attachments = list(record['fields'].get('音频') or [])
        existing = {a['name'] for a in attachments}
        names = {p.name for p in paths}
        if existing - names:
            raise FeishuError(f'Live {live_id} already has audio with a different naming/segment scheme; refusing to duplicate it')
        count = 0
        for path in paths:
            if path.name in existing:
                continue
            # One attachment per call: next run reconciles remote filenames after an uncertain upload.
            self._upload_attachment(table, record['id'], path, attachments)
            existing.add(path.name)
            count += 1
        return count
