"""The same evidence-first generator serves CLI, Python callers, and the web adapter."""

import json
from pathlib import Path

from generator.editorial import prepare_illustrations, validate_team_context
from generator.evidence import extract_evidence, merge_materials
from generator.output import save_output, write_json
from generator.writer import (
    GENERIC_ARTICLE_PROMPT,
    GENERIC_OUTLINE_PROMPT,
    GENERIC_REVIEW_PROMPT,
    GENERIC_REWRITE_PROMPT,
    generic_section_evidence,
    lint_article,
    validate_generic_article,
    validate_generic_outline,
    validate_review,
)


def generate(
    materials,
    llm,
    work_dir,
    *,
    angle="",
    audience="工程师",
    depth="detailed",
    include_summary=True,
    team_context=None,
    illustrations=None,
    progress=lambda message: None,
):
    team_context = validate_team_context(team_context)
    if (
        not isinstance(include_summary, bool)
        or depth not in ("brief", "detailed", "deep-dive")
        or not isinstance(audience, str)
        or not audience.strip()
        or not isinstance(angle, str)
    ):
        raise ValueError("写作选项无效。")
    directory = Path(work_dir)
    progress("合并材料与配图")
    evidence = merge_materials(materials, directory)
    evidence["figures"].extend(prepare_illustrations(illustrations, directory))
    facts, limitations = extract_evidence(evidence["sections"], llm, progress)
    evidence["limitations"].extend(limitations)
    evidence.update(
        facts=facts,
        angle=angle,
        model=llm.model,
        team_context=team_context,
        writing_options={"audience": audience, "depth": depth, "include_summary": include_summary},
    )
    write_json(
        directory / "materials.json",
        {"sources": evidence["sources"], "sections": evidence["sections"]},
    )
    write_json(directory / "notes.json", evidence)
    progress("设计写作大纲")
    outline = validate_generic_outline(
        llm.complete(
            GENERIC_OUTLINE_PROMPT,
            json.dumps({k: v for k, v in evidence.items() if k != "sections"}, ensure_ascii=False),
            True,
        ),
        facts,
        evidence["figures"],
    )
    evidence["outline"] = outline
    evidence["section_evidence"] = generic_section_evidence(outline, facts)
    write_json(directory / "outline.json", outline)
    progress("按大纲生成草稿")
    article = validate_generic_article(
        llm.complete(
            GENERIC_ARTICLE_PROMPT,
            json.dumps({k: v for k, v in evidence.items() if k != "sections"}, ensure_ascii=False),
        ),
        facts,
        evidence["figures"],
    )
    (directory / "draft.md").write_text(article, encoding="utf-8")
    initial_lint = lint_article(article)
    progress("自查逻辑、证据与写作风格")
    issues = validate_review(
        llm.complete(
            GENERIC_REVIEW_PROMPT,
            json.dumps(
                {
                    **{k: v for k, v in evidence.items() if k != "sections"},
                    "article": article,
                    "lint": initial_lint,
                },
                ensure_ascii=False,
            ),
            True,
        )
    )
    review = {
        "lint_before": initial_lint,
        "issues": issues,
        "rewrite_attempted": False,
        "rewrite_applied": False,
    }
    if issues or initial_lint:
        progress("根据自查结果改写一次")
        review["rewrite_attempted"] = True
        revised = llm.complete(
            GENERIC_REWRITE_PROMPT,
            json.dumps(
                {
                    **{k: v for k, v in evidence.items() if k != "sections"},
                    "article": article,
                    "issues": issues,
                    "lint": initial_lint,
                },
                ensure_ascii=False,
            ),
        )
        try:
            article = validate_generic_article(revised, facts, evidence["figures"])
            review["rewrite_applied"] = True
        except ValueError as error:
            review["rewrite_error"] = str(error)
            evidence["limitations"].append(f"改写未通过校验，保留初稿：{error}")
    review["lint_after"] = lint_article(article)
    evidence["review"] = review
    return save_output(article, evidence, directory)


def generate_arxiv(paper, angle, llm, directory, progress=lambda message: None, **options):
    """Web compatibility adapter. No collection, store, or selection dependency."""
    from generator.identifiers import safe_id
    from generator.sources.url import UrlSource

    work_dir = Path(directory) / safe_id(paper["version_id"])
    material = UrlSource().extract_paper(paper, work_dir / "sources/S1")
    result = generate([material], llm, work_dir, angle=angle, progress=progress, **options)
    return result.as_dict()
