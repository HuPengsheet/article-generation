import json
from pathlib import Path

import pymupdf
import pytest
import requests

from scripts.wechat_draft import main
from shared.wechat_draft import (
    Account,
    WechatClient,
    WechatError,
    load_article,
    prepare_image,
    sync_draft,
    verify_draft,
)


def png(path):
    pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 30, 20), False)
    pix.clear_with(255)
    path.write_bytes(pix.tobytes("png"))


@pytest.fixture
def article_dir(tmp_path):
    (tmp_path / "figures").mkdir()
    png(tmp_path / "figures" / "one.png")
    png(tmp_path / "cover.png")
    (tmp_path / "article.md").write_text(
        "# 中文标题\n\n正文 **加粗**。\n\n![原图](figures/one.png)\n", encoding="utf-8"
    )
    return tmp_path


class FakeClient:
    def __init__(self):
        self.account = Account("wx-test", "test-secret")
        self.calls = []
        self.saved = None
        self.fail_create = False
        self.corrupt_readback = False

    def check(self):
        self.calls.append("check")
        return 0

    def upload(self, image, *, cover=False):
        self.calls.append("cover" if cover else "image")
        return "cover-id" if cover else "https://mmbiz.qpic.cn/image/one.png"

    def post(self, endpoint, payload):
        self.calls.append(endpoint)
        assert "freepublish" not in endpoint and "mass" not in endpoint
        if endpoint == "/cgi-bin/draft/add":
            if self.fail_create:
                raise WechatError("timeout", uncertain=True)
            self.saved = payload["articles"][0]
            return {"media_id": "draft-id"}
        if endpoint == "/cgi-bin/draft/update":
            assert payload["media_id"] == "draft-id" and payload["index"] == 0
            self.saved = payload["articles"]
            return {"errcode": 0}
        if endpoint == "/cgi-bin/draft/get":
            item = dict(self.saved)
            if self.corrupt_readback:
                item["content"] = "<p>图片被过滤</p>"
            return {"news_item": [item]}
        raise AssertionError(endpoint)


def test_uploads_images_and_cover_and_verifies_draft(article_dir):
    article = load_article(article_dir, "cover.png", author="署名")
    client = FakeClient()
    result = sync_draft(article, client)
    assert result["status"] == "verified"
    assert result["body_image_count"] == 1
    assert client.saved["title"] == "中文标题"
    assert "中文标题" not in client.saved["content"]
    assert "https://mmbiz.qpic.cn/" in client.saved["content"]
    assert "figures/one.png" not in client.saved["content"]
    assert "data:image" not in client.saved["content"]
    record = (article_dir / "wechat-draft.json").read_text()
    assert "test-secret" not in record and "access_token" not in record
    assert (article_dir / "wechat-draft.json").stat().st_mode & 0o777 == 0o600


def test_repeated_upload_blocks_duplicates_and_explicit_update_reuses_assets(article_dir):
    article = load_article(article_dir, "cover.png")
    client = FakeClient()
    sync_draft(article, client)
    previous = list(client.calls)
    with pytest.raises(WechatError, match="已经创建"):
        sync_draft(article, client)
    assert client.calls == previous
    updated = load_article(article_dir, "cover.png", title="修改后的标题")
    result = sync_draft(updated, client, update=True)
    assert result["title"] == "修改后的标题"
    assert client.calls.count("image") == client.calls.count("cover") == 1
    assert client.calls.count("/cgi-bin/draft/add") == 1
    assert client.calls.count("/cgi-bin/draft/update") == 1


def test_uncertain_create_persists_intent_and_blocks_retry(article_dir):
    client = FakeClient()
    client.fail_create = True
    article = load_article(article_dir, "cover.png")
    with pytest.raises(WechatError, match="timeout"):
        sync_draft(article, client)
    state = json.loads((article_dir / "wechat-draft.json").read_text())
    assert state["status"] == "create_uncertain"
    with pytest.raises(WechatError, match="结果不确定"):
        sync_draft(article, client)
    assert client.calls.count("/cgi-bin/draft/add") == 1


def test_failed_readback_keeps_created_id_instead_of_creating_again(article_dir):
    client = FakeClient()
    client.corrupt_readback = True
    article = load_article(article_dir, "cover.png")
    with pytest.raises(WechatError, match="回读"):
        sync_draft(article, client)
    state = json.loads((article_dir / "wechat-draft.json").read_text())
    assert state["media_id"] == "draft-id" and state["status"] == "saved_unverified"
    with pytest.raises(WechatError, match="已经创建"):
        sync_draft(article, client)


def test_account_mismatch_and_update_without_id_do_not_call_api(article_dir):
    article = load_article(article_dir, "cover.png")
    client = FakeClient()
    with pytest.raises(WechatError, match="没有可更新"):
        sync_draft(article, client, update=True)
    (article_dir / "wechat-draft.json").write_text('{"app_id":"another-account"}')
    with pytest.raises(WechatError, match="另一公众号"):
        sync_draft(article, client)
    assert not client.calls


@pytest.mark.parametrize(
    "image",
    [
        "../escape.png",
        "/tmp/image.png",
        "https://example.com/image.png",
        "data:image/png;base64,abc",
    ],
)
def test_preflight_rejects_nonlocal_images(article_dir, image):
    (article_dir / "article.md").write_text(f"# 标题\n\n![图]({image})\n")
    with pytest.raises(WechatError, match="本地图片|越出"):
        load_article(article_dir, "cover.png")


