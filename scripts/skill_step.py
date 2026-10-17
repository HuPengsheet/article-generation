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
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator import sources
from generator.evidence import NOTES_PROMPT, chunks, merge_materials, validate_notes
from generator.output import save_output, write_json
from generator.styles import inject_style, load_style, style_context, writing_targets
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

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

STEPS = ("evidence", "outline", "article", "review", "rewrite", "critique", "revise")


def _read_state(work_dir):
    return json.loads((Path(work_dir) / "state.json").read_text("utf-8"))


def _write_state(work_dir, state):
    write_json(Path(work_dir) / "state.json", state)


# Long text moves to Markdown files: the Read tool truncates very long lines, so a
# one-line JSON payload would silently hide figures, source text and references.
CONTEXT_FILES = {
    "source_blocks": ("sources.md", "原文正文块"),
    "reference_articles": ("references.md", "范文（已发表的完整文章）"),
    "article": ("article.md", "待审阅 / 待修订的文章"),
}


def _context_markdown(key, value):
    if key == "article":
        return value
    if key == "reference_articles":
        return "\n\n---\n\n".join(
            f"<!-- 范文 {item['name']} -->\n\n{item['markdown']}" for item in value
        )
    return "\n\n".join(
        f"## [{block['ref']}] {block.get('heading', '')}\n\n{block['text']}" for block in value
    )


def _write_current(work_dir, meta, system, user):
    current = Path(work_dir) / "current"
    current.mkdir(parents=True, exist_ok=True)
    for name in ("response.json", "response.md", *(f for f, _ in CONTEXT_FILES.values())):
        path = current / name
        if path.exists():
            path.unlink()
    if (current / "critic").exists():
        shutil.rmtree(current / "critic")
    user, files = _split_context(current, user)
    meta = {**meta, "context_files": files}
    write_json(current / "meta.json", meta)
    (current / "system.md").write_text(system, encoding="utf-8")
    write_json(current / "user.json", user)


def _split_context(directory, user):
    """Write long fields to Markdown files beside user.json and point to them."""
    files = []
    user = dict(user)
    for key, (filename, label) in CONTEXT_FILES.items():
        if user.get(key):
            (directory / filename).write_text(
                _context_markdown(key, user.pop(key)) + "\n", encoding="utf-8"
            )
            user[key] = f"见同目录 {filename}"
            files.append({"file": filename, "content": label})
    return user, files


def _read_response(work_dir, fmt, root=False):
    """Read current/response.*; with root=True, `work_dir` is the response directory."""
    current = Path(work_dir) if root else Path(work_dir) / "current"
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
    # Same blocks the validator checks quotes against; the text goes to sources.md.
    user = {
        "blocks": [{k: v for k, v in block.items() if k != "text"} for block in part],
        "source_blocks": part,
    }
    return meta, NOTES_PROMPT, user


SYSTEMS = {
    "outline": GENERIC_OUTLINE_PROMPT,
    "article": GENERIC_ARTICLE_PROMPT,
    "review": GENERIC_REVIEW_PROMPT,
    "rewrite": GENERIC_REWRITE_PROMPT,
    "revise": REVISE_PROMPT,
}
FORMATS = {
    "outline": "json",
    "article": "markdown",
    "review": "json",
    "rewrite": "markdown",
    "critique": "json",
    "revise": "markdown",
}


def _prepare_stage(state, style, stage, **extra):
    meta = {"step": stage, "format": FORMATS[stage]}
    if stage in ("article", "rewrite", "revise"):
        meta["min_chars"] = state["evidence"]["targets"]["min_chars"]
    user = stage_payload(state["evidence"], style, stage, **extra)
    return meta, inject_style(SYSTEMS[stage], style), user


def _advance(work_dir, state, style, stage, **extra):
    state["step"] = stage
    state["attempts"] = 0
    _write_state(work_dir, state)
    if stage == "critique":
        _write_critic(work_dir, state, style)
        print(f"CRITIQUE: {Path(work_dir).resolve() / 'current/critic'}")
        return
    _write_current(work_dir, *_prepare_stage(state, style, stage, **extra))
    print(f"READY: {Path(work_dir) / 'current/'}")


def _write_critic(work_dir, state, style):
    """A self-contained task directory, so any host can hand it to a fresh subagent:
    the agent only reads files in this directory and writes one file."""
    current = Path(work_dir) / "current"
    _write_current(work_dir, {"step": "critique", "format": "json"}, "", {})
    (current / "system.md").unlink()
    (current / "user.json").unlink()
    directory = current / "critic"
    directory.mkdir()
    payload = stage_payload(state["evidence"], style, "critique", article=state["article"])
    user, files = _split_context(directory, payload)
    write_json(directory / "user.json", user)
    names = "、".join(["user.json"] + [item["file"] for item in files])
    (directory / "task.md").write_text(
        f"{CRITIC_PROMPT}\n\n## 输入与输出\n\n"
        f"完整读取本目录下的 {names}（较长的文件分段读完）。"
        "审稿意见写入本目录的 response.json，只写纯 JSON，不加代码围栏。"
        "不要修改本目录以外的任何文件。\n",
        encoding="utf-8",
    )
    write_json(
        current / "meta.json",
        {"step": "critique", "format": "json", "critic_dir": str(directory.resolve())},
    )


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
    if not evidence["figures"]:
        write_json(work_dir / "materials.json", evidence)
        raise ValueError("未取得可用配图，保留材料；请先提取原图，不能生成无图完成稿。")

    # Load style
    style = load_style(args.style)
    examples = style_context(style)
    if examples:
        evidence["style_examples"] = examples
    evidence["targets"] = writing_targets(
        style,
        args.depth,
        len(evidence["figures"]),
        sum(len(block["text"]) for block in evidence["sections"]),
    )

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
    if step == "done":
        print("ERROR: 该工作目录已完成生成。")
        return 1

    meta_path = work_dir / "current" / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    fmt = meta["format"]

    try:
        response = (
            _read_response(work_dir / "current" / "critic", fmt, root=True)
            if step == "critique"
            else _read_response(work_dir, fmt)
        )
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
        elif step == "critique":
            _handle_critique(state, response, work_dir, style)
        elif step == "revise":
            _handle_revise(state, response, work_dir, style)
        else:
            print(f"ERROR: 未知步骤 {step}")
            return 1
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    return 0


