# article-generation

本地论文选题与技术文章生成 MVP：**一天的全部 arXiv 新论文 → 标题/摘要/关键词过滤 → AI 过滤 → 人工筛选 → 全文阅读 → Markdown 文章草稿**。

## 启动

使用已有的 Conda `py313` 环境（Python 3.13）。

```bash
conda activate py313
python -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填写模型服务配置；未配置时仍可采集、关键词筛选和人工选择。
python app.py
```

打开 <http://127.0.0.1:8000>。服务仅监听本机。端口可通过 `.env` 的 `PORT` 修改。

## 完整操作流程

1. 点击「采集最新一天」：覆盖全部学科主档案，原始 RSS 和论文元数据保存到本地。完整采集成功后自动运行关键词过滤。
2. 点击「运行 AI 筛选」：处理所有关键词通过的论文。`keep` 和 `uncertain` 进入「人工候选」，`reject` 也保留，可切换查看。未配置模型时此按钮不可用。
3. 在候选卡片中选择「已选中 / 已跳过 / 稍后看」，可填写写作角度，然后点击「保存选择」。也可以从「全部论文」中选中被过滤的论文。
4. 点击「生成选中文章」：仅处理人工选中且没有已有文章的论文；下载指定版本 PDF，同时读取源码配图，逐段生成已校验的证据笔记，再依次设计大纲、成稿、自查，必要时改写一次。不会在全文失败时退回摘要编写。
5. 点击「查看文章」审阅草稿，通过「公众号预览」复制图文，或下载 Markdown 和图片包。「查看证据笔记」打开单独的核对材料；「导出全部记录」导出论文、过滤结果、人工选择、文章与证据。

采集和模型任务在后台运行，页面显示进度。一次只运行一个任务。重复采集同一天保留已成功的 AI 筛选、人工选择和文章；重复运行 AI 或文章任务会重试失败项。重跑关键词会清除该批次的 AI 结果，保留人工选择和文章。服务重启会把未完成任务标为中断，可重新运行。

第一版每篇论文生成一篇中文技术解读，文章正文以 Markdown 原文展示。写作角度在第一次生成时生效，已有文章不会自动覆盖。此版没有自动发布、定时调度、多篇综述和文章在线编辑。

## 模型配置

接入支持 Chat Completions 和 JSON 对象输出的兼容接口。在本地 `.env` 填写：

```dotenv
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=你的本地密钥
LLM_MODEL=你的模型名
```

服务地址包含 `/v1`；程序追加 `/chat/completions`。没有指定服务商或默认付费模型，只有点击 AI 筛选或文章生成才调用模型。模型名需采用服务商实际提供的名称；服务端读取密钥，不发送到前端，`.env` 已加入忽略列表。修改配置后重启服务。

