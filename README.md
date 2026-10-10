# article-generation

两个独立工具：**arXiv 每日采集与筛选**、**基于多种资料的中文技术文章生成**。通过 JSON 文件或链接衔接，不依赖彼此的代码或数据库。原有 Web 工作台作为可选界面保留。

## 安装与配置

使用 Conda `py313`（Python 3.13）：

```bash
conda activate py313
python -m pip install -r requirements.txt
cp .env.example .env
```

仅采集和关键词筛选无需模型。AI 筛选和文章生成需要在本地 `.env` 配置支持 Chat Completions 与 JSON 对象输出的兼容接口：

```dotenv
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=你的本地密钥
LLM_MODEL=你的模型名
```

程序在服务地址后追加 `/chat/completions`，密钥仅在本地使用。CLI 每次运行读取项目 `.env`，Web 修改配置后需重启。没有默认付费模型。

## 工具一：arXiv 采集与筛选

一条命令采集最新公告日的全部新论文，并完成关键词筛选：

```bash
python -m arxiv_tool --skip-ai
```

启用 AI 筛选，以及可选的人工交互选择：

```bash
python -m arxiv_tool
python -m arxiv_tool --interactive
python -m arxiv_tool --skip-ai --interactive
```

`--config` 指定关键词配置，默认 `config/ai_infra.json`；`--output-dir` 指定输出根目录，默认 `data/arxiv`；`--date YYYY-MM-DD` 重用指定公告日缓存。RSS 无法恢复任意历史日，完整缓存可以离线重跑。

输出：

```text
 data/arxiv/<公告日>/
 ├── papers.json          全量新论文
 ├── filtered.json        全量关键词判定，包含未通过记录
 ├── ai-reviewed.json     关键词通过项的 AI 判定或错误
 ├── human-review.json    候选的人工状态
 ├── selected.json        候选，或开启交互后人工选中的记录
 ├── selected.md          英文标题、中文译名、arXiv 链接
 ├── filter-config.json   本次关键词配置快照
 ├── manifest.json        全量采集完整性与来源校验
 └── *.xml / taxonomy.html 原始 RSS 与分类缓存
```

同日重跑按论文 ID 保留已有人工状态，交互时输入空行保留该状态；跳过或稍后看的记录不自动重新选中。

未开启人工交互时，候选的 `review` 为 `pending`，不冒充人工已选中。AI 的 `keep` 与 `uncertain` 均进入候选；`reject` 和失败项仍保存在核对记录中。AI 部分失败会导出成功项并以非零状态退出。

中文标题由 AI 筛选一并翻译；`--skip-ai` 或模型未提供译名时清单明确标记“未翻译”。不会仅为关键词采集偷偷调用翻译服务。

### 采集范围与筛选规则

按 RSS 公告日期采集，而非北京时间自然日或投稿日。从官方分类目录动态发现全部活跃主档案，采集前不做 AI Infra 过滤。仅纳入 `announce_type=new`，更新与交叉分类不作为新论文；按论文 ID 去重。

所有来源须属于同一天，才返回完整批次。单个 feed 达到 2000 条时改用子分类；子分类仍达到上限则报错，不宣称完整。请求间隔至少 3 秒，失败重试，缓存已成功响应。公告日可能早于本地今天。

关键词规则匹配标题、摘要和可选关键词：强相关词命中即可通过，宽泛词还需命中 AI 上下文词。统一大小写、空格和连字符，按完整词/短语匹配。配置在 `config/ai_infra.json`，AI 提示词在 `prompts/ai_infra_filter.md`。AI 的引文须能匹配标题或摘要；相关性分数不代表论文质量。

仍可独立筛选已有 JSON 数组：

```bash
python -m arxiv_tool.keyword_filter daily_papers.json --output filtered_papers.json
# 旧命令兼容
python scripts/filter_papers.py daily_papers.json --output filtered_papers.json
```

