import httpx
import pytest
from qiniu_get.api import QiniuClient, ApiError


def client(handler):
    return QiniuClient('https://example.com/livestreamapi', 'id', 'secret', 12,
                       client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)


def test_refresh_token_after_401_and_paginate():
    auth = []
    def handler(r):
        if r.url.path.endswith('auth-token/'):
            auth.append(1)
            return httpx.Response(200, json={'code': 200, 'result': {'access_token': f't{len(auth)}'}})
        assert r.url.params['enterprise_id'] == '12'
        if r.headers['Authorization'] == 'jwt t1':
            return httpx.Response(401)
        page = int(r.url.params['page'])
        return httpx.Response(200, json={'code': 200, 'result': [{'id': page}], 'next': 'next' if page == 1 else None})
    assert [x['id'] for x in client(handler).sessions('2026-09-09')] == [1, 2]
    assert len(auth) == 2


def test_barrage_repeated_cursor_is_error():
    def handler(r):
        if r.url.path.endswith('auth-token/'):
            return httpx.Response(200, json={'code': 200, 'result': {'access_token': 't'}})
        return httpx.Response(200, json={'code': 200, 'result': {'msg_data': [{'msg_id': 'm'}], 'msg_over': False, 'last_msg_id': 'm', 'last_msg_timstamp': 1}})
    with pytest.raises(ApiError, match='cursor'):
        list(client(handler).danmaku(1))


def test_barrage_missing_completion_flag_is_not_silent_success():
    def handler(r):
        result = {'access_token': 't'} if r.url.path.endswith('auth-token/') else {'msg_data': []}
        return httpx.Response(200, json={'code': 200, 'result': result})
    with pytest.raises(ApiError):
        list(client(handler).danmaku(1))


def test_retry_transient_but_not_business_error():
    calls = []
    def handler(r):
        if r.url.path.endswith('auth-token/'):
            return httpx.Response(200, json={'code': 200, 'result': {'access_token': 't'}})
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={'code': 400, 'msg': 'private detail'})
    with pytest.raises(ApiError):
        client(handler).recording(1)
    assert len(calls) == 3
