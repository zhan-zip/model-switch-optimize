"""websearch 模块测试：Bing HTML 解析与错误分类（注入假 opener）。"""
import urllib.error

from model_switch.websearch import default_web_search

FAKE_HTML = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://example.com/a">GPT-4o &amp; 跑分评测</a></h2>
<div class="b_caption"><p>延迟 <b>1200ms</b>&#x27;性价比高&#x27;</p></div></li>
<li class="b_algo"><h2><a href="//cn.bing.com/ck/a?u=aHR0cHM6Ly9wbGFpbi5leGFtcGxlLmNvbS9i">直接链接标题</a></h2>
<div class="b_caption"><p>第二条摘要</p></div></li>
</ol>
"""


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener_with(html):
    def opener(request, timeout=None):
        return _FakeResponse(html.encode("utf-8"))
    return opener


def test_parse_results_bing_and_decodes_redirect():
    result = default_web_search("gpt-4o 跑分", opener=_opener_with(FAKE_HTML))
    assert result["ok"] is True
    items = result["results"]
    assert items[0]["url"] == "https://example.com/a"
    assert items[0]["title"] == "GPT-4o & 跑分评测"
    assert "1200ms" in items[0]["snippet"] and "性价比高" in items[0]["snippet"]
    assert "<b>" not in items[0]["snippet"]
    # Bing /ck/a 重定向 u=<base64> 解码回真实 URL
    assert items[1]["url"] == "https://plain.example.com/b"


def test_no_results_page_returns_empty_list():
    result = default_web_search("随便", opener=_opener_with("<html><body>无结果</body></html>"))
    assert result["ok"] is True
    assert result["results"] == []


def test_http_error_returns_not_ok():
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)
    result = default_web_search("q", opener=opener)
    assert result["ok"] is False
    assert result["detail"] == "HTTP 403"


def test_url_error_timeout_classified():
    def opener(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))
    result = default_web_search("q", opener=opener)
    assert result["ok"] is False
    assert result["error_class"] == "timeout"


def test_url_error_network_classified():
    def opener(request, timeout=None):
        raise urllib.error.URLError(OSError("connection refused"))
    result = default_web_search("q", opener=opener)
    assert result["ok"] is False
    assert result["error_class"] == "network"
