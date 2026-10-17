"""Upload an exported illustrated article to WeChat's draft box (never publish)."""

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import pymupdf
import requests
from markdown_it import MarkdownIt

from shared.wechat import preview, render


class WechatError(RuntimeError):
    def __init__(self, message, *, code=None, uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(repr=False)
class Account:
    app_id: str
    app_secret: str
    author: str = ""

    @classmethod
    def from_environment(cls):
        app_id = os.environ.get("WECHAT_APP_ID", "").strip()
        secret = os.environ.get("WECHAT_APP_SECRET", "").strip()
        if not app_id or not secret:
            raise WechatError("请在本机 .env 设置 WECHAT_APP_ID 和 WECHAT_APP_SECRET。")
        return cls(app_id, secret, os.environ.get("WECHAT_ACCOUNT_NAME", "").strip())


class WechatClient:
    BASE_URL = "https://api.weixin.qq.com"

    def __init__(self, account, session=None):
        self.account = account
        self.session = session or requests.Session()
        self._token = ""
        self._expires_at = 0

    def _redact(self, message):
        for secret in (self.account.app_secret, self._token):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)((?:access_token|secret)=)[^&\s]+", r"\1[REDACTED]", message)

    def _request(self, method, endpoint, **kwargs):
        # No automatic retries: an interrupted draft/add may have created a draft.
        try:
            response = self.session.request(
                method,
                self.BASE_URL + endpoint,
                timeout=(15, 60),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException:
            raise WechatError("微信请求中断或超时；已隐藏敏感请求信息。", uncertain=True) from None
        if not 200 <= response.status_code < 300:
            raise WechatError(f"微信接口 HTTP {response.status_code}；未自动重试。", uncertain=True)
        try:
            # WeChat can return UTF-8 JSON with text/plain and no charset.
            # requests then assigns ISO-8859-1, corrupting otherwise valid CJK.
            result = json.loads(response.content.decode("utf-8-sig"))
        except ValueError:
            raise WechatError("微信接口返回非 JSON 响应。", uncertain=True) from None
        if not isinstance(result, dict):
            raise WechatError("微信接口响应格式无效。", uncertain=True)
        code = result.get("errcode", 0)
        if code:
            detail = self._redact(str(result.get("errmsg", "未知错误")))
            if code == 40164:
                match = re.search(r"invalid ip ([^ ,]+)", detail)
                ip = match[1] if match else "接口提示的出口 IP"
                detail = f"请将本次请求出口 {ip} 加入公众号 IP 白名单。"
            elif code == 48001:
                detail = "账号未获该接口权限，请检查后台的草稿与素材接口权限。"
            elif code == 40125:
                detail = "AppSecret 无效，请检查本机配置；如已重置，请更新配置。"
            raise WechatError(f"微信错误 {code}：{detail}", code=code)
        return result

    def token(self):
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        result = self._request(
            "GET",
            "/cgi-bin/token",
            params={
                "grant_type": "client_credential",
                "appid": self.account.app_id,
                "secret": self.account.app_secret,
            },
        )
        token = result.get("access_token")
        lifetime = result.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(lifetime, (int, float)):
            raise WechatError("微信未返回有效调用凭据。")
        self._token = token
        self._expires_at = time.monotonic() + max(0, lifetime - 120)
        return token

    def post(self, endpoint, payload):
        # Explicit UTF-8 avoids escaped CJK text and incorrect encoding detection.
        return self._request(
            "POST",
            endpoint,
            params={"access_token": self.token()},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def upload(self, image, *, cover=False):
        endpoint = "/cgi-bin/material/add_material" if cover else "/cgi-bin/media/uploadimg"
        params = {"access_token": self.token()}
        if cover:
            params["type"] = "image"
        result = self._request(
            "POST",
            endpoint,
            params=params,
            files={"media": (image.name, image.content, image.mime)},
        )
        key = "media_id" if cover else "url"
        value = result.get(key)
        if not isinstance(value, str) or not value:
            raise WechatError("微信未返回图片素材标识。", uncertain=True)
        if not cover and not is_wechat_image(value):
            raise WechatError("微信返回了非预期的正文图片地址。")
        return value

    def check(self):
        self.token()
        # This read-only request verifies draft-box access, not just token access.
        result = self._request("GET", "/cgi-bin/draft/count", params={"access_token": self.token()})
        if not isinstance(result.get("total_count"), int):
            raise WechatError("微信未返回有效草稿数量。")
        return result["total_count"]


def is_wechat_image(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme in ("http", "https")
        and not parsed.username
        and not parsed.password
        and any(
            host == domain or host.endswith("." + domain)
            for domain in ("qpic.cn", "qlogo.cn", "weixin.qq.com")
        )
    )


@dataclass
class UploadImage:
    name: str
    content: bytes
    mime: str

    @property
    def digest(self):
        return hashlib.sha256(self.content).hexdigest()


def prepare_image(path, *, cover=False):
    raw = Path(path).read_bytes()
    if len(raw) > 50 * 1024 * 1024:
        raise WechatError(f"图片文件过大：{Path(path).name}")
    mime = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
    if mime == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
        raise WechatError(f"仅支持 PNG/JPEG 图片：{Path(path).name}")
    limit = 10 * 1024 * 1024 if cover else 1024 * 1024 - 1
    try:
        with pymupdf.open(stream=raw, filetype="png" if mime == "image/png" else "jpeg") as doc:
            page = doc[0]
            pix = pymupdf.Pixmap(raw)
            if pix.width * pix.height > 60_000_000:
                raise WechatError("图片像素过多，请先缩小后重试。")
            if len(raw) <= limit:
                return UploadImage(Path(path).name, raw, mime)
            # Preserve the original; compress only the bytes sent to WeChat.
            for scale in (1, 0.85, 0.7, 0.55, 0.4):
                matrix = pymupdf.Matrix(
                    pix.width * scale / page.rect.width, pix.height * scale / page.rect.height
                )
                rgb = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csRGB, alpha=False)
                for quality in (92, 85, 75):
                    data = rgb.tobytes("jpeg", jpg_quality=quality)
                    if len(data) <= limit:
                        return UploadImage(Path(path).stem + ".jpg", data, "image/jpeg")
    except WechatError:
        raise
    except Exception:
        raise WechatError(f"图片无法解码：{Path(path).name}") from None
    raise WechatError(f"图片压缩后仍超过接口大小限制：{Path(path).name}")


@dataclass
class DraftArticle:
    directory: Path
    title: str
    body: str
    author: str
    digest: str
    source_url: str
    images: dict
    cover: UploadImage

    def payload(self, image_urls, cover_id):
        html = render(self.body, image_urls=image_urls)
        if len(html.encode("utf-8")) > 1024 * 1024:
            raise WechatError("正文 HTML 超过本地 1MB 校验上限。")
        return {
            "title": self.title,
            "author": self.author,
            "digest": self.digest,
            "content": html,
            "thumb_media_id": cover_id,
            "content_source_url": self.source_url,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }


def local_asset(directory, value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in value:
        raise WechatError("上传只接受文章目录内的本地图片，请先下载远程图片。")
    relative = Path(unquote(parsed.path))
    path = (directory / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(directory) or not path.is_file():
        raise WechatError("正文或封面图片不存在，或路径越出文章目录。")
    return path


def load_article(directory, cover, *, title=None, author="", digest="", source_url=""):
    directory = Path(directory).resolve()
    article = (directory / "article.md").read_text("utf-8")
    lines = article.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise WechatError("article.md 必须以一级标题开头。")
    title = title or lines[0][2:].strip()
    # Conservative local limits; never silently truncate a generated title.
    for label, value, maximum in (("标题", title, 32), ("作者", author, 16), ("摘要", digest, 120)):
        if len(value) > maximum or any(ord(c) < 32 for c in value):
            raise WechatError(f"{label}超过本地限制（{maximum}字符）或含控制字符。")
    if not title.strip():
        raise WechatError("上传标题不能为空。")
    if source_url:
        p = urlsplit(source_url)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise WechatError("阅读原文地址必须为 HTTP(S) URL。")
    body = "\n".join(lines[1:])
    if re.search(r"\[S\d+:B\d+\]", body):
        raise WechatError("公开正文仍含内部引文标记，请使用导出的 article.md。")
    images = {}
    for token in MarkdownIt("commonmark").parse(body):
        for child in token.children or []:
            if child.type == "image":
                name = child.attrGet("src") or ""
                images[name] = prepare_image(local_asset(directory, name))
    if not images:
        raise WechatError("正文没有实际图片，不能上传无图稿。")
    return DraftArticle(
        directory,
        title,
        body,
        author,
        digest,
        source_url,
        images,
        prepare_image(local_asset(directory, cover), cover=True),
    )


def write_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".wechat-", delete=False
    ) as f:
        temporary = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            os.chmod(temporary, 0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def dry_run(article):
    urls = {
        name: f"https://example.invalid/wechat-image/{i}"
        for i, name in enumerate(article.images, 1)
    }
    write_json(
        article.directory / "wechat-payload-preview.json",
        {
            "dry_run": True,
            "articles": [article.payload(urls, "DRY_RUN_COVER_ID")],
            "images": [
                {"file": name, "bytes": len(img.content), "sha256": img.digest}
                for name, img in article.images.items()
            ],
        },
    )
    # Standard preview retains the original images and rich-text copy controls.
    from base64 import b64encode

    embedded = {
        name: f"data:{img.mime};base64," + b64encode(img.content).decode("ascii")
        for name, img in article.images.items()
    }
    (article.directory / "wechat-draft-preview.html").write_text(
        preview(
            f"# {article.title}\n{article.body}",
            image_urls=embedded,
            download_url="article.md",
            notes_url="review.md",
            images_url="images.zip",
        ),
        encoding="utf-8",
    )


class ImageSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            values = dict(attrs)
            self.sources.append(values.get("src") or values.get("data-src") or "")

    def handle_data(self, data):
        self.text.append(data)


def image_identity(url):
    if not is_wechat_image(url):
        raise WechatError("回读正文包含非微信图片地址。")
    parsed = urlsplit(url)
    # The draft service rewrites HTTP to HTTPS and original-size /0 to /640.
    # Keep the asset identifier and query; never accept a different image ID.
    path = parsed.path
    if parsed.hostname.endswith(".qpic.cn"):
        path = re.sub(r"/(?:0|640)$", "/wechat-size", path)
    return parsed.hostname, parsed.port, path, tuple(sorted(parse_qsl(parsed.query)))


def sync_draft(article, client, *, update=False):
    """Caller holds the per-directory lock. Persist mutation intent before draft/add."""
    path = article.directory / "wechat-draft.json"
    state = json.loads(path.read_text("utf-8")) if path.exists() else {}
    if state and state.get("app_id") != client.account.app_id:
        raise WechatError("本目录的上传记录属于另一公众号；请使用独立文章目录。")
    media_id = state.get("media_id")
    if not media_id and state.get("status") in ("create_pending", "create_uncertain"):
        raise WechatError("上次创建结果不确定。请先在公众号草稿箱核对，勿直接重试创建。")
    if update and not media_id:
        raise WechatError("没有可更新的草稿 ID，请先 upload。")
    if media_id and not update:
        raise WechatError("本目录已经创建草稿；修改请使用 update，避免重复创建。")
    client.check()
    state.update(app_id=client.account.app_id, status="uploading")
    state.setdefault("images", {})
    write_json(path, state)
    urls = {}
    for name, img in article.images.items():
        cached = state["images"].get(img.digest)
        if not isinstance(cached, str) or not is_wechat_image(cached):
            cached = client.upload(img)
            state["images"][img.digest] = cached
            write_json(path, state)
        urls[name] = cached
    if state.get("cover_sha256") != article.cover.digest or not state.get("cover_media_id"):
        state["cover_media_id"] = client.upload(article.cover, cover=True)
        state["cover_sha256"] = article.cover.digest
        write_json(path, state)
    payload = article.payload(urls, state["cover_media_id"])
    state.update(title=article.title, status="update_pending" if update else "create_pending")
    write_json(path, state)
    try:
        if update:
            client.post(
                "/cgi-bin/draft/update", {"media_id": media_id, "index": 0, "articles": payload}
            )
        else:
            result = client.post("/cgi-bin/draft/add", {"articles": [payload]})
            media_id = result.get("media_id")
            if not isinstance(media_id, str) or not media_id:
                raise WechatError("创建草稿响应缺少 ID，请先到后台核对。", uncertain=True)
        state.update(media_id=media_id, status="saved_unverified")
        write_json(path, state)
    except WechatError as e:
        state["status"] = (
            ("update_uncertain" if update else "create_uncertain")
            if e.uncertain
            else "request_rejected"
        )
        write_json(path, state)
        raise
    return verify_draft(article, client, state=state, payload=payload)


def verify_draft(article, client, *, state=None, payload=None):
    """Read and verify the existing draft without uploading or updating anything."""
    path = article.directory / "wechat-draft.json"
    if state is None:
        state = json.loads(path.read_text("utf-8")) if path.exists() else {}
    if state.get("app_id") != client.account.app_id or not state.get("media_id"):
        raise WechatError("没有此公众号的可核对草稿记录。")
    if payload is None:
        urls = {
            name: state.get("images", {}).get(img.digest) for name, img in article.images.items()
        }
        if (
            any(not isinstance(url, str) or not is_wechat_image(url) for url in urls.values())
            or state.get("cover_sha256") != article.cover.digest
        ):
            raise WechatError("本地图片已改变，请使用 update 后再核对。")
        payload = article.payload(urls, state["cover_media_id"])
    result = client.post("/cgi-bin/draft/get", {"media_id": state["media_id"]})
    items = result.get("news_item")
    item = items[0] if isinstance(items, list) and len(items) == 1 else {}
    parser = ImageSources()
    parser.feed(item.get("content", ""))
    expected = ImageSources()
    expected.feed(payload["content"])
    if (
        item.get("title") != article.title
        or item.get("thumb_media_id") != payload["thumb_media_id"]
        or [image_identity(url) for url in parser.sources]
        != [image_identity(url) for url in expected.sources]
        or " ".join("".join(parser.text).split()) != " ".join("".join(expected.text).split())
    ):
        raise WechatError("草稿已保存，但回读的标题、正文、封面或图片不一致，请到后台核对。")
    state.update(
        status="verified",
        body_image_count=len(parser.sources),
        content_sha256=hashlib.sha256(payload["content"].encode("utf-8")).hexdigest(),
    )
    write_json(path, state)
    return state
