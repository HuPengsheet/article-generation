import json

import pytest

import pipeline.articles as articles
from pipeline.writing import (
    article_images,
    lint_article,
    validate_article,
    validate_outline,
    validate_review,
)

FACTS = [{"claim": "事实", "page": 1, "quote": "source"}]
FIGURES = [{"file": "figures/figure-01-01.png", "caption_en": "Method", "order": 1}]


def outline():
    return {
        "title": "具体技术标题",
        "sections": [
            {
                "heading": f"技术机制{i}",
                "goal": "解释问题",
                "fact_pages": [1],
                "figures": ["figures/figure-01-01.png"],
                "transition": "承接前文",
            }
            for i in range(4)
        ],
    }


def test_outline_rejects_unknown_pages_images_and_invalid_sections():
    assert validate_outline(outline(), FACTS, FIGURES)
    for key, value in [("fact_pages", [2]), ("fact_pages", [True]), ("figures", ["missing.png"])]:
        data = outline()
        data["sections"][0][key] = value
        with pytest.raises(ValueError):
            validate_outline(data, FACTS, FIGURES)
    data = outline()
    data["sections"] = data["sections"][:3]
    with pytest.raises(ValueError):
        validate_outline(data, FACTS, FIGURES)


@pytest.mark.parametrize(
    "image",
    [
        "![图](figures/missing.png)",
        "![图](../secret.png)",
        "![图][ref]\n\n[ref]: https://other.example/image.png",
        '<img src="figures/missing.png">',
    ],
)
def test_article_rejects_unlisted_images_including_reference_syntax(image):
    with pytest.raises(ValueError, match="图片"):
        validate_article("# 标题\n\n事实 [PDF p.1]。\n\n" + image, FACTS, FIGURES)


def test_real_markdown_images_validated_but_examples_in_code_are_not_images():
    article = (
        "# 标题\n\n事实 [PDF p.1]。\n\n![图](figures/figure-01-01.png)\n\n`![例子](example.png)`"
    )
    assert article_images(article) == ["figures/figure-01-01.png"]
    assert validate_article(article, FACTS, FIGURES) == article


def test_lint_identifies_cliches_bullet_dominance_and_mechanical_paragraphs():
    issues = lint_article("# 标题\n\n值得注意的是。\n\n## 对 AI Infra 的启发\n\n- A\n- B\n- C\n- D")
    assert any("套话" in i["problem"] for i in issues)
    assert any("启发" in i["problem"] for i in issues)
    assert any("列表" in i["problem"] for i in issues)
    issues = lint_article("# 标题\n\n" + "\n\n".join("这是重复的技术解释。" * 6 for _ in range(6)))
    assert any("段落长度" in i["problem"] for i in issues)


def test_review_requires_specific_locations_and_problems():
    assert validate_review({"issues": []}) == []
    with pytest.raises(ValueError):
        validate_review({"issues": ["不够好"]})


class StagedModel:
    model = "staged-test"

    def __init__(self, revised="# 技术标题\n\n事实 [PDF p.1]。", draft=None, review_issues=None):
        self.calls = []
        self.revised = revised
        self.draft = draft or "# 技术标题\n\n值得注意的是，事实 [PDF p.1]。"
        self.review_issues = review_issues or []

    def complete(self, system, user, json_mode=False):
        self.calls.append(system)
        if system == articles.NOTES_PROMPT:
            return {"facts": FACTS, "limitations": []}
        if system == articles.OUTLINE_PROMPT:
            data = outline()
            for section in data["sections"]:
                section["figures"] = []
            return data
        if system == articles.ARTICLE_PROMPT:
            return self.draft
        if system == articles.REVIEW_PROMPT:
            return {"issues": self.review_issues}
        if system == articles.REWRITE_PROMPT:
            return self.revised
        raise AssertionError("Unexpected extra model call")