def test_symlink_outside_article_directory_is_rejected(article_dir):
    (article_dir / "figures" / "link.png").symlink_to(Path(__file__).resolve())
    (article_dir / "article.md").write_text("# 标题\n\n![图](figures/link.png)\n")
    with pytest.raises(WechatError, match="越出"):
        load_article(article_dir, "cover.png")


def test_long_title_requires_explicit_short_title_and_no_image_is_rejected(article_dir):
    (article_dir / "article.md").write_text("# " + "长" * 33 + "\n\n![图](figures/one.png)\n")
    with pytest.raises(WechatError, match="标题"):
        load_article(article_dir, "cover.png")
    assert load_article(article_dir, "cover.png", title="短标题").title == "短标题"
    (article_dir / "article.md").write_text("# 标题\n\n纯文字\n")
    with pytest.raises(WechatError, match="没有实际图片"):
        load_article(article_dir, "cover.png")


def test_dry_run_does_not_need_credentials_or_network(article_dir, monkeypatch):
    monkeypatch.delenv("WECHAT_APP_ID", raising=False)
    monkeypatch.delenv("WECHAT_APP_SECRET", raising=False)
    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: pytest.fail("network"))
    assert (
        main(
            [
                "--env-file",
                str(article_dir / "absent.env"),
                "dry-run",
                str(article_dir),
                "--cover",
                "cover.png",
            ]
        )
        == 0
    )
    payload = json.loads((article_dir / "wechat-payload-preview.json").read_text())
    assert payload["dry_run"] is True
    assert "example.invalid" in payload["articles"][0]["content"]
    html = (article_dir / "wechat-draft-preview.html").read_text()
    assert "data:image/png;base64," in html and 'id="copy"' in html
    assert not (article_dir / "wechat-draft.json").exists()


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(item).encode("utf-8")
        return result


def test_token_cache_count_uses_get_and_request_unicode_encoding():
    session = FakeSession(
        [{"access_token": "test-token", "expires_in": 7200}, {"total_count": 7}, {"errcode": 0}]
    )
    client = WechatClient(Account("wx-test", "test-secret"), session)
    assert client.check() == 7
    client.post("/cgi-bin/draft/update", {"title": "中文标题"})
    assert len(session.calls) == 3
    assert session.calls[1][0] == "GET"
    assert session.calls[1][1].endswith("/cgi-bin/draft/count")
    assert session.calls[2][2]["data"].decode("utf-8") == '{"title": "中文标题"}'
    assert all(c[2]["allow_redirects"] is False for c in session.calls)


@pytest.mark.parametrize(
    "response",
    [
        requests.ConnectionError("https://host/?secret=test-secret&access_token=test-token"),
        {"errcode": 40001, "errmsg": "secret=test-secret&access_token=test-token"},
    ],
)
def test_transport_errors_never_expose_secret_or_token(response):
    client = WechatClient(Account("wx-test", "test-secret"), FakeSession([response]))
    with pytest.raises(WechatError) as info:
        client.token()
    assert "test-secret" not in str(info.value) and "test-token" not in str(info.value)


def test_whitelist_error_is_actionable_without_echoing_credentials():
    client = WechatClient(
        Account("wx-test", "test-secret"),
        FakeSession([{"errcode": 40164, "errmsg": "invalid ip 192.0.2.1, not in whitelist"}]),
    )
    with pytest.raises(WechatError, match=r"192.0.2.1.*白名单"):
        client.check()


def test_large_png_is_compressed_without_changing_source(tmp_path):
    import os

    pix = pymupdf.Pixmap(pymupdf.csRGB, 800, 600, os.urandom(800 * 600 * 3), False)
    path = tmp_path / "large.png"
    original = pix.tobytes("png")
    assert len(original) > 1024 * 1024
    path.write_bytes(original)
    result = prepare_image(path)
    assert len(result.content) < 1024 * 1024 and result.mime == "image/jpeg"
    assert path.read_bytes() == original
    assert pymupdf.Pixmap(result.content).width > 0


def test_utf8_json_ignores_incorrect_latin1_response_encoding():
    class Latin1Session:
        def request(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.encoding = "ISO-8859-1"
            response._content = '{"title":"中文标题"}'.encode("utf-8")
            return response

    client = WechatClient(Account("wx-test", "test-secret"), Latin1Session())
    assert client._request("GET", "/test")["title"] == "中文标题"


def test_verify_accepts_wechat_size_and_https_rewrite_without_new_upload(article_dir):
    client = FakeClient()
    article = load_article(article_dir, "cover.png")
    sync_draft(article, client)
    # Model the two transformations observed in a real WeChat draft response.
    state_path = article_dir / "wechat-draft.json"
    state = json.loads(state_path.read_text())
    state["images"] = {
        digest: "http://mmbiz.qpic.cn/mmbiz_png/ASSET/0?from=appmsg" for digest in state["images"]
    }
    state_path.write_text(json.dumps(state))
    client.saved["content"] = client.saved["content"].replace(
        "https://mmbiz.qpic.cn/image/one.png",
        "https://mmbiz.qpic.cn/mmbiz_png/ASSET/640?from=appmsg",
    )
    client.calls.clear()
    assert verify_draft(article, client)["status"] == "verified"
    assert client.calls == ["/cgi-bin/draft/get"]


@pytest.mark.parametrize("change", ["image", "text"])
def test_verify_still_rejects_different_image_or_lost_body_text(article_dir, change):
    client = FakeClient()
    article = load_article(article_dir, "cover.png")
    sync_draft(article, client)
    before = client.saved["content"]
    client.saved["content"] = (
        before.replace("/image/one.png", "/image/another.png")
        if change == "image"
        else before.replace("正文", "")
    )
    with pytest.raises(WechatError, match="回读"):
        verify_draft(article, client)