本地 Ollama 可使用 `LLM_BASE_URL=http://localhost:11434/v1`、`LLM_API_KEY=ollama` 以及已下载的模型名。接口需支持 `response_format: {"type":"json_object"}`。参考 [Ollama 官方兼容接口文档](https://docs.ollama.com/api/openai-compatibility)。

AI 相关性评分用于排序，不代表论文质量。AI 证据必须能匹配标题/摘要中的文本；全文笔记的引文须能匹配对应 PDF 页。检查不通过会报错并保留失败状态，不伪装成已完成。这些检查验证引用位置，不能保证模型对原文的解读一定正确，发布前仍需人工审核。

## 图文提取与写作流程

- 配图读取固定版本的 `https://arxiv.org/e-print/{version_id}`，支持 tar、gzip 单文件和纯 TeX。源码解包拒绝越界路径、链接和特殊文件，不执行 TeX。
- 从主文件递归展开 `input/include`，读取 figure/figure* 和 wrapfigure 内的图片及外层图注，处理嵌套花括号与 graphicspath。PDF 使用 PyMuPDF 按 200 DPI 渲染，JPEG 转 PNG，PNG 校验后保留。提取 `subfigure` 环境以及 `subfloat/subfigure` 命令的分图注。多子图连同图例合并，清单保留 `panels` 的子图编号、图注及合并后的位置；宽高比超过 3 的长条图用单列。图注清理 cite/ref、数学定界符和常见文字宏，简单的零参数文字宏可安全展开，不执行 TeX。布局仍是近似，原图的数据和坐标保留。
- 源码缺失、宏绘图、TikZ/EPS 或转换失败时，尝试固定版本的官方 HTML。只下载该版本 arXiv 路径下的素材，按原图编号补全；成功的源码图片不重复下载，不把缺失面板的 HTML 图冒充完整图。网络获取与解包拒绝分别记录，两个来源均不可用时才生成纯文字文章。解析器不是完整 LaTeX 引擎，特殊宏和编号规则仍需人工核对。
- 大纲为 4–8 节，章节页码和图片文件均须来自已验证清单。成稿与改写稿重新校验页码、Markdown 图片路径；引用清单外图片不能发布。
- 初稿检查套话、列表占比、段首四字重复、节末机械小结、三段式脚手架和缺少量化限定的“显著提升”。这些均为编辑提示，操作步骤或必要推论可人工保留，段落长度检测仅作弱提示。模型自查与改写同时收到每节 goal、transition 和对应页的逐字 fact quotes，以核对转折的原文依据。有问题时最多改写一次；改写后仍有风格提示则记录待确认事项。改写稿的证据校验失败时保留已校验初稿并明确记录原因。不会反复调用模型，也不声称风格检查能保证文章质量。
- 正文不附“核对依据 / 阅读局限”及英文引文清单，改存 `review.md` 和 notes 接口；发布正文去掉 `[PDF p.N]` 页码标记，带引用稿保存为 `article-cited.md`，段落与页码对应关系留在核对材料中。标题和章节具体，展开机制与实验，默认结尾有简短收束，标题不固定；`include_summary=false` 时由内容决定是否需要收束，不写泛化的启发章节或逐节小结。团队介绍必须有经调用者核实的 `team_context`；作者名单和写作角度不能证明团队声望。自绘图必须登记实际素材和假设，模型不能虚构图片。可见图注使用独立斜体段落，公众号渲染为灰色小字。

每篇有 N 个 PDF 阅读分块时，模型调用为 N+3 次（笔记、大纲、成稿、自查），需要一次改写时为 N+4 次。自查接收正文、章节转折、对应原文引文及分图图注，不读取图片像素，因此不能替代对图中细节的人工核对。

生成后的图片通过 `/api/article/image` 按清单返回，`/api/article/images.zip` 下载图片和正文。预览逐图生成接口地址，复制时嵌入同源 PNG；实际公众号对嵌入图片的支持仍需验证，图片包可用于手动上传。

可选资料入口：`generate(..., team_context=..., illustrations=..., include_summary=True)`。团队资料形如 `{"verified":true,"description":"已核实事实","sources":[{"title":"机构页面","url":"https://..."}]}`；程序检查字段和来源网址格式，不自动证明声望或真实性。自绘素材为 `[{"path":"/本地/diagram.png","caption":"机制示意","assumptions":"假设数据，非实测"}]`，校验 PNG 后复制至统一 `figures/` 清单，支持预览、核对材料和图片包。图可以由当前对话制作或由人工提供，此入口不调用图片生成 API。

`POST /api/review` 可另提交 `team_context` 和 `include_summary`；省略时保留已有设置。自绘 PNG 由可信本地调用者传入生成函数，网页暂未提供上传入口。已有旧报告文章可运行 `conda run -n py313 python -m scripts.migrate_report_articles` 迁移，操作幂等且保留人工与 AI 筛选状态。工作台统一使用文章目录及 `/api/article/*` 接口，独立报告链接继续可用。

## 「一天的全部论文」如何定义

- 按 RSS 的公告日期采集一个批次，而非北京时间自然日或投稿日期。
- 从官方分类目录动态发现全部活跃分类和主档案；目前是 20 个主档案，涵盖全部学科，采集阶段没有 AI Infra 预过滤。
- 仅纳入 `announce_type=new` 的首次公开论文；旧论文更新与交叉分类记录不作为新论文计数。按论文 ID 去重。
- 各来源必须属于同一个公告日，才提交完整批次并进入筛选。单个 feed 达到 2000 条时继续按子分类采集；子分类仍达到上限时停止并报错，不宣称完整。
- 请求间隔至少 3 秒，缓存成功响应，网络失败重试。RSS 当前公告日可能早于本地今天，周末或节假日可能没有新论文。
- RSS 只提供当前批次，MVP 不支持从网络恢复任意历史公告日。采集过的批次可长期保存、查看和导出。

依据：[RSS 文档](https://info.arxiv.org/help/rss.html)、[字段与公告类型说明](https://info.arxiv.org/help/rss_specifications.html)、[分类目录](https://arxiv.org/category_taxonomy)。

## AI Infra 条件

配置在 `config/ai_infra.json`，AI 提示词在 `prompts/ai_infra_filter.md`。

- 强相关词命中任意一个即可通过；宽泛词需要同时命中 AI 上下文词。
- 忽略大小写，统一空格和连字符，按完整词或短语匹配。
- 默认没有硬性排除词；关键词为可选元数据，arXiv 未提供时匹配标题和摘要。
- 所有被排除记录保留，可从「全部论文」中查看和人工选择。
- 修改配置后，在页面点击「重跑关键词」，再重新运行 AI 筛选。

独立过滤已有 JSON 数组也仍然可用：

```bash
python scripts/filter_papers.py daily_papers.json --output filtered_papers.json
```

输入字段至少提供 `id`、`title`、`abstract`（或 `summary`）；`keywords` 为可选字符串数组。输出保留原始记录、筛选理由、命中词和配置快照。

## 数据与代码

```text
app.py                       本地 HTTP 服务与后台任务
pipeline/arxiv.py            分类发现、RSS 采集、日期与完整性检查
pipeline/llm.py              模型接口与摘要证据校验
pipeline/articles.py         PDF 阅读、证据笔记、文章生成
pipeline/figures.py          源码获取、安全解包、配图与分图注提取
pipeline/html_figures.py     固定版本的官方 HTML 配图兜底
pipeline/editorial.py        团队背景与自绘素材的明确输入和校验
pipeline/writing.py          大纲、自查与改写提示词，引用和风格检查
pipeline/store.py            SQLite 持久化
pipeline/wechat.py           Markdown 转行内样式 HTML 与公众号复制预览
static/                      人工筛选与文章查看页面
config/ai_infra.json          关键词配置
prompts/ai_infra_filter.md    AI 筛选要求
data/app.sqlite3             论文、任务、人工选择和文章
data/raw/<公告日>/            原始 RSS、完整性清单、全量论文 JSON
data/articles/<公告日>/<ID>/   PDF、源码、figures/、笔记、大纲、初稿、正文和 review.md
```

`data/`、`reports/`、本地 `.env` 及其变体、SQLite 数据库和缓存均不进入 Git；仅保留不含密钥的 `.env.example` 配置模板。首次克隆不包含已采集论文、筛选记录或已生成文章，启动后从页面采集并生成即可。

PDF 文字提取可能遗漏图表、公式和扫描页，笔记会标记无文字的页，并记录提取器发出的遗漏或截断提示；MVP 未接入 OCR 或图表识别。参考 [pypdf 文字提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

## 验证

```bash
conda activate py313
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
node --check static/app.js
```

测试覆盖公告采集、证据校验、源码安全解包、嵌套图注、多子图合并、图片失败降级、大纲校验、清单外图片、自查改写、正文与核对材料分离、供图与 ZIP 导出、完整后台流程和配置缺失。自动测试中的模型为受控测试接口，不代表真实模型已经运行。

## 浏览器检查

服务启动后，可检查图片加载、图注、图文复制、普通富文本粘贴和手机排版：

```bash
python -m scripts.check_preview --url "你的本地文章预览地址"
```

脚本优先使用本机 Google Chrome，也可通过 `--browser-path` 指定 Chromium。如果未安装浏览器，运行 `python -m playwright install chromium` 安装 Playwright Chromium。`--url` 可传多个预览地址。检查不会生成截图、临时数据库或修改文章；实际公众号后台仍可能调整粘贴样式。

## 本地产物与 GitHub 上传

`data/raw/` 保存全量采集缓存；`data/papers/` 保存原始论文；`data/articles/` 保存后台文章目录及初稿、大纲、证据和配图。可选的 `reports/` 保存独立导出的成品和核对材料。这两个目录保留在本地，均已加入 `.gitignore`。

服务启动并生成文章后，在工作台点击「查看文章 → 公众号预览」，将预览地址传入浏览器检查脚本。复制正文后粘贴到公众号，标题单独复制；如果公众号未保留图片，可下载图片包上传。

上传前可运行 `git status --short` 和 `git add --dry-run .` 查看待提交文件。仓库包含代码、关键词配置、提示词、测试与文档，不包含实际采集结果、人工筛选记录、生成文章、论文 PDF、图片包或模型密钥。本地数据无需删除，不影响继续使用。
