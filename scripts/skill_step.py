#!/usr/bin/env python3
"""Step-based orchestrator for AI-assisted article generation.

Two subcommands:
  init <source> <work_dir> [options]   – download & extract materials, prepare first step
  next <work_dir>                      – validate response, advance to next step

Each step writes current/{meta.json, system.md, user.json}. The AI reads these,
generates a response into current/response.json or response.md, then calls `next`.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator import sources
from generator.evidence import NOTES_PROMPT, chunks, merge_materials, validate_notes
from generator.output import save_output, write_json
from generator.styles import inject_style, load_style, style_context
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

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

STEPS = ("evidence", "outline", "article", "review", "rewrite")


def _read_state(work_dir):
    return json.loads((Path(work_dir) / "state.json").read_text("utf-8"))


def _write_state(work_dir, state):
    write_json(Path(work_dir) / "state.json", state)


def _write_current(work_dir, meta, system, user):
    current = Path(work_dir) / "current"
    current.mkdir(parents=True, exist_ok=True)
    write_json(current / "meta.json", meta)
    (current / "system.md").write_text(system, encoding="utf-8")
    # user.json is always a JSON string (may be large)
    if isinstance(user, str):
        (current / "user.json").write_text(user, encoding="utf-8")
    else:
        write_json(current / "user.json", user)
    # Clean previous response files
    for name in ("response.json", "response.md"):
        path = current / name
        if path.exists():
            path.unlink()


def _read_response(work_dir, fmt):
    current = Path(work_dir) / "current"
    if fmt == "json":
        path = current / "response.json"
        if not path.exists():
            raise FileNotFoundError("response.json not found")
        return json.loads(path.read_text("utf-8"))
    else:
        path = current / "response.md"
        if not path.exists():
            raise FileNotFoundError("response.md not found")
        return path.read_text("utf-8")


# ---------------------------------------------------------------------------
# Prompt preparation helpers
# ---------------------------------------------------------------------------


def _evidence_without_sections(state):
    """Build the evidence dict passed to outline/article/review/rewrite prompts,
    stripping bulky raw sections to stay within context limits."""
    return {k: v for k, v in state["evidence"].items() if k != "sections"}


def _prepare_evidence_step(state, style):
    """Prepare the next evidence chunk prompt."""
    idx = state["chunk_index"]
    all_chunks = list(chunks(state["evidence"]["sections"]))
    part = all_chunks[idx]
    meta = {
        "step": "evidence",
        "chunk": idx + 1,
        "total_chunks": len(all_chunks),
        "format": "json",
    }
    system = NOTES_PROMPT
    user = json.dumps(part, ensure_ascii=False)
    return meta, system, user


def _prepare_outline_step(state, style):
    ev = _evidence_without_sections(state)
    meta = {"step": "outline", "format": "json"}
    system = inject_style(GENERIC_OUTLINE_PROMPT, style)
    user = json.dumps(ev, ensure_ascii=False)
    return meta, system, user


def _prepare_article_step(state, style):
    ev = _evidence_without_sections(state)
    meta = {"step": "article", "format": "markdown"}
    system = inject_style(GENERIC_ARTICLE_PROMPT, style)
    user = json.dumps(ev, ensure_ascii=False)
    return meta, system, user


def _prepare_review_step(state, style, article):
    ev = _evidence_without_sections(state)
    lint_issues = lint_article(article, style=style)
    state["lint_before"] = lint_issues
    ev_with_article = {**ev, "article": article, "lint": lint_issues}
    meta = {"step": "review", "format": "json"}
    system = inject_style(GENERIC_REVIEW_PROMPT, style)
    user = json.dumps(ev_with_article, ensure_ascii=False)
    return meta, system, user


def _prepare_rewrite_step(state, style, article, issues):
    ev = _evidence_without_sections(state)
    ev_with_context = {
        **ev,
        "article": article,
        "issues": issues,
        "lint": state.get("lint_before", []),
    }
    meta = {"step": "rewrite", "format": "markdown"}
    system = inject_style(GENERIC_REWRITE_PROMPT, style)
    user = json.dumps(ev_with_context, ensure_ascii=False)
    return meta, system, user


# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------


def cmd_init(args):
    source_value = args.source
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Extract material
    handler = sources.detect(source_value)
    material = handler.extract(source_value, work_dir / "sources" / "S1")

    # Merge materials (single source)
    evidence = merge_materials([material], work_dir)

    # Load style
    style = load_style(args.style)
    examples = style_context(style)
    if examples:
        evidence["style_examples"] = examples

    # Build initial state
    all_chunks = list(chunks(evidence["sections"]))
    state = {
        "step": "evidence",
        "chunk_index": 0,
        "total_chunks": len(all_chunks),
        "facts": [],
        "limitations": [],
        "evidence": evidence,
        "outline": None,
        "article": None,
        "review": None,
        "model": "kiro",
        "style": style.name if style else None,
        "options": {
            "angle": args.angle or "",
            "audience": args.audience,
            "depth": args.depth,
            "include_summary": not args.no_summary,
        },
    }

    # Inject writing_options into evidence for prompt consumption
    evidence.update(
        facts=state["facts"],
        angle=state["options"]["angle"],
        model=state["model"],
        team_context=None,
        writing_options={
            "audience": state["options"]["audience"],
            "depth": state["options"]["depth"],
            "include_summary": state["options"]["include_summary"],
        },
        style=state["style"],
    )

    # Save materials.json (for reference)
    write_json(
        work_dir / "materials.json",
        {"sources": evidence["sources"], "sections": evidence["sections"]},
    )

    _write_state(work_dir, state)

    # Prepare first evidence chunk
    meta, system, user = _prepare_evidence_step(state, style)
    _write_current(work_dir, meta, system, user)

    print(f"READY: {work_dir / 'current/'}")
    return 0


# ---------------------------------------------------------------------------
# next subcommand
# ---------------------------------------------------------------------------


def cmd_next(args):
    work_dir = Path(args.work_dir).resolve()
    state = _read_state(work_dir)
    style = load_style(state["style"]) if state["style"] else None
    step = state["step"]
    evidence = state["evidence"]

    meta_path = work_dir / "current" / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    fmt = meta["format"]

    try:
        response = _read_response(work_dir, fmt)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR: 无法读取响应文件: {e}")
        return 1

    # --- Validate & advance ---
    try:
        if step == "evidence":
            _handle_evidence(state, response, work_dir, style)
        elif step == "outline":
            _handle_outline(state, response, work_dir, style)
        elif step == "article":
            _handle_article(state, response, work_dir, style)
        elif step == "review":
            _handle_review(state, response, work_dir, style)
        elif step == "rewrite":
            _handle_rewrite(state, response, work_dir, style)
        else:
            print(f"ERROR: 未知步骤 {step}")
            return 1
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    return 0


def _handle_evidence(state, response, work_dir, style):
    idx = state["chunk_index"]
    all_chunks = list(chunks(state["evidence"]["sections"]))
    part = all_chunks[idx]

    notes = validate_notes(response, part)
    state["facts"].extend(notes["facts"])
    state["limitations"].extend(notes.get("limitations", []))

    next_idx = idx + 1
    if next_idx < len(all_chunks):
        # More chunks to process
        state["chunk_index"] = next_idx
        state["step"] = "evidence"
        _write_state(work_dir, state)
        meta, system, user = _prepare_evidence_step(state, style)
        _write_current(work_dir, meta, system, user)
        print(f"READY: {work_dir / 'current/'}")
    else:
        # Evidence complete, move to outline
        state["evidence"]["facts"] = state["facts"]
        state["evidence"]["limitations"].extend(state["limitations"])
        state["evidence"].update(
            angle=state["options"]["angle"],
            model=state["model"],
            team_context=None,
            writing_options={
                "audience": state["options"]["audience"],
                "depth": state["options"]["depth"],
                "include_summary": state["options"]["include_summary"],
            },
        )
        write_json(work_dir / "notes.json", state["evidence"])
        state["step"] = "outline"
        _write_state(work_dir, state)
        meta, system, user = _prepare_outline_step(state, style)
        _write_current(work_dir, meta, system, user)
        print(f"READY: {work_dir / 'current/'}")


def _handle_outline(state, response, work_dir, style):
    facts = state["facts"]
    figures = state["evidence"]["figures"]
    outline = validate_generic_outline(response, facts, figures)

    state["outline"] = outline
    state["evidence"]["outline"] = outline
    state["evidence"]["section_evidence"] = generic_section_evidence(outline, facts)
    write_json(work_dir / "outline.json", outline)

    state["step"] = "article"
    _write_state(work_dir, state)
    meta, system, user = _prepare_article_step(state, style)
    _write_current(work_dir, meta, system, user)
    print(f"READY: {work_dir / 'current/'}")


def _handle_article(state, response, work_dir, style):
    facts = state["facts"]
    figures = state["evidence"]["figures"]
    article = validate_generic_article(response, facts, figures)

    state["article"] = article
    (work_dir / "draft.md").write_text(article, encoding="utf-8")

    state["step"] = "review"
    _write_state(work_dir, state)
    meta, system, user = _prepare_review_step(state, style, article)
    _write_state(work_dir, state)  # save lint_before
    _write_current(work_dir, meta, system, user)
    print(f"READY: {work_dir / 'current/'}")


def _handle_review(state, response, work_dir, style):
    issues = validate_review(response)

    lint_before = state.get("lint_before", [])
    review = {
        "lint_before": lint_before,
        "issues": issues,
        "rewrite_attempted": False,
        "rewrite_applied": False,
    }

    if issues or lint_before:
        # Need rewrite
        state["review"] = review
        state["step"] = "rewrite"
        _write_state(work_dir, state)
        meta, system, user = _prepare_rewrite_step(
            state, style, state["article"], issues
        )
        _write_current(work_dir, meta, system, user)
        print(f"READY: {work_dir / 'current/'}")
    else:
        # No issues, save and finish
        review["lint_after"] = lint_article(state["article"], style=style)
        state["review"] = review
        state["evidence"]["review"] = review
        _write_state(work_dir, state)
        result = save_output(state["article"], state["evidence"], work_dir)
        print(f"DONE: {result.directory}/article.md")


def _handle_rewrite(state, response, work_dir, style):
    facts = state["facts"]
    figures = state["evidence"]["figures"]
    review = state["review"]
    review["rewrite_attempted"] = True

    try:
        article = validate_generic_article(response, facts, figures)
        review["rewrite_applied"] = True
        state["article"] = article
    except ValueError as error:
        review["rewrite_error"] = str(error)
        state["evidence"]["limitations"].append(f"改写未通过校验，保留初稿：{error}")
        article = state["article"]

    review["lint_after"] = lint_article(article, style=style)
    state["review"] = review
    state["evidence"]["review"] = review
    _write_state(work_dir, state)
    result = save_output(article, state["evidence"], work_dir)
    print(f"DONE: {result.directory}/article.md")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="下载材料，准备第一步")
    init_p.add_argument("source", help="URL、PDF 或本地文件路径")
    init_p.add_argument("work_dir", help="工作目录")
    init_p.add_argument("--style", default=None, help="风格名称")
    init_p.add_argument("--angle", default="", help="写作角度")
    init_p.add_argument("--audience", default="工程师", help="目标读者")
    init_p.add_argument(
        "--depth", default="detailed", choices=["brief", "detailed", "deep-dive"]
    )
    init_p.add_argument("--no-summary", action="store_true", help="不写总结")

    next_p = sub.add_parser("next", help="校验响应并推进到下一步")
    next_p.add_argument("work_dir", help="工作目录")

    args = parser.parse_args()
    if args.command == "init":
        return cmd_init(args)
    elif args.command == "next":
        return cmd_next(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
