# 重构方案：拆分为两个独立工具

## 目标

将现有项目拆分为两个独立工具，通过文件衔接，彼此可单独使用：

1. **arXiv 采集与筛选工具** — 纯 CLI，一键采集、关键词筛选、AI 筛选、人工选择、导出
2. **通用文章生成工具** — 支持 PDF / HTML / Markdown / URL，不依赖 arXiv 采集结果

现有 Web 工作台保留为可选界面。爬取数据、生成文章和本地密钥继续排除在 Git 之外。

---

## 目录结构

```
article-generation/
├── arxiv_tool/                  # 工具 1：arXiv 采集与筛选（纯 CLI）
│   ├── __init__.py
│   ├── cli.py                   # 入口，一键运行
│   ├── collector.py             # RSS 全量采集（← pipeline/arxiv.py）
│   ├── keyword_filter.py        # 可配置关键词两级筛选（← scripts/filter_papers.py）
│   ├── ai_filter.py             # LLM 筛选 + 证据校验（← app.py AI 筛选段）
│   └── export.py                # 导出 JSON / 筛选结果 / 中英标题 Markdown
│
├── generator/                   # 工具 2：通用文章生成（CLI + 可编程调用）
│   ├── __init__.py
│   ├── cli.py                   # 入口，接收文件/链接/角度等参数
│   ├── sources/                 # 材料提取（核心扩展点）
│   │   ├── __init__.py          # registry: detect(path_or_url) → Source
│   │   ├── base.py              # Source 协议 + Material 数据结构
│   │   ├── pdf.py               # PDF 文字提取（← articles.py extract_pages）
│   │   ├── html.py              # HTML readability 提取正文
│   │   ├── markdown.py          # Markdown 直接解析
│   │   └── url.py               # URL 抓取后按 content-type 委派到上述三种
│   ├── figures/                 # 配图提取（扩展点）
│   │   ├── __init__.py
│   │   ├── latex.py             # LaTeX 源码配图（← pipeline/figures.py）
│   │   ├── html_img.py          # HTML 页面图片提取（← pipeline/html_figures.py）
│   │   └── local.py             # 本地图片直接复制
│   ├── pipeline.py              # 组装：提取 → 笔记 → 大纲 → 写作 → 自查 → 改写
│   ├── evidence.py              # 笔记提取 + 事实校验（← articles.py notes/validate 部分）
│   ├── writer.py                # 大纲、草稿、审阅、改写（← pipeline/writing.py）
│   └── output.py                # 输出 Markdown + 配图包 + WeChat 预览
│
├── shared/                      # 两工具共享的基础设施
│   ├── __init__.py
│   ├── llm.py                   # LLM 客户端（← pipeline/llm.py）
│   └── wechat.py                # Markdown → 内联 HTML 预览（← pipeline/wechat.py）
│
├── web/                         # 可选 Web 界面（保留现有功能）
│   ├── app.py                   # Flask 薄壳，调用 arxiv_tool + generator 的 Python API
│   ├── store.py                 # SQLite 持久化（← pipeline/store.py）
│   └── static/                  # 前端（← static/）
│       ├── index.html
│       ├── app.js
│       └── style.css
│
├── config/
│   └── ai_infra.json            # 关键词筛选配置
├── prompts/
│   └── ai_infra_filter.md       # AI 筛选 prompt
├── tests/
├── .env.example
├── pyproject.toml
└── requirements.txt
```

---

## 关键设计

### 1. Source 协议 — 文章生成的扩展核心

所有材料类型实现同一个协议，pipeline 对具体类型无感知。添加新材料类型只需写一个 `Source` 子类并注册。

```python
# generator/sources/base.py
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExtractedText:
    """一个可引用的文本片段，对应 PDF 页 / HTML 段落 / Markdown 节"""

    ref: str  # 引用标记，如 "PDF p.3" 或 "§2.1"
    text: str
    heading: str = ""  # 所属标题层级


@dataclass
class Figure:
    file: str  # 相对路径 figures/xxx.png
    caption_en: str
    order: int
    origin: str = "source"  # latex / html / local / author_diagram
    panels: list = field(default_factory=list)


@dataclass
class Material:
    """统一的材料表示 — 所有 Source 的输出契约"""

    title: str
    authors: list[str]
    sections: list[ExtractedText]  # 有序正文片段
    figures: list[Figure]
    metadata: dict  # url, date, source_type 等
    limitations: list[str] = field(default_factory=list)


class Source:
    """协议：每种材料类型实现 extract()"""

    def extract(self, path_or_url: str, work_dir: Path) -> Material:
        raise NotImplementedError
```

各 Source 实现示例：

