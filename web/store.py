import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS papers (
                    batch TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(batch, id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
            """)
            for row in db.execute("SELECT id, data FROM jobs").fetchall():
                job = json.loads(row[1])
                if job["status"] in ("queued", "running"):
                    job.update(status="error", message="服务重启导致任务中断，请重试。")
                    db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), row[0]))

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def save_batch(self, batch, papers, metadata):
        with self.connect() as db:
            previous = db.execute("SELECT metadata FROM batches WHERE id=?", (batch,)).fetchone()
            if previous:
                metadata = {**json.loads(previous[0]), **metadata}
            db.execute(
                "INSERT OR REPLACE INTO batches VALUES (?, ?)", (batch, json.dumps(metadata))
            )
            for paper in papers:
                # Re-collection is idempotent and preserves AI and human review data.
                row = db.execute(
                    "SELECT data FROM papers WHERE batch=? AND id=?", (batch, paper["id"])
                ).fetchone()
                state = json.loads(row[0]) if row else {"review": "pending", "angle": ""}
                state.update(paper=paper)
                db.execute(
                    "INSERT OR REPLACE INTO papers VALUES (?, ?, ?)",
                    (batch, paper["id"], json.dumps(state)),
                )

    def batches(self):
        with self.connect() as db:
            return [
                {"id": row[0], **json.loads(row[1])}
                for row in db.execute("SELECT id, metadata FROM batches ORDER BY id DESC")
            ]

    def papers(self, batch):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM papers WHERE batch=? ORDER BY id", (batch,))
            ]

    def get(self, batch, paper_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT data FROM papers WHERE batch=? AND id=?", (batch, paper_id)
            ).fetchone()
        if not row:
            raise ValueError("找不到论文。")
        return json.loads(row[0])

    def update(self, batch, paper_id, **changes):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM papers WHERE batch=? AND id=?", (batch, paper_id)
            ).fetchone()
            if not row:
                raise ValueError("找不到论文。")
            state = json.loads(row[0])
            state.update(changes)
            db.execute(
                "UPDATE papers SET data=? WHERE batch=? AND id=?",
                (json.dumps(state), batch, paper_id),
            )

    def save_job(self, job):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job)))

    def jobs(self):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM jobs ORDER BY rowid DESC LIMIT 20")
            ]
