"""Shared HTTP transport; no dependency on collection or generation."""

import time

import requests

USER_AGENT = "article-generation/0.2 (personal research digest)"


def request_bytes(url):
    for attempt in range(3):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(15, 90))
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError(f"获取失败：{url}") from None
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"获取失败：{url}")


def fetch(url, *, limit=50 * 1024 * 1024):
    """Bounded HTTP fetch for arbitrary materials; retain final URL and MIME type."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("资料链接必须为有效 HTTP(S) 地址，不能包含账号密码。")
    try:
        with requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=(15, 90), stream=True
        ) as response:
            response.raise_for_status()
            parts, size = [], 0
            for part in response.iter_content(65536):
                size += len(part)
                if size > limit:
                    raise ValueError("下载资料超过大小限制。")
                parts.append(part)
            return {
                "content": b"".join(parts),
                "url": response.url,
                "content_type": response.headers.get("Content-Type", ""),
                "encoding": response.encoding
                if "charset=" in response.headers.get("Content-Type", "").lower()
                else None,
            }
    except requests.RequestException:
        raise ValueError("资料下载失败，请检查链接与网络。") from None


def decode_text(raw, encoding=None):
    """Honor explicit HTTP/HTML charset; prefer UTF-8 rather than requests' Latin-1 default."""
    import re

    if encoding is None:
        declared = re.search(rb'charset\s*=\s*["\']?([A-Za-z0-9_-]+)', raw[:8192], re.I)
        encoding = declared[1].decode("ascii") if declared else "utf-8-sig"
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("正文编码无法识别，请转换为 UTF-8 或提供正确 charset。") from None