| Source 类 | 输入 | 提取逻辑 |
|---|---|---|
| `PdfSource` | 本地 `.pdf` 或 PDF URL | PyPDF 逐页提取 + LaTeX 源码配图 |
| `HtmlSource` | 本地 `.html` 或网页 URL | readability 提取正文 + `<img>` 下载 |
| `MarkdownSource` | 本地 `.md` | 解析标题层级 + 本地图片复制 |
| `UrlSource` | 任意 URL | 抓取后按 content-type 委派到上述三种 |

`sources/__init__.py` 中的 `detect()` 函数根据文件扩展名或 URL 自动选择 Source。

### 2. Generator Pipeline — 固定骨架，步骤可配

```python
# generator/pipeline.py
def generate(
    materials: list[Material],  # 支持多份材料合成一篇
    llm: LLM,
    work_dir: Path,
    *,
    angle: str = "",  # 写作角度
    audience: str = "工程师",  # 目标读者
    depth: str = "detailed",  # brief / detailed / deep-dive
    include_summary: bool = True,
    team_context: dict | None = None,
    progress: Callable = lambda m: None,
) -> ArticleResult:
    # 1. 合并多份材料的 sections、figures、limitations
    merged = merge_materials(materials)

    # 2. 分块提取笔记 → facts
    facts = extract_evidence(merged.sections, llm, progress)

    # 3. 生成大纲
    outline = design_outline(merged, facts, llm, angle, audience, depth)

    # 4. 写作草稿
    draft = write_draft(merged, facts, outline, llm, team_context, include_summary)

    # 5. 自查 + 改写
    article = review_and_rewrite(draft, merged, facts, outline, llm, progress)

    # 6. 输出
    return save_output(article, merged, facts, work_dir)
```

- `depth` / `audience` / `angle` 都是注入 prompt 的参数，不影响代码分支
- 多材料合成是 `merge_materials()` 的责任，其余步骤不需要知道材料数量
- 每一步的输入输出都是数据结构，方便单独测试和替换

### 3. arXiv CLI — 一键全流程

```python
# arxiv_tool/cli.py
def main():
    parser = argparse.ArgumentParser(description="arXiv 每日采集与筛选")
    parser.add_argument("--config", default="config/ai_infra.json")
    parser.add_argument("--output-dir", default="data/arxiv")
    parser.add_argument("--skip-ai", action="store_true", help="跳过 AI 筛选")
    parser.add_argument("--interactive", action="store_true", help="AI 筛选后进入交互选择")
    args = parser.parse_args()

    # 1. 采集最新一个公告日的全部新论文
    date, papers = collector.collect(args.output_dir)

    # 2. 可配置关键词筛选
    filtered = keyword_filter.run(papers, args.config)

    # 3. AI 筛选（可选）
    if not args.skip_ai:
        candidates = ai_filter.run(filtered, args.config)
    else:
        candidates = filtered

    # 4. 人工交互选择（可选）
    if args.interactive:
        selected = interactive_select(candidates)
    else:
        selected = candidates

    # 5. 导出
    export.save_all(args.output_dir, date, papers, filtered, selected)
```

导出文件：

```
data/arxiv/2025-01-15/
├── papers.json          # 全量记录
├── filtered.json        # 关键词筛选结果
├── selected.json        # 最终选中（含 AI 结果 + 人工标记）
└── selected.md          # 中英标题 + arXiv 链接，可直接喂给 generator
```

### 4. 两工具的衔接方式

两个工具通过文件（JSON 或 Markdown）衔接，互不依赖对方的代码或数据库。

```bash
# 场景 A：arXiv 全自动流程
python -m arxiv_tool
python -m generator data/arxiv/2025-01-15/selected.json --angle "AI Infra"

# 场景 B：手动给一篇本地 PDF
python -m generator paper.pdf --audience "算法工程师" --depth deep-dive

# 场景 C：多材料合成一篇文章
python -m generator paper.pdf supplementary.html blog-post.md \
    --angle "对比三种量化方案的工程取舍"

# 场景 D：直接从 URL 抓取
python -m generator https://arxiv.org/abs/2501.12345 https://blog.example.com/post

# 场景 E：arXiv 选中结果批量生成
python -m generator data/arxiv/2025-01-15/selected.json --batch
```

Generator CLI 的参数设计：

```
python -m generator [SOURCES...] [OPTIONS]

位置参数:
  SOURCES               一个或多个材料路径/URL/JSON 列表文件

选项:
  --angle TEXT           写作角度
  --audience TEXT        目标读者（默认: 工程师）
  --depth LEVEL         详细程度: brief / detailed / deep-dive（默认: detailed）
  --output-dir PATH     输出目录（默认: data/articles）
  --no-summary          不生成结尾总结
  --team-context FILE   团队背景 JSON 文件
  --batch               SOURCES 是 JSON 列表，逐篇生成
```