采集规则依据：[RSS 文档](https://info.arxiv.org/help/rss.html)、[公告类型说明](https://info.arxiv.org/help/rss_specifications.html)、[分类目录](https://arxiv.org/category_taxonomy)。

## 工具二：通用文章生成

本地文件与链接共用入口，不需要先采集、入库或人工选中：

```bash
python -m generator paper.pdf
python -m generator article.html
python -m generator notes.md
python -m generator "https://example.com/article"
python -m generator "https://arxiv.org/abs/2501.12345v1"
```

多份材料默认合成一篇文章：

```bash
python -m generator paper.pdf supplementary.html notes.md \
  --angle "比较实现机制与性能取舍" \
  --audience "系统工程师" --depth deep-dive
```

可选参数：`--depth brief|detailed|deep-dive`（默认 detailed）、`--audience`（默认工程师）、`--angle`、`--no-summary`、`--team-context 背景.json`、`--output-dir`（默认 `data/articles`）。篇幅受材料信息量限制，不为满足长度编造内容。

### 两个工具通过文件衔接

```bash
# 把所有选中资料合成一篇
python -m generator data/arxiv/<公告日>/selected.json --angle "AI Infra"

# 每个选中链接分别生成一篇
python -m generator data/arxiv/<公告日>/selected.json --batch
```

JSON 输入须为数组，元素可以是路径/链接字符串、`{"url":"..."}`、`{"path":"..."}` 或采集导出的 `{"paper":{...},"review":"...","ai":{...}}`。相对路径以 JSON 文件所在目录为基准。`skipped`、`later` 与 AI `reject` 项跳过。普通 Markdown 当作写作资料解析，不会自动抓取其中所有链接；论文清单衔接使用 `selected.json`。

每次运行创建独立目录，避免覆盖已有文章。批量模式一篇失败后继续处理其他篇，错误保存为 `failures.json`，最终以非零状态退出。

### 材料解析与证据

| 材料 | 提取内容 | 原始定位 |
| --- | --- | --- |
| PDF | 逐页文字，嵌入位图 | 页码 |
| HTML | 优先 article/main，其他页面用 readability 估计正文；保留标题、表格、代码、图片与图注 | 章节、正文段落 |
| Markdown | 标题层级、正文、表格、代码块与图片 | 原文件行号 |
| URL | 下载后依据实际内容、响应类型判断格式，再委派解析 | 最终链接与上述定位 |
| arXiv 链接 | 固定版本全文 PDF，LaTeX 源码与官方 HTML 配图 | 版本、页码、原图编号 |

内部引用使用 `[S1:B2]`，分别表示材料和正文块。两份 PDF 的第一页不会混为同一个证据位置。事实笔记的引文必须逐字匹配对应正文块；大纲和正文只能使用已有事实位置、图片清单。发布正文删除内部标记，带引用稿、原文摘录与来源映射单独保存。

流程为：提取 → 校验证据笔记 → 大纲 → 草稿 → 自查 → 必要时改写一次。自查/改写同时收到每节目标、承接逻辑及对应原文引文。改写校验失败保留已校验初稿并记录原因。风格检查覆盖套话、重复段首、列表占比、泛化启发章节和缺少量化条件的性能表述，属于提示，不能替代人工审核。

文章默认有简短总结，`--no-summary` 要求不写总结；不设置泛化“对 AI Infra 的启发”章节。团队背景只使用调用者已核实资料，不从作者名单推断声望。

### 配图与解析边界

HTML/Markdown 的相对图片地址按原网页或本地文件位置解析，支持图片下载及规范化为 PNG。配图失败会记录局限。独立 PDF 仅提取嵌入位图，不能自动恢复所有矢量图、图注或多面板关系；扫描 PDF 未接入 OCR，可读取文字不足时停止生成。

arXiv 专用适配器复用已有安全源码解包、递归 input/include、figure/wrapfigure、分图图注与多面板合并。不会执行 TeX；拒绝越界路径、链接与特殊文件。源码缺失或宏绘图无法提取时，固定版本官方 HTML 补全配图，保留原编号和分图说明。特殊宏或布局仍需人工核对。

网页只读取静态 HTML，不执行 JavaScript、不处理登录或付费访问。无法取得正文时报告失败，未知响应不会当作文章材料。材料和图片有下载大小限制。来源定位校验不保证模型解读正确，发表前仍需核对原文与原图。

### 输出与公众号预览

```text
 data/articles/<本次运行>/
 ├── article.md           干净的发布正文
 ├── article-cited.md     内部引用稿
 ├── draft.md / outline.json
 ├── materials.json       材料来源与有序正文块
 ├── notes.json / review.md 核对依据、自查与解析局限
 ├── figures/             统一 PNG 素材
 ├── images.zip           正文及配图
 ├── wechat.html          行内样式公众号预览
 └── sources/             各材料原始文件及提取素材
```

为了使用浏览器的图文复制，在输出根目录启动静态服务：

```bash
python -m http.server 8001 --bind 127.0.0.1 --directory data/articles
```

打开 `http://127.0.0.1:8001/<本次运行>/wechat.html`，复制正文粘贴到公众号，标题单独复制。批量文章在各自 `article-NNN/` 下。实际公众号可能调整图片或样式，可下载图片包手动上传。

Python 调用：

```python
from pathlib import Path
from shared.llm import LLM
from generator.sources import detect
from generator.pipeline import generate

work = Path("data/articles/my-article")
material = detect("notes.md").extract("notes.md", work / "sources/S1")
result = generate([material], LLM(), work, angle="机制与取舍", audience="工程师")
print(result.markdown)
```

编程调用前需自行加载 `.env` 或配置环境变量。`generate()` 可接收 `illustrations=[{"path":"diagram.png","caption":"自绘示意","assumptions":"机制示意，非实验结果"}]`；自绘图须有实际 PNG 和说明，不自动调用图片生成服务。

团队 JSON 为 `{"verified":true,"description":"经核实的团队背景","sources":[{"title":"机构页面","url":"https://..."}]}`。程序校验格式，不自动证明团队声望或背景真实性。

## 可选 Web 工作台

```bash
python -m web.app
# 旧启动命令仍可用
python app.py
```

打开 <http://127.0.0.1:8000>，端口可在 `.env` 的 `PORT` 修改。保留采集、AI 筛选、人工选择、生成选中文章与导出的界面；Web 调用两个工具的 Python API，SQLite 和任务队列只属于 Web 层。CLI 不读取或修改 `data/app.sqlite3`。

已有批次、人工选择和文章保留；已有文章不会自动覆盖。Web 重启把未完成任务标为中断，可重试。旧报告迁移仍可运行 `python -m scripts.migrate_report_articles`。通用文件/多材料输入目前使用 CLI 或 Python API，网页暂未增加上传界面。

## 代码结构

```text
arxiv_tool/       采集、关键词筛选、AI 筛选、交互与导出 CLI
 generator/
   sources/      Source 注册与 PDF/HTML/Markdown/URL 解析
   figures/      LaTeX、官方 HTML、本地/网页配图
   evidence.py   材料合并、来源编号、引文校验
   writer.py     写作提示词、引用和风格校验
   pipeline.py   通用生成流程与 Web 适配器
   output.py     Markdown、证据材料、预览和 ZIP
 shared/         模型客户端、HTTP、公众号渲染
 web/            可选 Flask 工作台、SQLite、static/
 config/         可配置关键词
 prompts/        AI 筛选要求
 tests/          独立工具和集成测试
 pipeline/       旧模块的兼容导入层
```

旧 PDF 编程入口由 `generator/legacy.py` 兼容，新的 CLI 和 Web 使用通用生成流程。添加资料格式只需实现 `Source.extract()` 并通过 `generator.sources.register(predicate, factory)` 注册，不修改写作骨架。

## 验证与 GitHub 上传

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
node --check web/static/app.js
python -m arxiv_tool --help
python -m generator --help
```

测试使用受控模型，不代表已调用真实模型服务。覆盖采集完整性、AI 摘要证据、四类材料、跨来源证据、源码与配图、改写回退、CLI 文件衔接、批量失败继续、Web 兼容及图文导出。

运行服务后，可检查图片、图注、图文复制、普通富文本粘贴和手机排版：

```bash
python -m scripts.check_preview --url "你的本地文章预览地址"
```

脚本使用本机 Chrome，或通过 `--browser-path` 指定 Chromium；无浏览器可运行 `python -m playwright install chromium`。不写截图或临时数据库，公众号后台仍需人工验证。

`data/`、`reports/`、本地 `.env` 及变体、数据库、缓存均被 Git 忽略，保留 `.env.example`。上传前用 `git status --short` 和 `git add --dry-run .` 检查。仓库包含代码、配置模板、提示词、测试与文档，不包含采集论文、人工记录、生成文章或密钥。本地数据无需删除。
