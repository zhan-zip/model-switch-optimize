"""websearch 模块测试：DuckDuckGo 简版解析与错误分类（注入假 opener）。"""
import urllib.error

from model_switch.websearch import default_web_search

FAKE_HTML = """
<div class="links_main results links">
<a rel="nofollow" class="result__a"
   href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc">GPT-4o &amp; 跑分评测</a>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">延迟 <b>1200ms</b>&#x27;性价比高&#x27;</a>
<a rel="nofollow" class="result__a"
   href="https://plain.example.com/b">直接链接标题</a>
<a class="result__snippet" href="#">第二条摘要</a>
</div>
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


def test_parse_results_decodes_uddg_and_entities():
    result = default_web_search("gpt-4o 跑分", opener=_opener_with(FAKE_HTML))
    assert result["ok"] is True
    items = result["results"]
    assert items[0]["url"] == "https://example.com/a"
    assert items[0]["title"] == "GPT-4o & 跑分评测"
    assert "1200ms" in items[0]["snippet"] and "性价比高" in items[0]["snippet"]
    assert "<b>" not in items[0]["snippet"]
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
