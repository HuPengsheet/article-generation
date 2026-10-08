import json
import sqlite3

from scripts.migrate_report_articles import migrate


def test_legacy_article_migrates_assets_review_and_preserves_selection(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    report = tmp_path / "reports/paper"
    (report / "images").mkdir(parents=True)
    (report / "images/a.png").write_bytes(b"png-content")
    (report / "paper-notes.md").write_text("原有核对材料")
    state = {
        "review": "selected",
        "ai": {"decision": "keep"},
        "paper": {"version_id": "2610.00001v1"},
        "article": {
            "markdown": "# 标题\n\n![图](images/a.png)",
            "notes": {"figures": [{"file": "images/a.png"}]},
            "image_base_url": "/reports/paper/",
            "images_url": "/reports/paper/images.zip",
            "notes_url": "/reports/paper/paper-notes.md",
        },
    }
    with sqlite3.connect(data / "app.sqlite3") as db:
        db.execute("CREATE TABLE papers(batch TEXT,id TEXT,data TEXT)")
        db.execute(
            "INSERT INTO papers VALUES(?,?,?)", ("2026-10-07", "2610.00001", json.dumps(state))
        )
    assert migrate(data, tmp_path / "reports") == 1
    assert migrate(data, tmp_path / "reports") == 0
    target = data / "articles/2026-10-07/2610.00001v1"
    assert (target / "figures/a.png").read_bytes() == b"png-content"
    assert (target / "review.md").read_text() == "原有核对材料"
    with sqlite3.connect(data / "app.sqlite3") as db:
        migrated = json.loads(db.execute("SELECT data FROM papers").fetchone()[0])
    assert migrated["review"] == "selected" and migrated["ai"] == state["ai"]
    assert "image_base_url" not in migrated["article"]
    assert "](figures/a.png)" in migrated["article"]["markdown"]
