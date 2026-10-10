"""Collect a single, complete announcement batch from all active arXiv archives."""

import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

from shared.http import request_bytes

ARXIV_NS = "http://arxiv.org/schemas/atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
USER_AGENT = "article-generation-mvp/0.1 (personal research digest)"


def discover_categories(markup):
    categories = sorted(
        set(re.findall(r"<h4[^>]*>\s*([a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?)\s", markup))
    )
    if len(categories) < 100:
        raise ValueError("arXiv 分类页面结构异常，不能确认全量分类覆盖。")
    return categories


def parse_feed(raw):
    root = ET.fromstring(raw)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("不是有效的 arXiv RSS。")
    date = parsedate_to_datetime(channel.findtext("pubDate", "")).date().isoformat()
    items = channel.findall("item")
    papers = []
    for item in items:
        description = html.unescape(item.findtext("description", ""))
        kind = item.findtext(f"{{{ARXIV_NS}}}announce_type", "").strip()
        if not kind:
            match = re.search(r"Announce Type:\s*([\w-]+)", description)
            kind = match.group(1) if match else ""
        if not kind:
            raise ValueError("论文缺少公告类型，无法区分新论文和更新。")
        # Every primary archive is collected; cross-listings and replacements are not new papers.
        if kind != "new":
            continue
        link = item.findtext("link", "")
        identifier = re.search(r"/abs/([\w./-]+)", link)
        if not identifier:
            raise ValueError("论文缺少有效 arXiv ID。")
        paper_id = re.sub(r"v\d+$", "", identifier.group(1))
        version_match = re.search(r"arXiv:([\w./-]+v\d+)", description)
        version_id = version_match.group(1) if version_match else paper_id + "v1"
        abstract = description.split("Abstract:", 1)
        if len(abstract) != 2:
            raise ValueError(f"{paper_id} 缺少摘要。")
        item_date = parsedate_to_datetime(item.findtext("pubDate", "")).date().isoformat()
        if item_date != date:
            raise ValueError("同一个 RSS 中出现不同公告日，拒绝混合批次。")
        papers.append(
            {
                "id": paper_id,
                "version_id": version_id,
                "title": item.findtext("title", "").strip(),
                "abstract": abstract[1].strip(),
                "authors": item.findtext(f"{{{DC_NS}}}creator", ""),
                "categories": [c.text for c in item.findall("category") if c.text],
                "keywords": [],
                "announcement_date": date,
                "url": f"https://arxiv.org/abs/{version_id}",
                "pdf_url": f"https://arxiv.org/pdf/{version_id}",
                "announce_type": kind,
            }
        )
    return date, papers, len(items)


def collect(cache_dir, progress=lambda message: None, requested_date=None):
    """Only return after all archives are fetched for the same announcement date.

    Persist each response, so failures can resume without re-fetching completed archives.
    RSS is current-only: explicit historical dates must already exist in the cache.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if requested_date:
        from datetime import date as calendar_date

        try:
            if calendar_date.fromisoformat(requested_date).isoformat() != requested_date:
                raise ValueError
        except ValueError:
            raise ValueError("公告日必须为 YYYY-MM-DD。") from None
    cached_taxonomy = cache_dir / requested_date / "taxonomy.html" if requested_date else None
    taxonomy = (
        cached_taxonomy.read_bytes()
        if cached_taxonomy and cached_taxonomy.is_file()
        else request_bytes("https://arxiv.org/category_taxonomy")
    )
    categories = discover_categories(taxonomy.decode("utf-8"))
    archives = sorted(set(category.split(".")[0] for category in categories))
    # Establish the announcement date from the first live feed, rather than a local clock.
    if requested_date and (cache_dir / requested_date / f"{archives[0]}.xml").exists():
        first = (cache_dir / requested_date / f"{archives[0]}.xml").read_bytes()
    else:
        first = request_bytes(f"https://rss.arxiv.org/rss/{archives[0]}")
    date, _, _ = parse_feed(first)
    if requested_date and date != requested_date:
        raise ValueError(
            f"RSS 当前公告日为 {date}，无法获取 {requested_date}；历史日期需要已有缓存。"
        )
    directory = cache_dir / date
    directory.mkdir(exist_ok=True)
    (directory / f"{archives[0]}.xml").write_bytes(first)
    (directory / "taxonomy.html").write_bytes(taxonomy)
    merged = {}
    manifest = {
        "announcement_date": date,
        "archives": archives,
        "categories": categories,
        "feeds": [],
        "complete": False,
        "scope": "all active archives; new only",
    }

    def fetch(feed):
        target = directory / f"{feed}.xml"
        if target.exists():
            raw = target.read_bytes()
        else:
            time.sleep(3)
            raw = request_bytes(f"https://rss.arxiv.org/rss/{feed}")
        feed_date, papers, count = parse_feed(raw)
        if feed_date != date:
            raise ValueError(f"{feed} 的公告日为 {feed_date}，与 {date} 不一致，请稍后重试。")
        target.write_bytes(raw)
        manifest["feeds"].append(
            {
                "feed": feed,
                "items": count,
                "new": len(papers),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        if count >= 2000:
            children = [c for c in categories if c.split(".")[0] == feed and c != feed]
            if not children:
                raise ValueError(f"{feed} 达到 RSS 结果上限，无法证明完整性。")
            progress(f"{feed} 达到结果上限，改为采集各子分类")
            for child in children:
                fetch(child)
            return
        for paper in papers:
            previous = merged.get(paper["id"])
            if previous:
                previous["categories"] = sorted(set(previous["categories"] + paper["categories"]))
            else:
                merged[paper["id"]] = paper

    try:
        for index, archive in enumerate(archives):
            progress(f"采集 {index + 1}/{len(archives)}：{archive} · 公告日 {date}")
            fetch(archive)
        manifest["complete"] = True
        manifest["unique_new_papers"] = len(merged)
    finally:
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    papers = sorted(merged.values(), key=lambda paper: paper["id"])
    (directory / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=2))
    return date, papers, manifest
