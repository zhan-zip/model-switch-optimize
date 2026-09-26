"""client 模块测试：OpenAI 兼容调用与错误分类（注入 fake opener，无真实网络）。"""
import io
import json
import urllib.error

from model_switch.client import ErrorClass, call_openai_compatible


class _FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_opener(payload=None):
    body = payload or {
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def open(req, timeout):
        return _FakeResponse(body)

    return open


def _http_error_opener(code, body="{}"):
    def open(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, code, "err", hdrs=None, fp=io.BytesIO(body.encode("utf-8"))
        )

    return open


def _raise_opener(exc):
    def open(req, timeout):
        raise exc

    return open


def _call(opener):
    return call_openai_compatible(
        "https://x.example.com/v1",
        "sk-test",
        "m1",
        [{"role": "user", "content": "ping"}],
        opener=opener,
    )


def test_success_parses_text_and_usage():
    result = _call(_ok_opener())
    assert result.ok is True
    assert result.text == "pong"
    assert result.usage["total_tokens"] == 2
    assert result.error_class == ""


def test_401_classified_as_auth():
    assert _call(_http_error_opener(401)).error_class == ErrorClass.AUTH


def test_403_classified_as_auth():
    assert _call(_http_error_opener(403)).error_class == ErrorClass.AUTH


def test_429_classified_as_rate_limit():
    assert _call(_http_error_opener(429)).error_class == ErrorClass.RATE_LIMIT


def test_500_classified_as_server():
    assert _call(_http_error_opener(500)).error_class == ErrorClass.SERVER


def test_404_classified_as_unknown():
    assert _call(_http_error_opener(404)).error_class == ErrorClass.UNKNOWN


def test_urlerror_timeout_classified_as_timeout():
    exc = urllib.error.URLError(TimeoutError("timed out"))
    assert _call(_raise_opener(exc)).error_class == ErrorClass.TIMEOUT


def test_urlerror_dns_classified_as_network():
    exc = urllib.error.URLError(OSError("name resolution failed"))
    assert _call(_raise_opener(exc)).error_class == ErrorClass.NETWORK


def test_bare_timeout_classified_as_timeout():
    assert _call(_raise_opener(TimeoutError())).error_class == ErrorClass.TIMEOUT


def test_detail_is_redacted():
    result = _call(_http_error_opener(401, '{"error": "bad key sk-abcdefgh1234567890"}'))
    assert "sk-abcdefgh1234567890" not in result.detail
    assert "sk-abcd****" in result.detail


def test_bad_response_shape_classified_as_unknown():
    assert _call(_ok_opener({"unexpected": True})).error_class == ErrorClass.UNKNOWN
