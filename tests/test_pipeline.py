import io
import json
import time

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import app as app_module
import pipeline.articles as articles
import pipeline.arxiv as arxiv
import pipeline.figures as figures
from generator import pdf_text, writer
from generator.evidence import NOTES_PROMPT as GENERIC_NOTES_PROMPT
from generator.sources import url as url_source
from pipeline.llm import LLM, validate_filter
from scripts.filter_papers import evaluate


def feed(date="Wed, 07 Oct 2026 00:00:00 -0400", kind="new", identifier="2610.00001", count=1):
    item = f"""<item><title>Efficient LLM Serving with KV Cache</title>
    <link>https://arxiv.org/abs/{identifier}</link>
    <description>arXiv:{identifier}v1 Announce Type: {kind} Abstract: We optimize inference serving.</description>
    <category>cs.DC</category><pubDate>{date}</pubDate>
    <arxiv:announce_type>{kind}</arxiv:announce_type><dc:creator>A. Author</dc:creator></item>"""
    return f'''<rss xmlns:arxiv="{arxiv.ARXIV_NS}" xmlns:dc="{arxiv.DC_NS}"><channel>
    <pubDate>{date}</pubDate>{item * count}</channel></rss>'''.encode()


def taxonomy():
    return "".join(
        f"<h4>{root}.C{i} <span>Category</span></h4>" for root in ["cs", "math"] for i in range(55)
    ).encode()


def pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 30 700 Td (" + b"We optimize inference serving. " * 15 + b") Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


class TestModel:
    configured = True
    model = "test-model"

    def complete(self, system, user, json_mode=False):
        if "decision" in system:
            paper = json.loads(user)
            return {
                "id": paper["id"],
                "decision": "keep",
                "relevance_score": 95,
                "topics": ["推理服务"],
                "summary_zh": "优化推理服务。",
                "reason": "涉及推理基础设施。",
                "evidence": ["We optimize inference serving."],
                "limitations": "摘要没有具体实验数字。",
            }
        if system == GENERIC_NOTES_PROMPT:
            blocks = json.loads(user)
            return {
                "facts": [
                    {
                        "claim": "优化推理服务。",
                        "ref": blocks[0]["ref"],
                        "quote": "We optimize inference serving.",
                    }
                ],
                "limitations": [],
            }
        if system == writer.GENERIC_OUTLINE_PROMPT:
            evidence = json.loads(user)
            return {
                "title": "推理服务优化",
                "sections": [
                    {
                        "heading": heading,
                        "goal": "解释具体机制",
                        "fact_refs": [evidence["facts"][0]["ref"]],
                        "figures": [f["file"] for f in evidence["figures"][:1]]
                        if index == 1
                        else [],
                        "transition": "承接前文问题",
                    }
                    for index, heading in enumerate(
                        ["服务负载", "计算机制", "实验条件", "性能边界"]
                    )
                ],
            }
        if system == writer.GENERIC_REVIEW_PROMPT:
            return {"issues": []}
        if system in (writer.GENERIC_ARTICLE_PROMPT, writer.GENERIC_REWRITE_PROMPT):
            evidence = json.loads(user)
            image = (
                "\n\n![图1：服务机制](" + evidence["figures"][0]["file"] + ")"
                if evidence.get("figures")
                else ""
            )
            return (
                "# 推理服务优化\n\n论文研究推理服务优化 [S1:B1]。"
                + image
                + "\n\n## 性能边界\n\n分析：需要结合实际负载验证。"
            )
        if system == articles.OUTLINE_PROMPT:
            evidence = json.loads(user)
            files = [f["file"] for f in evidence["figures"][:1]]
            return {
                "title": "推理服务优化",
                "sections": [
                    {
                        "heading": heading,
                        "goal": "解释具体机制",
                        "fact_pages": [1],
                        "figures": files if index == 1 else [],
                        "transition": "承接前文问题",
                    }
                    for index, heading in enumerate(
                        ["服务负载", "计算机制", "实验条件", "性能边界"]
                    )
                ],
            }
        if system == articles.REVIEW_PROMPT:
            return {"issues": []}
        if json_mode:
            return {
                "facts": [
                    {
                        "claim": "优化推理服务。",
                        "page": 1,
                        "quote": "We optimize inference serving.",
                    }
                ],
                "limitations": [],
            }
        evidence = json.loads(user)
        image = (
            "\n\n![图1：服务机制（译自原论文）](" + evidence["figures"][0]["file"] + ")"
            if evidence.get("figures")
            else ""
        )
        return (
            "# 推理服务优化\n\n论文研究推理服务优化 [PDF p.1]。"
            + image
            + "\n\n## 性能边界\n\n分析：需要结合实际负载验证。"
        )


