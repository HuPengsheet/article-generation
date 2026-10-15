---
name: generate-article
description: >
  从 URL、PDF 或本地文件生成中文技术文章。当用户提供论文链接并要求生成文章、写技术解读、
  技术分析时使用此 skill。支持 arXiv、PDF、HTML、Markdown 来源。
allowed-tools: Bash, Read, Write, Glob, Agent
---

# 文章生成 Skill

你将扮演 LLM，按步骤完成技术文章生成流水线。非 LLM 部分（材料下载提取、校验、保存）由 Python 脚本 `scripts/skill_step.py` 处理。

本 skill 同时用于 Claude Code（`.claude/skills/generate-article` 链接到这里）和 Codex（`.agents/skills/generate-article`）。所有命令在仓库根目录执行。

## 输入解析

用户输入：`$ARGUMENTS`（Claude Code 会替换为命令参数；如果这里仍是字面量 `$ARGUMENTS`，就从用户的消息中提取）

从中提取：
- **source**：URL 或文件路径（必需，第一个参数）
- **style**：风格名称（用户可能说"用 XXX 风格"，提取 XXX）
- **angle**：写作角度（用户可能说"从 XXX 角度"）
- **audience**：目标读者（默认"工程师"）
- **depth**：brief / detailed / deep-dive（默认 detailed）
- **no-summary**：用户说"不要总结"时加 `--no-summary`

## 执行流程

### 第 1 步：初始化

生成工作目录名并运行 init：

```bash
WORK_DIR="data/articles/$(date +%Y%m%d-%H%M%S)-$(head -c4 /dev/urandom | xxd -p)"
python scripts/skill_step.py init "<source>" "$WORK_DIR" [--style NAME] [--angle TEXT] [--audience TEXT] [--depth LEVEL] [--no-summary]
```

如果 init 失败，告知用户错误原因并停止。

### 第 2 步：循环生成

重复以下步骤直到收到 `DONE:` 输出：

1. 用 Read 工具读取 `<WORK_DIR>/current/meta.json`，获取当前步骤名、输出格式（json 或 markdown）和 `context_files`
2. 用 Read 工具读取 `<WORK_DIR>/current/system.md`，这是你的任务指令
3. 用 Read 工具读取 `<WORK_DIR>/current/user.json`，这是你的输入数据
4. **逐个完整读取 `context_files` 列出的文件**（同在 `current/` 下），它们是 user.json 里标注「见 current/xxx」的字段内容：
   - `sources.md`：原文正文块，讲机制、补细节时以它为准
   - `references.md`：已发表的完整范文，动笔前通读，模仿其结构、图文节奏、图注和语气
   - `article.md`：review / rewrite 步骤中待处理的文章
   文件较长时用 offset/limit 分段读完，不要只读开头

5. **严格按 system.md 的指令生成响应**：
   - 如果 format 是 `json`：输出**纯 JSON**，不要包含任何 markdown 代码围栏或其他文字
   - 如果 format 是 `markdown`：输出**纯 Markdown 正文**，以 `# 标题` 开头
   - 仔细阅读 system.md 中的所有要求，包括引用格式 `[S1:B2]`、图片格式、字数要求等

6. 将响应写入对应文件：
   - JSON 格式 → 用 Write 工具写入 `<WORK_DIR>/current/response.json`
   - Markdown 格式 → 用 Write 工具写入 `<WORK_DIR>/current/response.md`

7. 运行推进命令：
   ```bash
   python scripts/skill_step.py next "<WORK_DIR>"
   ```

8. 检查输出：
   - 如果输出以 `DONE:` 开头 → 退出循环，进入第 3 步
   - 如果输出以 `READY:` 开头 → 回到步骤 1 继续
   - 如果输出以 `CRITIQUE:` 开头 → 执行下面的「独立审稿」，完成后再次运行 next
   - 如果输出以 `ERROR:` 开头 → 阅读错误信息，修正响应后重新写入 response 文件，再次运行 next。常见原因：大纲选图不够、正文漏放大纲选定的图、正文字数低于 meta.json 的 `min_chars`、改写稿比原稿短。修正时补写内容，不要删减。连续 3 次未达标时脚本会放宽篇幅和配图要求并记录到 review.md

### 独立审稿（子 Agent）

第一轮自查和改写完成后，脚本输出 `CRITIQUE: <目录>`，并在该目录（即 `current/critic/`，`meta.json` 的 `critic_dir`）准备一个自包含的审稿任务：`task.md` 是审稿指令（整体质量、逻辑与一致性、AI 味），同目录下有文章、原文、范文和 `user.json`，审稿意见写入同目录的 `response.json`。

**审稿必须交给一个子 Agent，等它完成后再继续。** 主 Agent 写了这篇文章，自己审稿会带着写作时的判断。

- **Claude Code**：用 Agent 工具，`subagent_type` 为 `article-critic`，prompt 只写：`审稿任务目录：<critic_dir>。阅读其中的 task.md 并按要求完成。`
- **Codex**：启动一个 `article_critic` 子 agent，只告诉它：`审稿任务目录：<critic_dir>。阅读其中的 task.md 并按要求完成。` 等待它完成。
- 不要把文章内容、你自己的看法或修改建议写进子 Agent 的 prompt。

子 Agent 完成后运行 `python scripts/skill_step.py next "<WORK_DIR>"`：
- 输出 `ERROR:` 时，把错误信息发给同一个子 Agent（或重新启动一个），让它重写 `response.json`。
- 有 high 或 medium 级别问题时，输出 `READY:` 进入 `revise` 步骤：按 system.md 和 `user.json` 里的 `critique` 做第二轮修改，流程与其他 markdown 步骤相同。
- 没有需要处理的问题时直接输出 `DONE:`。

当前环境无法启动子 Agent 时（例如 Codex 关闭了 `agents.enabled`），自己完成审稿任务，并告知用户这次审稿不是独立进行的。

### 第 3 步：完成

1. `DONE:` 后面是文章目录路径
2. 读取该目录下的 `article.md` 的前几行，提取标题
3. 告知用户：
   - 文章已生成
   - 文章标题
   - 文件路径：`<目录>/article.md`（公开版）、`<目录>/article-cited.md`（带引用）
   - 预览：`<目录>/wechat.html`
   - 核对材料：`<目录>/review.md`（含审稿评分和意见）
   - 审稿评分，以及第二轮修改是否已应用

## 重要注意事项

- **evidence 步骤**可能有多个 chunk（见 meta.json 的 chunk/total_chunks），每个 chunk 都是独立的一轮生成
- **user.json 可能很大**（100KB+），始终用 Read 工具分段读取，不要用 cat
- **配图和篇幅是硬性要求**：大纲需按 targets.min_figures 分配原图；正文需放入大纲选定的全部图片，正文字数不少于 targets.min_chars
- 分析直接写在行文里，不要写「**分析**：」标签
- **JSON 响应必须是纯 JSON**，不要包裹在 markdown 代码块中
- 所有来源事实引用格式为 `[S1:B2]`，不要使用 `[PDF p.N]` 格式
- 图片路径只能使用 user.json 中 figures 清单里的路径
