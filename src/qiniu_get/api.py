"""Youin livestream API protocol; credentials never enter logs."""
import time
import httpx


class ApiError(RuntimeError):
    pass


class QiniuClient:
    def __init__(self, base_url, app_id, app_secret, enterprise_id, *, client=None,
                 sleep=time.sleep, max_pages=10000):
        self.base_url = base_url.rstrip('/')
        self.app_id, self.app_secret = app_id, app_secret
        self.enterprise_id = enterprise_id
        self.http = client or httpx.Client(timeout=60)
        self.sleep, self.max_pages = sleep, max_pages
        self.token, self.expires = None, 0

    def close(self):
        self.http.close()

    def _authenticate(self):
        payload = self._request('POST', '/v1/account/auth-token/', auth=False,
                                json={'app_id': self.app_id, 'app_secret': self.app_secret})
        result = payload.get('result') or {}
        self.token = result.get('access_token')
        if not self.token:
            raise ApiError('API authentication returned no access_token')
        self.expires = float(result.get('exprise_time') or time.time() + 3600)

    def _request(self, method, path, *, auth=True, **kwargs):
        refreshed = False
        for attempt in range(4):
            if auth and (not self.token or time.time() >= self.expires - 60):
                self._authenticate()
            headers = {'Authorization': f'jwt {self.token}'} if auth else {}
            try:
                response = self.http.request(method, self.base_url + path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise ApiError('API transport failed after retries') from None
                self.sleep(0.5 * 2 ** attempt)
                continue
            if response.status_code == 401 and auth and not refreshed:
                self.token = None
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    self.sleep(min(30, 0.5 * 2 ** attempt))
                    continue
            if not response.is_success:
                raise ApiError(f'API HTTP {response.status_code}')
            try:
                payload = response.json()
            except ValueError:
                raise ApiError('API returned invalid JSON') from None
            if not isinstance(payload, dict) or payload.get('code') != 200:
                raise ApiError('API returned unsuccessful business response')
            return payload
        raise ApiError('API authentication/retry limit reached')

    def sessions(self, end_date):
        seen = set()
        for page in range(1, self.max_pages + 1):
            data = self._request('GET', '/v1/course/system/over_course/',
                                 params={'page': page, 'endDate': str(end_date), 'enterprise_id': self.enterprise_id})
            rows = data.get('result')
            if not isinstance(rows, list):
                raise ApiError('Session response result must be a list')
            ids = tuple(str(row['id']) for row in rows)
            if ids in seen and data.get('next'):
                raise ApiError('Session pagination repeated a page')
            seen.add(ids)
            yield from rows
            if not data.get('next'):
                return
        raise ApiError('Session page limit exceeded')

    def live_info(self, live_id):
        return self._request('GET', f'/v1/course/system/live_info/{int(live_id)}/')['result']

    def recording(self, live_id):
        result = self._request('GET', f'/v1/course/system/get_vod/{int(live_id)}/').get('result') or {}
        if not (result.get('file_url') or result.get('play_url')):
            raise ApiError('Recording is not ready; rerun later')
        return result

    def danmaku(self, live_id):
        params, seen = {}, set()
        for _ in range(self.max_pages):
            data = self._request('GET', f'/v1/course/push_message/{int(live_id)}/', params=params)
            body = data.get('result') or {}
            if 'msg_over' not in body or not isinstance(body.get('msg_data'), list):
                raise ApiError('Danmaku response lacks completion flag or message list')
            yield from body['msg_data']
            if body['msg_over'] is True or body['msg_over'] == 1:
                return
            cursor = (body.get('last_msg_id'), body.get('last_msg_timstamp'))
            if cursor[0] is None or cursor[1] is None or cursor in seen:
                raise ApiError('Danmaku cursor missing or repeated; refusing incomplete success')
            seen.add(cursor)
            params = {'msg_id': cursor[0], 'msg_timstamp': cursor[1]}
        raise ApiError('Danmaku page limit exceeded')