def run_generation(tmp_path, monkeypatch, model, **options):
    paper = {
        "id": "2610.00001",
        "version_id": "2610.00001v1",
        "title": "Paper",
        "url": "https://arxiv.org/abs/2610.00001v1",
        "pdf_url": "unused",
    }
    monkeypatch.setattr(articles, "request_bytes", lambda url: b"pdf")
    monkeypatch.setattr(articles, "extract_pages", lambda raw: [{"page": 1, "text": "source"}])
    monkeypatch.setattr(articles, "collect_figures", lambda *args: ([], ["源码不可用，纯文字。"]))
    return articles.generate(paper, "解释机制", model, tmp_path, **options)


def test_generation_rewrites_once_and_keeps_evidence_out_of_body(tmp_path, monkeypatch):
    model = StagedModel()
    article = run_generation(tmp_path, monkeypatch, model)
    assert model.calls == [
        articles.NOTES_PROMPT,
        articles.OUTLINE_PROMPT,
        articles.ARTICLE_PROMPT,
        articles.REVIEW_PROMPT,
        articles.REWRITE_PROMPT,
    ]
    assert article["notes"]["review"]["rewrite_applied"]
    assert article["notes"]["review"]["lint_after"] == []
    assert "值得注意" not in article["markdown"] and "核对依据" not in article["markdown"]
    directory = tmp_path / "2610.00001v1"
    assert "source" in (directory / "review.md").read_text()
    assert "[PDF p." not in article["markdown"]
    assert "[PDF p.1]" in (directory / "article-cited.md").read_text()
    assert article["notes"]["citations"][0]["pages"] == [1]
    assert not lint_article("# 技术标题\n\n## 总结\n\n该机制仍需按 GPU 校准。")
    assert json.loads((directory / "notes.json").read_text())["limitations"]
    assert (directory / "outline.json").is_file() and (directory / "draft.md").is_file()


def test_model_review_can_trigger_rewrite_without_lint(tmp_path, monkeypatch):
    issue = {"location": "事实", "problem": "缺少承接", "suggestion": "补充原因"}
    model = StagedModel(draft="# 技术标题\n\n事实 [PDF p.1]。", review_issues=[issue])
    article = run_generation(tmp_path, monkeypatch, model)
    assert article["notes"]["review"]["lint_before"] == []
    assert article["notes"]["review"]["rewrite_applied"]


def test_unresolved_style_is_retained_with_limitations_not_looped(tmp_path, monkeypatch):
    model = StagedModel(revised="# 技术标题\n\n值得注意的是，事实 [PDF p.1]。")
    article = run_generation(tmp_path, monkeypatch, model)
    assert model.calls.count(articles.REWRITE_PROMPT) == 1
    assert article["notes"]["review"]["lint_after"]
    assert any("写作自查" in value for value in article["notes"]["limitations"])


def test_invalid_rewrite_keeps_valid_draft_with_explicit_diagnostic(tmp_path, monkeypatch):
    model = StagedModel(revised="# 技术标题\n\n事实 [PDF p.999]。")
    article = run_generation(tmp_path, monkeypatch, model)
    assert not article["notes"]["review"]["rewrite_applied"]
    assert "[PDF p.999]" not in article["markdown"]
    assert any("改写稿未通过" in value for value in article["notes"]["limitations"])


def test_initial_draft_with_unlisted_image_is_never_saved(tmp_path, monkeypatch):
    model = StagedModel(draft="# 技术标题\n\n事实 [PDF p.1]。\n\n![图](figures/fake.png)")
    with pytest.raises(ValueError, match="清单外图片"):
        run_generation(tmp_path, monkeypatch, model)
    assert not (tmp_path / "2610.00001v1/article.md").exists()
    assert articles.REVIEW_PROMPT not in model.calls