# Validation failures print ERROR and keep the step; after this many the content
# checks (length, figures) give way so a run cannot loop forever.
MAX_ATTEMPTS = 3


def _handle_evidence(state, response, work_dir, style):
    idx = state["chunk_index"]
    all_chunks = list(chunks(state["evidence"]["sections"]))
    part = all_chunks[idx]

    notes = validate_notes(response, part)
    state["facts"].extend(notes["facts"])
    state["limitations"].extend(notes.get("limitations", []))

    next_idx = idx + 1
    if next_idx < len(all_chunks):
        state["chunk_index"] = next_idx
        _write_state(work_dir, state)
        _write_current(work_dir, *_prepare_evidence_step(state, style))
        print(f"READY: {work_dir / 'current/'}")
        return
    if not state["facts"]:
        raise ValueError("没有提取到可验证事实，已停止生成。")
    state["evidence"]["facts"] = state["facts"]
    state["evidence"]["limitations"].extend(state["limitations"])
    write_json(work_dir / "notes.json", state["evidence"])
    _advance(work_dir, state, style, "outline")


def _handle_outline(state, response, work_dir, style):
    evidence = state["evidence"]
    outline = validate_generic_outline(
        response, state["facts"], evidence["figures"], evidence["targets"]
    )
    state["outline"] = outline
    evidence["outline"] = outline
    evidence["section_evidence"] = generic_section_evidence(outline, state["facts"])
    evidence["source_blocks"] = outline_source_blocks(outline, evidence["sections"])
    write_json(work_dir / "outline.json", outline)
    _advance(work_dir, state, style, "article")


def _check_article(state, response, previous=None):
    """Hard checks first; content targets are relaxed after repeated attempts."""
    evidence = state["evidence"]
    state["attempts"] = state.get("attempts", 0) + 1
    try:
        article = validate_generic_article(
            response, state["facts"], evidence["figures"], state["outline"], evidence["targets"]
        )
        return validate_rewrite(previous, article) if previous else article
    except ValueError as error:
        if state["attempts"] < MAX_ATTEMPTS or previous:
            raise
        # Citations and figure paths remain mandatory.
        article = validate_generic_article(response, state["facts"], evidence["figures"])
        evidence["limitations"].append(f"多次修改后仍未达标：{error}")
        return article


def _handle_article(state, response, work_dir, style):
    try:
        article = _check_article(state, response)
    except ValueError:
        _write_state(work_dir, state)
        raise
    state["article"] = article
    (work_dir / "draft.md").write_text(article, encoding="utf-8")
    state["lint_before"] = lint_article(article, style=style)
    _advance(work_dir, state, style, "review", article=article, lint=state["lint_before"])


def _handle_review(state, response, work_dir, style):
    issues = validate_review(response)
    lint_before = state.get("lint_before", [])
    review = {
        "lint_before": lint_before,
        "issues": issues,
        "rewrite_attempted": False,
        "rewrite_applied": False,
    }
    state["review"] = review
    if issues or lint_before:
        _advance(
            work_dir,
            state,
            style,
            "rewrite",
            article=state["article"],
            issues=issues,
            lint=lint_before,
        )
        return
    _advance(work_dir, state, style, "critique")


def _handle_rewrite(state, response, work_dir, style):
    review = state["review"]
    review["rewrite_attempted"] = True
    try:
        state["article"] = _check_article(state, response, previous=state["article"])
        review["rewrite_applied"] = True
    except ValueError as error:
        if state["attempts"] < MAX_ATTEMPTS:
            _write_state(work_dir, state)
            raise
        review["rewrite_error"] = str(error)
        state["evidence"]["limitations"].append(f"改写未通过校验，保留初稿：{error}")
    _advance(work_dir, state, style, "critique")


def _handle_critique(state, response, work_dir, style):
    critique = validate_critique(response)
    state["review"].update(critique=critique, revise_attempted=False)
    write_json(Path(work_dir) / "critique.json", critique)
    if not needs_revision(critique):
        _finish(state, work_dir, style)
        return
    _advance(work_dir, state, style, "revise", article=state["article"], critique=critique)


def _handle_revise(state, response, work_dir, style):
    review = state["review"]
    review["revise_attempted"] = True
    try:
        state["article"] = _check_article(state, response, previous=state["article"])
        review["revise_applied"] = True
    except ValueError as error:
        if state["attempts"] < MAX_ATTEMPTS:
            _write_state(work_dir, state)
            raise
        review["revise_error"] = str(error)
        state["evidence"]["limitations"].append(f"二轮修改未通过校验，保留上一稿：{error}")
    _finish(state, work_dir, style)


def _finish(state, work_dir, style):
    review = state["review"]
    review["lint_after"] = lint_article(state["article"], style=style)
    state["evidence"]["review"] = review
    state["step"] = "done"
    _write_state(work_dir, state)
    result = save_output(state["article"], state["evidence"], work_dir)
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
    init_p.add_argument("--depth", default="detailed", choices=["brief", "detailed", "deep-dive"])
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
