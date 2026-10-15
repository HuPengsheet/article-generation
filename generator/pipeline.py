"""The same evidence-first generator serves CLI, Python callers, and the web adapter."""

import json
from pathlib import Path

from generator.editorial import prepare_illustrations, validate_team_context
from generator.evidence import extract_evidence, merge_materials
from generator.output import save_output, write_json
from generator.styles import Style, inject_style, load_style, style_context, writing_targets
from generator.writer import (
    CRITIC_PROMPT,
    GENERIC_ARTICLE_PROMPT,
    GENERIC_OUTLINE_PROMPT,
    GENERIC_REVIEW_PROMPT,
    GENERIC_REWRITE_PROMPT,
    REVISE_PROMPT,
    generic_section_evidence,
    lint_article,
    needs_revision,
    outline_source_blocks,
    stage_payload,
    validate_critique,
    validate_generic_article,
    validate_generic_outline,
    validate_review,
    validate_rewrite,
)


def generate(
    materials,
    llm,
    work_dir,
    *,
    style=None,
    angle="",
    audience="工程师",
    depth="detailed",
    include_summary=True,
    team_context=None,
    illustrations=None,
    progress=lambda message: None,
):
    team_context = validate_team_context(team_context)
    if isinstance(style, str) or style is None:
        style = load_style(style)
    if not isinstance(style, (Style, type(None))):
        raise ValueError("style 参数无效。")
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
        style=style.name if style else None,
    )
    examples = style_context(style)
    if examples:
        evidence["style_examples"] = examples
    evidence["targets"] = writing_targets(
        style,
        depth,
        len(evidence["figures"]),
        sum(len(block["text"]) for block in evidence["sections"]),
    )
    write_json(
        directory / "materials.json",
        {"sources": evidence["sources"], "sections": evidence["sections"]},
    )
    write_json(directory / "notes.json", evidence)
    outline_prompt = inject_style(GENERIC_OUTLINE_PROMPT, style)
    article_prompt = inject_style(GENERIC_ARTICLE_PROMPT, style)
    review_prompt = inject_style(GENERIC_REVIEW_PROMPT, style)
    rewrite_prompt = inject_style(GENERIC_REWRITE_PROMPT, style)

    def ask(prompt, stage, validate, json_mode=False, fallback=None):
        """Call the model, feeding validator errors back for up to two corrections.
        `fallback` keeps only the hard checks once content targets keep failing."""
        payload = stage_payload(evidence, style, stage)
        for attempt in range(3):
            result = llm.complete(prompt, json.dumps(payload, ensure_ascii=False), json_mode)
            try:
                return validate(result)
            except ValueError as error:
                if attempt == 2:
                    if fallback is None:
                        raise
                    accepted = fallback(result)
                    evidence["limitations"].append(f"多次修改后仍未达标：{error}")
                    return accepted
                progress(f"{stage} 未通过校验，重试：{error}")
                payload = {**payload, "previous_error": str(error)}

    targets = evidence["targets"]
    progress("设计写作大纲")
    outline = ask(
        outline_prompt,
        "outline",
        lambda result: validate_generic_outline(result, facts, evidence["figures"], targets),
        True,
    )
    evidence["outline"] = outline
    evidence["section_evidence"] = generic_section_evidence(outline, facts)
    evidence["source_blocks"] = outline_source_blocks(outline, evidence["sections"])
    write_json(directory / "outline.json", outline)
    progress("按大纲生成草稿")
    article = ask(
        article_prompt,
        "article",
        lambda result: validate_generic_article(
            result, facts, evidence["figures"], outline, targets
        ),
        fallback=lambda result: validate_generic_article(result, facts, evidence["figures"]),
    )
    (directory / "draft.md").write_text(article, encoding="utf-8")
    initial_lint = lint_article(article, style=style)
    progress("自查逻辑、证据与写作风格")
    issues = validate_review(
        llm.complete(
            review_prompt,
            json.dumps(
                stage_payload(evidence, style, "review", article=article, lint=initial_lint),
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
            rewrite_prompt,
            json.dumps(
                stage_payload(
                    evidence, style, "rewrite", article=article, issues=issues, lint=initial_lint
                ),
                ensure_ascii=False,
            ),
        )
        try:
            article = validate_rewrite(
                article,
                validate_generic_article(revised, facts, evidence["figures"], outline, targets),
            )
            review["rewrite_applied"] = True
        except ValueError as error:
            review["rewrite_error"] = str(error)
            evidence["limitations"].append(f"改写未通过校验，保留初稿：{error}")
    progress("独立审稿：整体质量、逻辑一致性、AI 味")
    # A separate call gives the critic a fresh context, like a subagent.
    critique = validate_critique(
        llm.complete(
            CRITIC_PROMPT,
            json.dumps(
                stage_payload(evidence, style, "critique", article=article), ensure_ascii=False
            ),
            True,
        )
    )
    review.update(critique=critique, revise_attempted=False, revise_applied=False)
    write_json(directory / "critique.json", critique)
    if needs_revision(critique):
        progress("根据审稿意见进行二轮修改")
        review["revise_attempted"] = True
        revised = llm.complete(
            inject_style(REVISE_PROMPT, style),
            json.dumps(
                stage_payload(evidence, style, "revise", article=article, critique=critique),
                ensure_ascii=False,
            ),
        )
        try:
            article = validate_rewrite(
                article,
                validate_generic_article(revised, facts, evidence["figures"], outline, targets),
            )
            review["revise_applied"] = True
        except ValueError as error:
            review["revise_error"] = str(error)
            evidence["limitations"].append(f"二轮修改未通过校验，保留上一稿：{error}")
    review["lint_after"] = lint_article(article, style=style)
    evidence["review"] = review
    return save_output(article, evidence, directory)