def wait_job(client):
    for _ in range(200):
        job = client.get("/api/status").get_json()["jobs"][0]
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.01)
    pytest.fail("job did not finish")


def test_new_only_and_version_pin():
    date, papers, count = arxiv.parse_feed(feed())
    assert date == "2026-10-07" and count == 1
    assert papers[0]["version_id"] == "2610.00001v1"
    assert arxiv.parse_feed(feed(kind="replace"))[1] == []
    assert arxiv.parse_feed(feed(kind="cross"))[1] == []


def test_collection_deduplicates_and_refuses_mixed_dates(tmp_path, monkeypatch):
    responses = {
        "https://arxiv.org/category_taxonomy": taxonomy(),
        "https://rss.arxiv.org/rss/cs": feed(),
        "https://rss.arxiv.org/rss/math": feed(),
    }
    monkeypatch.setattr(arxiv, "request_bytes", lambda url: responses[url])
    monkeypatch.setattr(arxiv.time, "sleep", lambda seconds: None)
    date, papers, manifest = arxiv.collect(tmp_path / "good")
    assert len(papers) == 1 and manifest["complete"]
    assert manifest["archives"] == ["cs", "math"]
    responses["https://rss.arxiv.org/rss/math"] = feed(date="Tue, 06 Oct 2026 00:00:00 -0400")
    with pytest.raises(ValueError, match="不一致"):
        arxiv.collect(tmp_path / "mixed")
    partial = json.loads((tmp_path / "mixed/2026-10-07/manifest.json").read_text())
    assert not partial["complete"]


def test_truncated_feed_fails_instead_of_claiming_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(
        arxiv, "request_bytes", lambda url: taxonomy() if "taxonomy" in url else feed(count=2000)
    )
    monkeypatch.setattr(arxiv.time, "sleep", lambda seconds: None)
    with pytest.raises(ValueError, match="上限"):
        arxiv.collect(tmp_path)


def test_evidence_validation_rejects_invented_quotes():
    paper = arxiv.parse_feed(feed())[1][0]
    result = TestModel().complete("decision", json.dumps(paper), True)
    result["evidence"] = ["Invented speedup of 99 percent."]
    with pytest.raises(ValueError, match="证据"):
        validate_filter(result, paper)
    with pytest.raises(ValueError, match="引文"):
        articles.validate_notes(
            {"facts": [{"claim": "false", "page": 1, "quote": "invented"}], "limitations": []},
            [{"page": 1, "text": "real source"}],
        )


def test_long_pages_are_not_silently_truncated():
    pages = [{"page": 1, "text": "a" * 41000}, {"page": 2, "text": "b" * 50}]
    parts = list(articles.chunks(pages))
    assert "".join(p["text"] for part in parts for p in part) == "a" * 41000 + "b" * 50


def test_zero_and_short_words_do_not_match_unrelated_papers():
    config = json.loads((app_module.ROOT / "config/ai_infra.json").read_text())
    assert not evaluate({"title": "Zero sets of mathematical functions"}, config)["passed"]
    assert not evaluate({"title": "Moebius transformations"}, config)["passed"]
    assert evaluate({"title": "ZeRO-3 for efficient training"}, config)["passed"]
    assert not evaluate({"title": "Quantization of gravitational fields"}, config)["passed"]
    assert evaluate({"title": "Quantization for neural networks"}, config)["passed"]


