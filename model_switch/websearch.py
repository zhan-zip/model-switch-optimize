"""联网搜索保底实现：Bing HTML 版（标准库 urllib，零第三方依赖）。

宿主联网优先；本实现供 CLI 独立运行时保底。
搜索失败不阻断流程：返回 ok=False，调用方降级为无跑分模式。

换源说明：原 DuckDuckGo HTML 端点（html.duckduckgo.com）对部分网络环境返回
反爬挑战页（anomaly），解析恒为空；实测 Bing（www.bing.com/search）在当前
环境返回可解析的自然结果，故换用 Bing。
"""
from __future__ import annotations

import base64
import html as html_module
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .client import USER_AGENT

SEARCH_URL = "https://www.bing.com/search"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESULTS = 8

_ALGO_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_H2_RE = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def default_web_search(
    query: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """搜索 query；返回 {"ok", "query", "results": [{title, url, snippet}]} 或错误。"""

    url = SEARCH_URL + "?" + urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,  # 网关拦截默认 urllib UA 的教训（见 client.py）
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "query": query,
            "error_class": "5xx" if exc.code >= 500 else "unknown",
            "detail": f"HTTP {exc.code}",
        }
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower():
            return {"ok": False, "query": query, "error_class": "timeout", "detail": reason[:200]}
        return {"ok": False, "query": query, "error_class": "network", "detail": reason[:200]}
    except TimeoutError:
        return {"ok": False, "query": query, "error_class": "timeout", "detail": "search timed out"}

    return {"ok": True, "query": query, "results": _parse_results(page)}


def _parse_results(page: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for block in _ALGO_RE.findall(page):
        h2 = _H2_RE.search(block)
        if h2 is None:
            continue
        url = _clean_url(h2.group(1))
        if not url:
            continue
        p = _P_RE.search(block)
        results.append(
            {
                "title": _clean_text(h2.group(2)),
                "url": url,
                "snippet": _clean_text(p.group(1)) if p else "",
            }
        )
        if len(results) >= MAX_RESULTS:
            break
    return results


def _clean_url(href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if "u=" in href:  # Bing 重定向形如 /ck/a?...&u=<base64>
        query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        raw = query.get("u", [""])[0]
        if raw:
            try:
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
                    "utf-8", errors="replace"
                )
                if decoded.startswith(("http://", "https://")):
                    return decoded
            except (ValueError, UnicodeDecodeError):
                pass
    return ""


def _clean_text(fragment: str) -> str:
    return html_module.unescape(_TAG_RE.sub("", fragment)).strip()