def test_style_checks_repeated_starts_section_endings_and_unquantified_claims():
    paragraphs = [
        "论文提出" + "模型利用内核特征建立干扰预测，并根据执行时间约束后台任务提交。" * (i + 1)
        for i in range(3)
    ]
    paragraphs += [
        "离线阶段" + "选择合适的资源分配并准备性能特征。" * 4,
        "在线控制" + "依据每个请求剩余的预算决定是否放行。" * 5,
    ]
    text = (
        "# 技术机制\n\n## 请求预算\n\n"
        + "\n\n".join(paragraphs)
        + "\n\n这意味着吞吐还需要结合延迟一起评估。"
    )
    issues = lint_article(text)
    assert any("段首句式重复" in i["problem"] for i in issues)
    assert any("节末" in i["problem"] for i in issues)
    assert any(
        "量化限定" in i["problem"] for i in lint_article("# 性能\n\n该系统显著提升吞吐。[PDF p.8]")
    )
    assert not any(
        "量化限定" in i["problem"]
        for i in lint_article("# 性能\n\n该系统显著提升吞吐 18%。[PDF p.8]")
    )
    assert any(
        "脚手架" in i["problem"] for i in lint_article("# 方法\n\n首先读取。其次预测。最后调度。")
    )


def test_review_and_rewrite_receive_exact_quotes_per_section(tmp_path, monkeypatch):
    class CheckingModel(StagedModel):
        def complete(self, system, user, json_mode=False):
            if system in (articles.REVIEW_PROMPT, articles.REWRITE_PROMPT):
                data = json.loads(user)
                assert data["section_evidence"][0]["goal"] == "解释问题"
                assert data["section_evidence"][0]["transition"] == "承接前文"
                assert data["section_evidence"][0]["fact_quotes"] == FACTS
            return super().complete(system, user, json_mode)

    run_generation(tmp_path, monkeypatch, CheckingModel())


def test_invalid_pdf_does_not_start_source_download(tmp_path, monkeypatch):
    monkeypatch.setattr(articles, "request_bytes", lambda url: b"bad pdf")

    def bad_pdf(raw):
        raise ValueError("PDF 无法读取")

    monkeypatch.setattr(articles, "extract_pages", bad_pdf)
    monkeypatch.setattr(
        articles, "collect_figures", lambda *args: pytest.fail("Should not start download")
    )
    with pytest.raises(ValueError, match="PDF"):
        articles.generate(
            {"version_id": "2610.00001v1", "pdf_url": "unused"}, "", StagedModel(), tmp_path
        )


def test_editorial_material_flows_to_generation_and_asset_manifest(tmp_path, monkeypatch):
    import pymupdf

    context = {
        "verified": True,
        "description": "经人工核实的团队资料。",
        "sources": [{"title": "机构网页", "url": "https://example.edu/lab"}],
    }
    asset = tmp_path / "budget.png"
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=50)
        doc[0].get_pixmap().save(asset)

    class MaterialModel(StagedModel):
        def complete(self, system, user, json_mode=False):
            if system != articles.NOTES_PROMPT:
                data = json.loads(user)
                assert data["team_context"]["description"] == context["description"]
                assert data["writing_options"]["include_summary"] is False
                assert data["figures"][0]["origin"] == "author_diagram"
                assert data["figures"][0]["assumptions"] == "假设数字，非实验结果。"
            return super().complete(system, user, json_mode)

    result = run_generation(
        tmp_path,
        monkeypatch,
        MaterialModel(),
        team_context=context,
        include_summary=False,
        illustrations=[
            {"path": asset, "caption": "预算准入示意", "assumptions": "假设数字，非实验结果。"}
        ],
    )
    assert (tmp_path / "2610.00001v1/figures/illustration-01.png").is_file()
    assert "自绘假设" in (tmp_path / "2610.00001v1/review.md").read_text()
    assert result["notes"]["team_context"]["verification_method"] == "caller_reviewed_sources"


def test_unverified_team_context_is_rejected_before_network(tmp_path, monkeypatch):
    from pipeline.editorial import validate_team_context

    with pytest.raises(ValueError, match="核实"):
        run_generation(
            tmp_path, monkeypatch, StagedModel(), team_context={"description": "著名团队"}
        )
    with pytest.raises(ValueError, match="HTTPS"):
        validate_team_context(
            {
                "verified": True,
                "description": "团队",
                "sources": [{"title": "网页", "url": "javascript:alert(1)"}],
            }
        )
