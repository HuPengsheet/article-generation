import io
import json
import os
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from pipeline.articles import generate, safe_id
from pipeline.arxiv import collect
from pipeline.editorial import validate_team_context
from pipeline.llm import LLM, validate_filter
from pipeline.store import Store
from pipeline.wechat import preview
from scripts.filter_papers import evaluate

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def create_app(data_dir=None, llm=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
    directory = Path(data_dir or ROOT / "data")
    store = Store(directory / "app.sqlite3")
    model = llm or LLM()
    job_lock = threading.Lock()
    config_path = ROOT / "config/ai_infra.json"
    app.extensions.update(store=store, llm=model, data_dir=directory)

    def filter_batch(batch, force=True):
        config = json.loads(config_path.read_text())
        with store.connect() as db:
            row = db.execute("SELECT metadata FROM batches WHERE id=?", (batch,)).fetchone()
            metadata = json.loads(row[0])
        if not force and metadata.get("filter_config"):
            config = metadata["filter_config"]
        for state in store.papers(batch):
            if not force and state.get("keyword_filter") is not None:
                continue
            result = evaluate(state["paper"], config)
            # A new rule pass invalidates the earlier AI decision; manual choices persist.
            store.update(batch, state["paper"]["id"], keyword_filter=result, ai=None, ai_error=None)
        with store.connect() as db:
            metadata["filter_config"] = config
            db.execute("UPDATE batches SET metadata=? WHERE id=?", (json.dumps(metadata), batch))

    def start_job(action, batch=None):
        if not job_lock.acquire(blocking=False):
            raise ValueError("已有任务在运行，请等待完成。")
        job = {
            "id": str(uuid.uuid4()),
            "action": action,
            "batch": batch,
            "status": "queued",
            "message": "等待开始",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "errors": [],
        }
        store.save_job(job)

        def progress(message):
            job["message"] = message
            store.save_job(job)

        def run():
            try:
                job["status"] = "running"
                progress("开始任务")
                if action == "collect":
                    date, papers, manifest = collect(directory / "raw", progress)
                    job["batch"] = date
                    store.save_batch(
                        date, papers, {"date": date, "manifest": manifest, "source": "arxiv"}
                    )
                    filter_batch(date, force=False)
                    progress(f"全量采集完成：{date}，{len(papers)} 篇新论文；关键词过滤已完成。")
                elif action == "filter":
                    filter_batch(batch)
                    progress("关键词过滤完成；可继续运行 AI 筛选。")
                elif action == "ai":
                    config = json.loads(config_path.read_text())
                    prompt = (ROOT / config["ai_filter"]["prompt_file"]).read_text()
                    states = [
                        s
                        for s in store.papers(batch)
                        if s.get("keyword_filter", {}).get("passed") and not s.get("ai")
                    ]
                    for index, state in enumerate(states):
                        paper = state["paper"]
                        progress(f"AI 筛选 {index + 1}/{len(states)}：{paper['title'][:65]}")
                        try:
                            result = validate_filter(
                                model.complete(prompt, json.dumps(paper, ensure_ascii=False), True),
                                paper,
                            )
                            result["model"] = model.model
                            store.update(batch, paper["id"], ai=result, ai_error=None)
                        except Exception as error:
                            message = safe_error(error)
                            store.update(batch, paper["id"], ai_error=message)
                            job["errors"].append({"id": paper["id"], "error": message})
                    progress(
                        f"AI 筛选结束，处理 {len(states)} 篇，失败 {len(job['errors'])} 篇；可重试失败论文。"
                    )
                elif action == "generate":
                    states = [
                        s
                        for s in store.papers(batch)
                        if s["review"] == "selected" and not s.get("article")
                    ]
                    for index, state in enumerate(states):
                        paper = state["paper"]
                        prefix = f"文章 {index + 1}/{len(states)} · {paper['id']}："
                        try:
                            article = generate(
                                paper,
                                state.get("angle", ""),
                                model,
                                directory / "articles" / batch,
                                lambda message: progress(prefix + message),
                                team_context=state.get("team_context"),
                                include_summary=state.get("include_summary", True),
                            )
                            store.update(batch, paper["id"], article=article, article_error=None)
                        except Exception as error:
                            message = safe_error(error)
                            store.update(batch, paper["id"], article_error=message)
                            job["errors"].append({"id": paper["id"], "error": message})
                    progress(f"文章生成结束，处理 {len(states)} 篇，失败 {len(job['errors'])} 篇。")
                job["status"] = "error" if job["errors"] else "done"
            except Exception as error:
                job.update(status="error", message=safe_error(error))
            finally:
                store.save_job(job)
                job_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return job["id"]

    def safe_error(error):
        # Never expose model credentials or third-party response bodies to the browser.
        return (
            str(error)
            if isinstance(error, (ValueError, RuntimeError))
            else "任务失败，请检查网络或输入后重试。"
        )

    @app.before_request
    def same_origin():
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="仅接受同源页面请求。"), 403
            if not request.is_json:
                return jsonify(error="请提交 JSON。"), 415

    @app.errorhandler(ValueError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/status")
    def status():
        batches = store.batches()
        for batch in batches:
            states = store.papers(batch["id"])
            batch["counts"] = {
                "total": len(states),
                "keywords": sum(bool(s.get("keyword_filter", {}).get("passed")) for s in states),
                "ai_processed": sum(bool(s.get("ai")) for s in states),
                "ai_keep": sum(
                    s.get("ai", {}).get("decision") in ("keep", "uncertain")
                    for s in states
                    if s.get("ai")
                ),
                "selected": sum(s["review"] == "selected" for s in states),
                "articles": sum(bool(s.get("article")) for s in states),
            }
        return jsonify(
            batches=batches,
            jobs=store.jobs(),
            llm_configured=model.configured,
            model=model.model if model.configured else None,
        )

    @app.get("/api/papers")
    def papers():
        batch = request.args.get("batch", "")
        states = store.papers(batch)
        for state in states:
            if state.get("article"):
                # Text and evidence are loaded only when the article is opened.
                state["article"] = {"model": state["article"]["model"]}
        return jsonify(papers=states)

    @app.post("/api/jobs")
    def jobs():
        body = request.get_json()
        action, batch = body.get("action"), body.get("batch")
        if action not in ("collect", "filter", "ai", "generate"):
            raise ValueError("未知任务。")
        if action != "collect" and batch not in [b["id"] for b in store.batches()]:
            raise ValueError("请先采集论文或选择已有批次。")
        if action in ("ai", "generate") and not model.configured:
            raise ValueError("请先配置 .env 中的模型服务，再重启应用。")
        if action == "generate" and not any(
            s["review"] == "selected" and not s.get("article") for s in store.papers(batch)
        ):
            raise ValueError("请先人工选中至少一篇尚未生成文章的论文。")
        return jsonify(job_id=start_job(action, batch)), 202

    @app.post("/api/review")
    def review():
        body = request.get_json()
        if body.get("review") not in ("pending", "selected", "skipped", "later"):
            raise ValueError("无效的人工筛选状态。")
        angle = body.get("angle", "")
        if not isinstance(angle, str) or len(angle) > 3000:
            raise ValueError("写作角度最多 3000 字符。")
        editorial = {}
        if "team_context" in body:
            editorial["team_context"] = validate_team_context(body["team_context"])
        if "include_summary" in body:
            if not isinstance(body["include_summary"], bool):
                raise ValueError("include_summary 必须为布尔值。")
            editorial["include_summary"] = body["include_summary"]
        store.update(
            body.get("batch"), body.get("id"), review=body["review"], angle=angle, **editorial
        )
        return jsonify(ok=True)

    @app.get("/api/article")
    def article():
        state = store.get(request.args.get("batch"), request.args.get("id"))
        if not state.get("article"):
            raise ValueError("文章尚未生成。")
        return jsonify(article=state["article"])

    @app.get("/reports/<path:filename>")
    def report(filename):
        return send_from_directory(ROOT / "reports", filename)

    @app.get("/api/article/preview")
    def article_preview():
        state = store.get(request.args.get("batch"), request.args.get("id"))
        if not state.get("article"):
            raise ValueError("文章尚未生成。")
        query = {"batch": request.args.get("batch"), "id": request.args.get("id")}
        download = "/api/export?" + urlencode(
            {"kind": "markdown", "batch": request.args.get("batch"), "id": request.args.get("id")}
        )
        article_dir = article_directory(state, query["batch"])
        notes_route = (
            "/api/article/review?"
            if (article_dir / "review.md").is_file()
            else "/api/article/evidence?"
        )
        notes = notes_route + urlencode(query)
        image_urls = {
            file: "/api/article/image?" + urlencode({**query, "name": Path(file).name})
            for file in available_images(state)
        }
        default_images_url = ("/api/article/images.zip?" + urlencode(query)) if image_urls else ""
        return preview(
            state["article"]["markdown"],
            download,
            notes,
            images_url=default_images_url,
            image_urls=image_urls,
        )

    def article_directory(state, batch):
        # Compute paths from application-owned IDs, never from the saved/model directory field.
        root = (directory / "articles").resolve()
        target = (root / batch / safe_id(state["paper"]["version_id"])).resolve()
        if not target.is_relative_to(root):
            raise ValueError("文章路径无效。")
        return target

    def available_images(state):
        return [
            f["file"]
            for f in state.get("article", {}).get("notes", {}).get("figures", [])
            if isinstance(f, dict)
            and isinstance(f.get("file"), str)
            and f["file"].startswith("figures/")
            and len(Path(f["file"]).parts) == 2
            and Path(f["file"]).suffix.lower() == ".png"
        ]

    @app.get("/api/article/image")
    def article_image():
        batch = request.args.get("batch")
        state = store.get(batch, request.args.get("id"))
        name = request.args.get("name", "")
        if not name or Path(name).name != name or "figures/" + name not in available_images(state):
            abort(404)
        return send_from_directory(article_directory(state, batch) / "figures", name)

    @app.get("/api/article/images.zip")
    def article_images_zip():
        batch = request.args.get("batch")
        state = store.get(batch, request.args.get("id"))
        if not state.get("article"):
            raise ValueError("文章尚未生成。")
        article_dir = article_directory(state, batch)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("article.md", state["article"]["markdown"])
            for file in dict.fromkeys(available_images(state)):
                path = article_dir / file
                if not path.is_file() or not path.resolve().is_relative_to(article_dir):
                    raise ValueError("文章配图文件缺失，请重新生成。")
                archive.write(path, file)
        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{safe_id(state['paper']['version_id'])}-images.zip",
        )

    @app.get("/api/article/review")
    def article_review():
        batch = request.args.get("batch")
        state = store.get(batch, request.args.get("id"))
        if not state.get("article"):
            raise ValueError("文章尚未生成。")
        return send_from_directory(
            article_directory(state, batch), "review.md", mimetype="text/plain"
        )

    @app.get("/api/article/evidence")
    def article_evidence():
        state = store.get(request.args.get("batch"), request.args.get("id"))
        if not state.get("article"):
            raise ValueError("文章尚未生成。")
        return jsonify(notes=state["article"].get("notes", []))

    @app.get("/api/export")
    def export():
        batch = request.args.get("batch")
        if batch not in [b["id"] for b in store.batches()]:
            raise ValueError("找不到批次。")
        kind = request.args.get("kind", "json")
        if kind == "markdown":
            state = store.get(batch, request.args.get("id"))
            if not state.get("article"):
                raise ValueError("文章尚未生成。")
            content = state["article"]["markdown"]
            mime, name = "text/markdown", f"{state['paper']['id'].replace('/', '_')}.md"
        elif kind == "json":
            content = json.dumps(
                {
                    "batch": next(b for b in store.batches() if b["id"] == batch),
                    "papers": store.papers(batch),
                },
                ensure_ascii=False,
                indent=2,
            )
            mime, name = "application/json", f"{batch}.json"
        else:
            raise ValueError("未知导出格式。")
        return send_file(
            io.BytesIO(content.encode("utf-8")),
            mimetype=mime,
            as_attachment=True,
            download_name=name,
        )

    return app


if __name__ == "__main__":
    create_app().run(
        host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), threaded=True, debug=False
    )
