"""联网搜索保底实现：DuckDuckGo HTML 版（标准库 urllib，零第三方依赖）。

宿主联网优先；本实现供 CLI 独立运行时保底。
搜索失败不阻断流程：返回 ok=False，调用方降级为无跑分模式。
"""
from __future__ import annotations

import html as html_module
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .client import USER_AGENT

SEARCH_URL = "https://html.duckduckgo.com/html/"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_RESULTS = 8

_LINK_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
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
    links = _LINK_RE.findall(page)
    snippets = _SNIPPET_RE.findall(page)
    padded = snippets + [""] * len(links)
    results: list[dict[str, str]] = []
    for (href, title), snippet in zip(links, padded):
        url = _clean_url(href)
        if not url:
            continue
        results.append(
            {
                "title": _clean_text(title),
                "url": url,
                "snippet": _clean_text(snippet),
            }
        )
        if len(results) >= MAX_RESULTS:
            break
    return results


def _clean_url(href: str) -> str:
    if "uddg=" in href:
        raw = "https:" + href if href.startswith("//") else href
        query = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        encoded = query.get("uddg", [""])[0]
        if encoded:
            return encoded
    if href.startswith(("http://", "https://")):
        return href
    return ""


def _clean_text(fragment: str) -> str:
    return html_module.unescape(_TAG_RE.sub("", fragment)).strip()