def test_llm_http_payload_and_error_redaction(monkeypatch):
    for key, value in {
        "LLM_BASE_URL": "http://localhost:1234/v1",
        "LLM_API_KEY": "secret-test-key",
        "LLM_MODEL": "local-model",
    }.items():
        monkeypatch.setenv(key, value)
    from unittest.mock import Mock

    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": '{"result":true}'}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr("pipeline.llm.requests.post", post)
    assert LLM().complete("system", "input", True) == {"result": True}
    assert post.call_args.args[0] == "http://localhost:1234/v1/chat/completions"
    assert post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}
    response.status_code = 401
    with pytest.raises(ValueError, match="HTTP 401") as error:
        LLM().complete("system", "input")
    assert "secret-test-key" not in str(error.value)


def test_end_to_end_collect_filter_review_article_export_and_resume(
    tmp_path, monkeypatch, source_tar
):
    paper = arxiv.parse_feed(feed())[1][0]
    monkeypatch.setattr(
        app_module, "collect", lambda *args: ("2026-10-07", [paper], {"complete": True})
    )
    monkeypatch.setattr(articles, "request_bytes", lambda url: pdf_bytes())
    monkeypatch.setattr(url_source, "fetch", lambda url: {"content": pdf_bytes()})
    monkeypatch.setattr(figures, "request_bytes", lambda url: source_tar())
    application = app_module.create_app(tmp_path, TestModel())
    client = application.test_client()
    assert client.get("/").status_code == 200
    assert client.post("/api/jobs", json={"action": "collect"}).status_code == 202
    assert wait_job(client)["status"] == "done"
    batch = "2026-10-07"
    records = client.get("/api/papers", query_string={"batch": batch}).get_json()["papers"]
    assert records[0]["keyword_filter"]["passed"]
    assert client.post("/api/jobs", json={"action": "generate", "batch": batch}).status_code == 400
    assert client.post("/api/jobs", json={"action": "ai", "batch": batch}).status_code == 202
    assert wait_job(client)["status"] == "done"
    assert (
        client.post(
            "/api/review",
            json={
                "batch": batch,
                "id": paper["id"],
                "review": "selected",
                "angle": "关注推理服务",
                "include_summary": False,
                "team_context": {
                    "verified": True,
                    "description": "调用者核实的团队背景",
                    "sources": [{"title": "实验室主页", "url": "https://example.edu/lab"}],
                },
            },
        ).status_code
        == 200
    )
    assert client.post("/api/jobs", json={"action": "generate", "batch": batch}).status_code == 202
    assert wait_job(client)["status"] == "done"
    article = client.get(
        "/api/article", query_string={"batch": batch, "id": paper["id"]}
    ).get_json()["article"]
    assert "[PDF p." not in article["markdown"]
    assert article["notes"]["citations"][0]["refs"] == ["S1:B1"]
    assert article["notes"]["sections"][0]["location"] == "PDF p.1"
    assert article["notes"]["writing_options"]["include_summary"] is False
    assert article["notes"]["team_context"]["description"] == "调用者核实的团队背景"
    assert article["notes"]["figures"] and article["notes"]["outline"]
    assert "核对依据" not in article["markdown"] and "原文：" not in article["markdown"]
    assert not article["notes"]["review"]["rewrite_attempted"]
    query = {"batch": batch, "id": paper["id"]}
    html = client.get("/api/article/preview", query_string=query).get_data(as_text=True)
    assert "<img" in html and "/api/article/image?batch=" in html
    name = article["notes"]["figures"][0]["file"].split("/")[-1]
    image = client.get("/api/article/image", query_string={**query, "name": name})
    assert image.status_code == 200 and image.data.startswith(b"\x89PNG")
    assert (
        client.get("/api/article/image", query_string={**query, "name": "../paper.pdf"}).status_code
        == 404
    )
    assert (
        client.get("/api/article/image", query_string={**query, "name": "unknown.png"}).status_code
        == 404
    )
    import zipfile

    archive = client.get("/api/article/images.zip", query_string=query)
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
        assert set(bundle.namelist()) == {"article.md", "figures/" + name}
        assert bundle.read("article.md").decode() == article["markdown"]
    review = client.get("/api/article/review", query_string=query)
    assert review.status_code == 200 and "核对依据" in review.get_data(as_text=True)
    assert (
        client.get(
            "/api/export", query_string={"batch": batch, "kind": "markdown", "id": paper["id"]}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/export", query_string={"batch": batch}).get_json()["papers"][0]["review"]
        == "selected"
    )
    # Recollecting preserves successful AI results and manual review.
    client.post("/api/jobs", json={"action": "collect"})
    assert wait_job(client)["status"] == "done"
    saved = application.extensions["store"].get(batch, paper["id"])
    assert saved["ai"]["decision"] == "keep" and saved["review"] == "selected" and saved["article"]
    # Rules may be rerun intentionally; the manual choice remains.
    client.post("/api/jobs", json={"action": "filter", "batch": batch})
    assert wait_job(client)["status"] == "done"
    saved = application.extensions["store"].get(batch, paper["id"])
    assert saved["ai"] is None and saved["review"] == "selected"


def test_missing_model_config_is_explicit(tmp_path, monkeypatch):
    for key in ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    client = app_module.create_app(tmp_path, LLM()).test_client()
    assert client.get("/api/status").get_json()["llm_configured"] is False


def test_failed_ai_is_persisted_and_can_resume(tmp_path, monkeypatch):
    paper = arxiv.parse_feed(feed())[1][0]
    monkeypatch.setattr(
        app_module, "collect", lambda *args: ("2026-10-07", [paper], {"complete": True})
    )
    model = TestModel()
    application = app_module.create_app(tmp_path, model)
    client = application.test_client()
    client.post("/api/jobs", json={"action": "collect"})
    assert wait_job(client)["status"] == "done"
    original = model.complete
    monkeypatch.setattr(
        model, "complete", lambda *args: (_ for _ in ()).throw(ValueError("测试：模型失败"))
    )
    client.post("/api/jobs", json={"action": "ai", "batch": "2026-10-07"})
    assert wait_job(client)["status"] == "error"
    saved = application.extensions["store"].get("2026-10-07", paper["id"])
    assert saved["ai"] is None and saved["ai_error"] == "测试：模型失败"
    monkeypatch.setattr(model, "complete", original)
    client.post("/api/jobs", json={"action": "ai", "batch": "2026-10-07"})
    assert wait_job(client)["status"] == "done"
    saved = application.extensions["store"].get("2026-10-07", paper["id"])
    assert saved["ai"]["decision"] == "keep" and saved["ai_error"] is None


def test_pdf_extraction_warnings_are_preserved(monkeypatch):
    import logging
    from unittest.mock import Mock

    def extract():
        logging.getLogger("pypdf._page").warning("Some form content was skipped.")
        return "We optimize inference serving. " * 15

    page = Mock()
    page.extract_text.side_effect = extract
    reader = Mock(pages=[page])
    monkeypatch.setattr(pdf_text, "PdfReader", lambda *args: reader)
    pages = articles.extract_pages(b"test")
    assert pages[0]["extraction_warnings"] == ["Some form content was skipped."]


def test_cross_origin_mutation_is_rejected(tmp_path):
    client = app_module.create_app(tmp_path, TestModel()).test_client()
    assert (
        client.post(
            "/api/jobs", json={"action": "collect"}, headers={"Origin": "https://other.example"}
        ).status_code
        == 403
    )