### 5. Web 界面的位置

`web/app.py` 变成一个薄壳，只负责 HTTP 路由、任务队列和持久化，核心逻辑全部调用两个工具的 Python API：

```python
# web/app.py
from arxiv_tool import collector, keyword_filter, ai_filter
from generator import pipeline, sources
from shared.llm import LLM

# 采集任务 → 调 arxiv_tool
date, papers = collector.collect(data_dir / "raw")
filtered = keyword_filter.run(papers, config)

# 生成任务 → 调 generator
material = sources.detect(pdf_path).extract(pdf_path, work_dir)
result = pipeline.generate([material], llm, work_dir, angle=angle)
```

Store（SQLite）和 job queue 留在 web 层，不侵入两个工具的核心逻辑。CLI 用户和 Web 用户走同一套底层代码。

---

## 从现有代码的迁移映射

| 现有文件 | 迁移去向 | 改动程度 |
|---|---|---|
| `pipeline/arxiv.py` | `arxiv_tool/collector.py` | 平移，去掉对 Store 的隐式依赖 |
| `scripts/filter_papers.py` | `arxiv_tool/keyword_filter.py` | 平移，`evaluate()` 改为批量接口 |
| `app.py` 中 AI 筛选段 | `arxiv_tool/ai_filter.py` | 抽出为独立函数 |
| 新增 | `arxiv_tool/export.py` | 新写，导出 JSON + Markdown |
| `pipeline/articles.py` | `generator/pipeline.py` + `evidence.py` | **拆分重构**，解耦 arXiv 专用结构 |
| `pipeline/figures.py` | `generator/figures/latex.py` | 平移 |
| `pipeline/html_figures.py` | `generator/figures/html_img.py` | 平移 |
| `pipeline/writing.py` | `generator/writer.py` | 平移，prompt 参数化 audience/depth |
| `pipeline/editorial.py` | `generator/pipeline.py` 中内联 | 合并，逻辑简单 |
| `pipeline/llm.py` | `shared/llm.py` | 平移 |
| `pipeline/wechat.py` | `shared/wechat.py` | 平移 |
| `pipeline/store.py` | `web/store.py` | 平移 |
| `app.py` | `web/app.py` | 瘦身，改为调用两个工具的 API |
| `static/` | `web/static/` | 不变 |
| 新增 | `generator/sources/html.py` | 新写，HTML 正文提取 |
| 新增 | `generator/sources/markdown.py` | 新写，Markdown 解析 |
| 新增 | `generator/sources/url.py` | 新写，URL 抓取 + 委派 |

改动最大的是 `pipeline/articles.py` → `generator/pipeline.py` + `evidence.py`，需要把 arXiv 专用逻辑（`paper["pdf_url"]`、`paper["version_id"]` 等）替换为 `Material` 统一表示。其余模块基本是平移 + 调整 import。

---

## 扩展性说明

| 扩展场景 | 改动范围 |
|---|---|
| 加新材料类型（如 EPUB） | 写一个 `Source` 子类，注册到 `sources/__init__.py`，pipeline 零改动 |
| 加新筛选维度（如按机构过滤） | 在 `arxiv_tool/` 加一个 filter 模块，CLI 加个 flag |
| 换 prompt 策略 | `writer.py` 里的 prompt 模板，audience/depth/angle 作为变量注入 |
| 换 LLM provider | `shared/llm.py` 已经基于 OpenAI 兼容接口，改 `.env` 即可 |
| 批量生成 | generator CLI 的 `--batch` flag，读 JSON 列表逐篇处理 |
| 接入新 Web 框架 | 两个工具都是纯 Python API，不绑定 Flask |
| 加新配图来源 | 在 `generator/figures/` 加模块，Source 的 `extract()` 中调用 |

---

## 建议实施顺序

1. **创建目录结构 + 平移共享模块**：`shared/llm.py`、`shared/wechat.py`
2. **搭建 arxiv_tool**：平移 collector / keyword_filter / ai_filter，写 CLI 入口和 export
3. **搭建 generator 骨架**：定义 `Source` 协议和 `Material`，先实现 `PdfSource`（从现有代码迁移）
4. **迁移 pipeline**：拆 `articles.py` 为 `pipeline.py` + `evidence.py`，用 `Material` 替换 `paper` dict
5. **实现新 Source**：`HtmlSource`、`MarkdownSource`、`UrlSource`
6. **改造 Web 层**：`web/app.py` 改为调用两个工具的 API
7. **补测试**：每个模块独立可测

步骤 1-4 完成后两个工具就可以独立运行。步骤 5-7 是增量扩展。
