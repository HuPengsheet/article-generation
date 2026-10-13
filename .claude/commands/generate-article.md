---
description: >
  从 URL、PDF 或本地文件生成中文技术文章。当用户提供论文链接并要求生成文章、写技术解读、
  技术分析时使用此 skill。支持 arXiv、PDF、HTML、Markdown 来源。
allowed-tools: Bash, Read, Write, Glob
---

# 文章生成 Skill

你将扮演 LLM，按步骤完成技术文章生成流水线。非 LLM 部分（材料下载提取、校验、保存）由 Python 脚本 `scripts/skill_step.py` 处理。

## 输入解析

用户输入格式：`$ARGUMENTS`

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

1. 用 Read 工具读取 `<WORK_DIR>/current/meta.json`，获取当前步骤名和输出格式（json 或 markdown）
2. 用 Read 工具读取 `<WORK_DIR>/current/system.md`，这是你的任务指令
3. 用 Read 工具读取 `<WORK_DIR>/current/user.json`，这是你的输入数据

4. **严格按 system.md 的指令生成响应**：
   - 如果 format 是 `json`：输出**纯 JSON**，不要包含任何 markdown 代码围栏或其他文字
   - 如果 format 是 `markdown`：输出**纯 Markdown 正文**，以 `# 标题` 开头
   - 仔细阅读 system.md 中的所有要求，包括引用格式 `[S1:B2]`、图片格式、字数要求等

5. 将响应写入对应文件：
   - JSON 格式 → 用 Write 工具写入 `<WORK_DIR>/current/response.json`
   - Markdown 格式 → 用 Write 工具写入 `<WORK_DIR>/current/response.md`

6. 运行推进命令：
   ```bash
   python scripts/skill_step.py next "<WORK_DIR>"
   ```

7. 检查输出：
   - 如果输出以 `DONE:` 开头 → 退出循环，进入第 3 步
   - 如果输出以 `READY:` 开头 → 回到步骤 1 继续
   - 如果输出以 `ERROR:` 开头 → 阅读错误信息，修正响应后重新写入 response 文件，再次运行 next（最多重试 2 次）

### 第 3 步：完成

1. `DONE:` 后面是文章目录路径
2. 读取该目录下的 `article.md` 的前几行，提取标题
3. 告知用户：
   - 文章已生成
   - 文章标题
   - 文件路径：`<目录>/article.md`（公开版）、`<目录>/article-cited.md`（带引用）
   - 预览：`<目录>/wechat.html`
   - 核对材料：`<目录>/review.md`

## 重要注意事项

- **evidence 步骤**可能有多个 chunk（见 meta.json 的 chunk/total_chunks），每个 chunk 都是独立的一轮生成
- **user.json 可能很大**（40KB+），始终用 Read 工具读取，不要用 cat
- **JSON 响应必须是纯 JSON**，不要包裹在 markdown 代码块中
- 所有来源事实引用格式为 `[S1:B2]`，不要使用 `[PDF p.N]` 格式
- 图片路径只能使用 user.json 中 figures 清单里的路径
- 生成文章时遵循 system.md 中的字数要求（brief/detailed/deep-dive）
