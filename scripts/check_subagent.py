#!/usr/bin/env python3
"""Check whether a run's CRITIQUE step was handled by a host subagent.

Usage: python scripts/check_subagent.py <WORK_DIR>

The critic task directory path is unique per run, and the skill passes exactly that
path to the subagent. So the subagent's own transcript must contain it:
  - Claude Code: ~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl,
    with agentType in the sibling .meta.json
  - Codex: ~/.codex/sessions/**/rollout-*.jsonl whose first line has
    thread_source=subagent (the parent's spawn_agent message is encrypted)
A main agent that reviewed by itself leaves the path only in its own transcript.
"""

import json
import sys
from pathlib import Path


def claude_hits(needle):
    hits = []
    for transcript in Path.home().glob(".claude/projects/*/*/subagents/agent-*.jsonl"):
        if needle in transcript.read_text(encoding="utf-8", errors="ignore"):
            meta = transcript.with_suffix(".meta.json")
            agent_type = (
                json.loads(meta.read_text(encoding="utf-8")).get("agentType", "?")
                if meta.is_file()
                else "?"
            )
            hits.append(f"{agent_type}  {transcript}")
    return hits


def codex_hits(needle):
    """Codex encrypts the spawn_agent message in the parent rollout, so look for the
    subagent's own rollout: its first line marks thread_source=subagent."""
    hits = []
    for rollout in Path.home().glob(".codex/sessions/**/rollout-*.jsonl"):
        text = rollout.read_text(encoding="utf-8", errors="ignore")
        if needle not in text:
            continue
        meta = json.loads(text.split("\n", 1)[0]).get("payload") or {}
        if meta.get("thread_source") == "subagent":
            hits.append(f"{meta.get('agent_role', '?')}  {rollout}")
    return hits


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    work_dir = Path(argv[1]).resolve()
    needle = str(work_dir / "current" / "critic")
    if not (work_dir / "critique.json").is_file():
        print(f"该目录还没有完成审稿步骤（缺少 critique.json）：{work_dir}")
        return 1
    claude, codex = claude_hits(needle), codex_hits(needle)
    for label, hits in (("Claude Code 子 Agent", claude), ("Codex 子 agent", codex)):
        print(f"{label}：{len(hits)} 条")
        for hit in hits:
            print(f"  {hit}")
    if claude or codex:
        print("结论：审稿任务交给了子 Agent。")
        return 0
    print("结论：没有找到子 Agent 处理该审稿任务的记录，可能是主 Agent 自己审的。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
