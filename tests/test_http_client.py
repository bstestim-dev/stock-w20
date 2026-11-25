import json

import pytest
import requests

from src.fetchers.http import FetchError, HttpClient, HttpStatusError, QuotaExceeded


class FakeResponse:
    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.content = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def make(responses, **kw):
    t = FakeTime()
    sess = FakeSession(responses)
    c = HttpClient(session=sess, sleep=t.sleep, clock=t.clock, **kw)
    return c, sess, t


def test_success_sends_user_agent_and_params():
    c, sess, _ = make([FakeResponse(body={"stat": "OK"})])
    assert c.get_json("https://x/y", params={"a": 1}) == {"stat": "OK"}
    assert "Mozilla/5.0" in sess.calls[0]["headers"]["User-Agent"]
    assert sess.calls[0]["params"] == {"a": 1}
    assert sess.calls[0]["timeout"] == 20.0


def test_polite_delay_between_requests():
    c, _, t = make([FakeResponse(body=1), FakeResponse(body=2)], delay_sec=0.7)
    c.get_json("u")
    c.get_json("u")
    assert t.sleeps == [pytest.approx(0.7)]


@pytest.mark.parametrize(
    "first",
    [
        FakeResponse(status=429, body={}),
        FakeResponse(status=503, body={}),
        FakeResponse(raw="您的查詢太頻繁，請稍待再查詢".encode("utf-8")),
        FakeResponse(raw=b"<html>oops</html>"),
        requests.ConnectionError("boom"),
        requests.Timeout("slow"),
    ],
)
def test_retryable_errors_then_success(first):
    c, sess, t = make([first, FakeResponse(body={"ok": 1})], delay_sec=0)
    assert c.get_json("u") == {"ok": 1}
    assert t.sleeps == [3.0]               # 第 1 次重試等 3 秒
    assert len(sess.calls) == 2


def test_backoff_schedule_and_give_up():
    c, sess, t = make([FakeResponse(status=500, body={})] * 4, delay_sec=0)
    with pytest.raises(FetchError, match="重試 3 次"):
        c.get_json("u")
    assert t.sleeps == [3.0, 8.0, 20.0]    # spec 第 9 節
    assert len(sess.calls) == 4
    assert c.logs[-1].error == "HTTP 500"


def test_client_error_not_retried():
    c, sess, _ = make([FakeResponse(status=404, body={})])
    with pytest.raises(HttpStatusError) as ei:
        c.get_json("u")
    assert ei.value.status == 404 and len(sess.calls) == 1


def test_quota_402_raises_specific_error():
    c, _, _ = make([FakeResponse(status=402, body={"msg": "Requests reach the upper limit"})])
    with pytest.raises(QuotaExceeded):
        c.get_json("u")


def test_from_config():
    cfg = {"data_source": {"request_delay_sec": 1.0, "timeout_sec": 15, "max_retries": 2}}
    c = HttpClient.from_config(cfg, session=FakeSession([]))
    assert (c.delay_sec, c.timeout_sec, c.max_retries) == (1.0, 15.0, 2)
