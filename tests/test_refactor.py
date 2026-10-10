"""Independent CLI and multi-format integration tests, without real model calls."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest

from arxiv_tool import cli as arxiv_cli
from generator import cli, writer
from generator.evidence import NOTES_PROMPT, merge_materials, validate_notes
from generator.pipeline import generate
from generator.sources import detect, register
from generator.sources import url as url_source
from generator.sources.base import ExtractedText, Material, Source
from generator.sources.html import HtmlSource
from generator.sources.markdown import MarkdownSource


class GenericModel:
    configured = True
    model = "controlled-test-model"

    def __init__(self, bad_rewrite=False):
        self.calls = []
        self.bad_rewrite = bad_rewrite

    def complete(self, system, user, json_mode=False):
        data = json.loads(user)
        self.calls.append((system, data))
        if system == NOTES_PROMPT:
            return {
                "facts": [
                    {
                        "claim": "材料明确支持的事实。",
                        "ref": block["ref"],
                        "quote": block["text"][:80],
                    }
                    for block in data
                    if block["text"].strip()
                ],
                "limitations": [],
            }
        if system == writer.GENERIC_OUTLINE_PROMPT:
            assert "sections" not in data  # full raw material does not inflate later prompts
            return {
                "title": "资料中的技术机制",
                "sections": [
                    {
                        "heading": heading,
                        "goal": "解释具体问题",
                        "fact_refs": [fact["ref"] for fact in data["facts"]],
                        "figures": [],
                        "transition": "承接材料证据",
                    }
                    for heading in ["背景", "机制", "实现", "边界"]
                ],
            }
        if system == writer.GENERIC_REVIEW_PROMPT:
            assert data["section_evidence"][0]["fact_quotes"] == data["facts"]
            return {
                "issues": [
                    {"location": "机制", "problem": "需要说明条件", "suggestion": "补充已有条件"}
                ]
                if self.bad_rewrite
                else []
            }
        if system == writer.GENERIC_REWRITE_PROMPT and self.bad_rewrite:
            return "# 无效改写\n\n伪造事实 [S99:B9]。"
        assert system in (writer.GENERIC_ARTICLE_PROMPT, writer.GENERIC_REWRITE_PROMPT)
        text = "# 资料中的技术机制\n\n" + "\n\n".join(
            "材料包含具体技术描述。 [" + fact["ref"] + "]" for fact in data["facts"]
        )
        for index, figure in enumerate(data["figures"], 1):
            text += f"\n\n![图{index}：机制]({figure['file']})\n\n*图{index}：来源中的机制图。*"
        return text + "\n"


def image(path):
    with pymupdf.open() as doc:
        doc.new_page(width=200, height=160)
        doc[0].get_pixmap().save(path)
    return path


def pdf(path):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(
            pymupdf.Rect(20, 20, 580, 700),
            "Detailed inference serving mechanism and measured conditions. " * 15,
        )
        doc.save(path)
    return path


def test_markdown_html_pdf_extraction_and_assets(tmp_path):
    image(tmp_path / "method.png")
    md = tmp_path / "notes.md"
    md.write_text(
        "# KV Cache\n\nA factual mechanism.\n\n![Cache layout](method.png)\n\n```python\ncache = {}\n```\n\n| Metric | Value |\n| --- | --- |\n| Memory | 12 |\n",
        encoding="utf-8",
    )
    material = detect(md).extract(md, tmp_path / "md-work")
    assert material.title == "KV Cache"
    assert material.figures[0].caption_en == "Cache layout"
    assert any("cache = {}" in section.text for section in material.sections)
    assert any("| Memory | 12 |" in section.text for section in material.sections)
    assert all(section.ref.startswith("lines ") for section in material.sections)
    html = tmp_path / "post.html"
    html.write_text(
        '<html><head><title>Original title</title><meta name="author" content="A"></head><body><nav>Noise</nav><main><h1>Mechanism</h1><p>Useful fact.</p><p hidden>Hidden text</p><figure><img src="method.png"><figcaption>Diagram caption</figcaption></figure><pre>cache = {}\nsize = 12</pre><table><tr><td>Memory</td><td>12</td></tr></table></main></body></html>',
        encoding="utf-8",
    )
    material = detect(html).extract(html, tmp_path / "html-work")
    assert material.title == "Mechanism" and material.authors == ["A"]
    assert "Noise" not in "\n".join(s.text for s in material.sections)
    assert "Hidden text" not in "\n".join(s.text for s in material.sections)
    assert material.figures[0].caption_en == "Diagram caption"
    assert any("Memory\t12" in section.text for section in material.sections)
    material = detect(pdf(tmp_path / "paper.pdf")).extract(
        tmp_path / "paper.pdf", tmp_path / "pdf-work"
    )
    assert material.sections[0].ref == "PDF p.1"
    assert material.metadata["source_type"] == "pdf"


def test_two_pdf_page_ones_do_not_share_evidence(tmp_path):
    first = Material("A", [], [ExtractedText("PDF p.1", "First source only.")], [], {})
    second = Material("B", [], [ExtractedText("PDF p.1", "Second source only.")], [], {})
    merged = merge_materials([first, second], tmp_path)
    assert [block["ref"] for block in merged["sections"]] == ["S1:B1", "S2:B1"]
    with pytest.raises(ValueError, match="对应"):
        validate_notes(
            {
                "facts": [{"claim": "x", "ref": "S1:B1", "quote": "Second source only."}],
                "limitations": [],
            },
            merged["sections"],
        )


def test_generic_pipeline_output_and_invalid_rewrite_retains_draft(tmp_path):
    image(tmp_path / "method.png")
    md = tmp_path / "paper.md"
    md.write_text(
        "# Mechanism\n\nConcrete factual description.\n\n![Caption](method.png)", encoding="utf-8"
    )
    material = MarkdownSource().extract(md, tmp_path / "source")
    output = tmp_path / "article"
    model = GenericModel(bad_rewrite=True)
    result = generate(
        [material],
        model,
        output,
        angle="资源取舍",
        audience="系统工程师",
        depth="deep-dive",
        include_summary=False,
    )
    assert "[S1:B" not in result.markdown
    assert "[S1:B" in (output / "article-cited.md").read_text()
    assert result.notes["review"]["rewrite_attempted"]
    assert not result.notes["review"]["rewrite_applied"]
    assert result.notes["writing_options"] == {
        "audience": "系统工程师",
        "depth": "deep-dive",
        "include_summary": False,
    }
    assert "lines " in (output / "review.md").read_text()
    assert "data-figure-caption" in (output / "wechat.html").read_text()
    with zipfile.ZipFile(output / "images.zip") as archive:
        assert "article.md" in archive.namelist()
        assert result.notes["figures"][0]["file"] in archive.namelist()
    assert not list(tmp_path.rglob("*.sqlite3"))


@pytest.mark.parametrize(
    "suffix,mime,body,expected",
    [
        ("/content", "text/html", b"<main><h1>Title</h1><p>A useful fact.</p></main>", "html"),
        ("/notes.md", "text/plain", b"# Title\n\nA useful fact.", "markdown"),
    ],
)
def test_url_dispatch_keeps_final_url(tmp_path, monkeypatch, suffix, mime, body, expected):
    monkeypatch.setattr(
        url_source,
        "fetch",
        lambda value: {
            "content": body,
            "url": "https://example.org" + suffix,
            "content_type": mime,
            "encoding": "utf-8",
        },
    )
    material = detect("https://example.org/start").extract("https://example.org/start", tmp_path)
    assert material.metadata["source_type"] == expected
    assert material.metadata["url"] == "https://example.org" + suffix
    assert material.metadata["requested_url"] == "https://example.org/start"


def test_url_pdf_sniffing_and_unknown_content(tmp_path, monkeypatch):
    raw = pdf(tmp_path / "paper.pdf").read_bytes()
    monkeypatch.setattr(
        url_source,
        "fetch",
        lambda value: {
            "content": raw,
            "url": value,
            "content_type": "application/octet-stream",
            "encoding": None,
        },
    )
    assert (
        url_source.UrlSource()
        .extract("https://example.org/download", tmp_path / "work")
        .metadata["source_type"]
        == "pdf"
    )
    monkeypatch.setattr(
        url_source,
        "fetch",
        lambda value: {
            "content": b'{"error": "blocked"}',
            "url": value,
            "content_type": "application/json",
            "encoding": "utf-8",
        },
    )
    with pytest.raises(ValueError, match="不把未知"):
        url_source.UrlSource().extract("https://example.org/download", tmp_path / "bad")


def test_arxiv_url_resolves_full_pinned_version(tmp_path, monkeypatch):
    raw = pdf(tmp_path / "paper.pdf").read_bytes()
    urls = []

    def fetch(value, **kwargs):
        urls.append(value)
        return {
            "content": b'<meta name="citation_title" content="Paper"><meta name="citation_author" content="A"><a href="/abs/2501.12345v2">Version 2</a>'
            if "/abs/" in value
            else raw
        }

    monkeypatch.setattr(url_source, "fetch", fetch)
    monkeypatch.setattr(url_source, "collect_figures", lambda paper, directory: ([], []))
    material = url_source.UrlSource().extract("https://arxiv.org/abs/2501.12345", tmp_path / "work")
    assert urls[-1] == "https://arxiv.org/pdf/2501.12345v2"
    assert material.title == "Paper" and material.authors == ["A"]
    assert len(material.sections[0].text) > 300
    assert material.metadata["version_id"] == "2501.12345v2"


def test_arxiv_cli_no_llm_database_and_complete_exports(tmp_path, monkeypatch):
    papers = [
        {
            "id": "test",
            "title": "LLM KV Cache Serving",
            "abstract": "Inference serving.",
            "url": "https://arxiv.org/abs/2501.12345v1",
        },
        {
            "id": "other",
            "title": "Geometry",
            "abstract": "",
            "url": "https://arxiv.org/abs/2501.12346v1",
        },
    ]
    monkeypatch.setattr(
        arxiv_cli.collector, "collect", lambda *a, **k: ("2025-01-15", papers, {"complete": True})
    )
    monkeypatch.setattr(arxiv_cli, "LLM", lambda: pytest.fail("skip-ai must not instantiate model"))
    assert arxiv_cli.main(["--skip-ai", "--output-dir", str(tmp_path)]) == 0
    output = tmp_path / "2025-01-15"
    assert len(json.loads((output / "papers.json").read_text())) == 2
    assert len(json.loads((output / "filtered.json").read_text())) == 2
    assert len(json.loads((output / "selected.json").read_text())) == 1
    assert "未翻译" in (output / "selected.md").read_text()
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_generator_cli_json_merge_batch_and_skip_records(tmp_path, monkeypatch):
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.write_text("# First\n\nFirst source fact.", encoding="utf-8")
    second.write_text("# Second\n\nSecond source fact.", encoding="utf-8")
    manifest = tmp_path / "selected.json"
    manifest.write_text(
        json.dumps(
            [
                {"paper": {"path": "first.md"}, "review": "selected"},
                {"paper": {"path": "second.md"}, "review": "pending"},
                {"paper": {"url": "https://example.org/rejected"}, "ai": {"decision": "reject"}},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LLM", GenericModel)
    assert cli.main([str(manifest), "--output-dir", str(tmp_path / "merged")]) == 0
    article = next((tmp_path / "merged").rglob("article.md"))
    evidence = json.loads(article.with_name("notes.json").read_text())
    assert [source["id"] for source in evidence["sources"]] == ["S1", "S2"]
    assert cli.main([str(manifest), "--batch", "--output-dir", str(tmp_path / "batch")]) == 0
    assert len(list((tmp_path / "batch").rglob("article.md"))) == 2


def test_generator_batch_continues_after_source_failure(tmp_path, monkeypatch):
    good = tmp_path / "good.md"
    good.write_text("# Good\n\nUseful source.", encoding="utf-8")
    monkeypatch.setattr(cli, "LLM", GenericModel)
    assert (
        cli.main(
            [
                str(tmp_path / "missing.md"),
                str(good),
                "--batch",
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 1
    )
    assert len(list((tmp_path / "output").rglob("article.md"))) == 1
    assert len(json.loads(next((tmp_path / "output").rglob("failures.json")).read_text())) == 1


def test_source_registry_extension_and_package_independence(tmp_path):
    class CustomSource(Source):
        pass

    register(lambda value: value == "custom:test", CustomSource)
    assert isinstance(detect("custom:test"), CustomSource)
    root = Path(__file__).resolve().parents[1]
    for module, forbidden in [
        ("generator.cli", ("arxiv_tool", "web", "pipeline")),
        ("arxiv_tool.cli", ("generator", "web", "pipeline")),
    ]:
        code = f'import {module}; import sys; assert not any(name.split(".")[0] in {forbidden!r} for name in sys.modules)'
        subprocess.run(
            [sys.executable, "-B", "-c", code], cwd=root, check=True, capture_output=True
        )


def test_html_readability_and_declared_encoding(tmp_path):
    path = tmp_path / "legacy.html"
    content = (
        '<html><head><meta charset="gbk"><title>技术文档</title></head><body><nav>菜单</nav><div class="content"><h1>缓存机制</h1><p>'
        + "这是带有实现条件的技术正文，包含缓存机制和性能边界。" * 30
        + "</p></div></body></html>"
    )
    path.write_bytes(content.encode("gbk"))
    material = HtmlSource().extract(path, tmp_path / "work")
    assert "缓存机制" in material.title
    assert any("性能边界" in section.text for section in material.sections)
    assert any("readability" in item for item in material.limitations)
    assert not any("菜单" in section.text for section in material.sections)


def test_collection_offline_cache_and_invalid_date(tmp_path, monkeypatch):
    from arxiv_tool import collector
    from tests.test_pipeline import feed, taxonomy

    directory = tmp_path / "2026-10-07"
    directory.mkdir()
    (directory / "taxonomy.html").write_bytes(taxonomy())
    for name in ("cs", "math"):
        (directory / f"{name}.xml").write_bytes(feed())
    monkeypatch.setattr(
        collector,
        "request_bytes",
        lambda url: pytest.fail("Complete historical cache must not use network"),
    )
    assert collector.collect(tmp_path, requested_date="2026-10-07")[0] == "2026-10-07"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        collector.collect(tmp_path, requested_date="../bad")


def test_ai_filter_translation_rejection_and_error_export(tmp_path, monkeypatch):
    from arxiv_tool import ai_filter

    papers = [
        {
            "id": str(i),
            "title": "LLM KV Cache Serving",
            "abstract": "Inference serving.",
            "url": f"https://example.org/{i}",
        }
        for i in range(3)
    ]

    class Model:
        configured = True
        model = "controlled-filter-model"

        def complete(self, system, user, json_mode=False):
            paper = json.loads(user)
            if paper["id"] == "2":
                raise ValueError("受控失败")
            return {
                "id": paper["id"],
                "decision": "keep" if paper["id"] == "0" else "reject",
                "title_zh": "大模型 KV 缓存服务",
                "relevance_score": 80,
                "topics": ["推理"],
                "summary_zh": "服务优化",
                "reason": "基础设施",
                "evidence": ["Inference serving."],
                "limitations": "摘要证据",
            }

    monkeypatch.setattr(
        arxiv_cli.collector, "collect", lambda *a, **k: ("2025-01-15", papers, {"complete": True})
    )
    monkeypatch.setattr(arxiv_cli, "LLM", Model)
    assert arxiv_cli.main(["--output-dir", str(tmp_path)]) == 1
    output = tmp_path / "2025-01-15"
    assert len(json.loads((output / "ai-reviewed.json").read_text())) == 3
    assert len(json.loads((output / "selected.json").read_text())) == 1
    assert "大模型 KV 缓存服务" in (output / "selected.md").read_text()
    with pytest.raises(ValueError, match="证据"):
        ai_filter.filter_one(
            papers[0],
            type(
                "BadModel",
                (),
                {
                    "model": "bad",
                    "complete": lambda *a: {
                        "id": "0",
                        "decision": "keep",
                        "relevance_score": 1,
                        "topics": [],
                        "summary_zh": "",
                        "reason": "",
                        "evidence": ["invented"],
                        "limitations": "",
                    },
                },
            )(),
            "",
        )


def test_interactive_exports_only_selected_with_all_human_states(tmp_path, monkeypatch):
    papers = [
        {"id": str(i), "title": "LLM KV Cache", "abstract": "", "url": f"https://example.org/{i}"}
        for i in range(3)
    ]
    monkeypatch.setattr(
        arxiv_cli.collector, "collect", lambda *a, **k: ("2025-01-15", papers, {"complete": True})
    )
    answers = iter(["bad", "y", "n", "l"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert arxiv_cli.main(["--skip-ai", "--interactive", "--output-dir", str(tmp_path)]) == 0
    output = tmp_path / "2025-01-15"
    assert [item["review"] for item in json.loads((output / "human-review.json").read_text())] == [
        "selected",
        "skipped",
        "later",
    ]
    assert [item["paper"]["id"] for item in json.loads((output / "selected.json").read_text())] == [
        "0"
    ]


def test_cli_full_process_with_local_http_sources_and_model(tmp_path):
    """Exercise the real entry point and LLM HTTP client against a controlled local server."""
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    png = image(tmp_path / "asset.png").read_bytes()
    model = GenericModel()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, mime, body):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/asset.png":
                self.send("image/png", png)
            elif self.path == "/notes.md":
                self.send(
                    "text/markdown", b"# Notes\n\nA measured fact.\n\n![A diagram](asset.png)"
                )
            else:
                self.send(
                    "text/html",
                    '<main><h1>技术机制</h1><p>网页包含明确的实现条件。</p><figure><img src="/asset.png"><figcaption>缓存结构</figcaption></figure></main>'.encode(),
                )

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            result = model.complete(
                payload["messages"][0]["content"],
                payload["messages"][1]["content"],
                "response_format" in payload,
            )
            content = json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else result
            self.send(
                "application/json",
                json.dumps(
                    {"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
                    ensure_ascii=False,
                ).encode(),
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    env = {
        **os.environ,
        "LLM_BASE_URL": base + "/v1",
        "LLM_API_KEY": "test-only",
        "LLM_MODEL": model.model,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "generator",
                base + "/post",
                base + "/notes.md",
                "--output-dir",
                str(tmp_path / "output"),
            ],
            env=env,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    article = next((tmp_path / "output").rglob("article.md"))
    evidence = json.loads(article.with_name("notes.json").read_text())
    assert evidence["sources"][0]["title"] == "技术机制"
    assert len(evidence["sources"]) == 2 and len(evidence["figures"]) == 2
    assert all((article.parent / figure["file"]).is_file() for figure in evidence["figures"])
    assert "[S1:" not in article.read_text() and "[S2:" not in article.read_text()


def test_url_html_declared_charset_is_not_decoded_twice(tmp_path, monkeypatch):
    raw = '<meta charset="gbk"><main><h1>缓存机制</h1><p>这是一段有证据的技术资料。</p></main>'.encode(
        "gbk"
    )
    monkeypatch.setattr(
        url_source,
        "fetch",
        lambda value: {
            "content": raw,
            "url": value,
            "content_type": "text/html; charset=gbk",
            "encoding": "gbk",
        },
    )
    material = url_source.UrlSource().extract("https://example.org/post", tmp_path)
    assert material.title == "缓存机制"
    assert "技术资料" in material.sections[-1].text
    assert (tmp_path / "source.html").read_bytes() == raw


def test_arxiv_rerun_preserves_manual_choices(tmp_path, monkeypatch):
    papers = [
        {"id": "one", "title": "LLM KV Cache", "abstract": "", "url": "https://example.org/one"}
    ]
    monkeypatch.setattr(
        arxiv_cli.collector, "collect", lambda *a, **k: ("2025-01-15", papers, {"complete": True})
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert arxiv_cli.main(["--skip-ai", "--interactive", "--output-dir", str(tmp_path)]) == 0
    assert arxiv_cli.main(["--skip-ai", "--output-dir", str(tmp_path)]) == 0
    output = tmp_path / "2025-01-15"
    assert json.loads((output / "selected.json").read_text()) == []
    assert json.loads((output / "human-review.json").read_text())[0]["review"] == "skipped"


def test_batch_damaged_pdf_does_not_abort_other_articles(tmp_path, monkeypatch):
    broken, good = tmp_path / "broken.pdf", tmp_path / "good.md"
    broken.write_bytes(b"%PDF-1.7\ntruncated")
    good.write_text("# Good\n\nUseful fact.", encoding="utf-8")
    monkeypatch.setattr(cli, "LLM", GenericModel)
    assert (
        cli.main([str(broken), str(good), "--batch", "--output-dir", str(tmp_path / "output")]) == 1
    )
    assert len(list((tmp_path / "output").rglob("article.md"))) == 1


def test_generic_citations_and_images_cannot_escape_manifest():
    facts = [{"ref": "S1:B1", "claim": "x", "quote": "x"}]
    with pytest.raises(ValueError, match="引用"):
        writer.validate_generic_article("# Title\n\nUnknown [S2:B1].", facts, [])
    with pytest.raises(ValueError, match="图片"):
        writer.validate_generic_article(
            "# Title\n\nKnown [S1:B1].\n\n![x](../private.png)", facts, []
        )
    with pytest.raises(ValueError, match="图片"):
        writer.validate_generic_article('# Title\n\nKnown [S1:B1].\n\n<img src="x">', facts, [])
