import re
import shutil
import subprocess

import pytest

from generator.writer import lint_article
from shared.wechat import preview, render


def test_chinese_bold_labels_render_and_broken_markers_are_flagged():
    broken = "**删去末 tile 的等待：**FA-4 写出 tile。"
    corrected = "**删去末 tile 的等待**：FA-4 写出 tile。"
    html = render(corrected)
    assert "<strong style=" in html
    assert "删去末 tile 的等待</strong>：FA-4" in html
    assert "**" not in html
    assert any("加粗标记未被" in issue["problem"] for issue in lint_article(broken))
    assert lint_article(corrected) == []
    # Code examples containing literal Markdown must remain untouched.
    assert lint_article(f"`{broken}`") == []
    assert lint_article(f"```markdown\n{broken}\n```") == []


def test_rich_text_escapes_raw_html_and_unsafe_links():
    html = render("<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n**正文**")
    assert "<script>" not in html
    assert 'href="javascript:' not in html
    assert "<strong style=" in html


def test_preview_keeps_title_out_of_clipboard_body_and_styles_tables():
    html = preview("# 唯一标题\n\n正文。\n\n| A | B |\n| --- | --- |\n| 1 | 2 |")
    body = html.split('<section id="article"', 1)[1].split("</section>", 1)[0]
    assert "唯一标题" not in body
    assert "<table style=" in body and "<td style=" in body
    assert "'text/html'" in html and "'text/plain'" in html
    assert '<h1 id="title">唯一标题</h1>' in html


def test_workbench_preview_resolves_relative_images_from_article_directory():
    html = preview(
        "# 标题\n\n![方法](images/method.png)",
        image_base_url="/reports/paper/",
        images_url="/reports/paper/images.zip",
    )
    assert 'src="/reports/paper/images/method.png"' in html
    assert 'href="/reports/paper/images.zip"' in html
    assert "reader.readAsDataURL(blob)" in html


def test_api_image_mapping_preserves_query_and_encodes_ampersands():
    html = preview(
        "# 标题\n\n![方法](figures/method.png)",
        image_urls={
            "figures/method.png": "/api/article/image?batch=2026-10-07&id=2610.00001&name=method.png"
        },
    )
    assert 'src="/api/article/image?batch=2026-10-07&amp;id=2610.00001&amp;name=method.png"' in html


def test_local_preview_embeds_images_and_supports_title_clipboard_fallback():
    html = preview(
        "# 标题\n\n![方法](figures/method.png)",
        image_urls={"figures/method.png": "data:image/png;base64,test"},
    )
    assert 'src="data:image/png;base64,test"' in html
    assert "if(url.protocol==='data:') return" in html
    assert "await writeClipboard(null,node.textContent,node)" in html
    assert "document.execCommand('copy')" in html


@pytest.mark.parametrize("clipboard_mode", ["modern", "denied", "unavailable"])
def test_copy_actions_keep_embedded_images_and_title_separate(tmp_path, clipboard_mode):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise clipboard JavaScript")
    html = preview("# 唯一标题\n\n正文。")
    script = re.search(r"<script>(.*?)</script>", html, re.S)[1]
    path = tmp_path / "clipboard.js"
    path.write_text(script, encoding="utf-8")
    harness = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const mode = process.argv[2], source = fs.readFileSync(process.argv[1], 'utf8');
let copied, listener, fetches = 0, fallbackCalls = 0;
const image = {getAttribute:()=> 'data:image/png;base64,embedded'};
const clone = {querySelectorAll:()=>[image],querySelector:()=>image,outerHTML:'<section>正文<img src="data:image/png;base64,embedded"></section>'};
const article = {innerText:'正文',cloneNode:()=>clone};
const title = {textContent:'唯一标题'};
const nodes = {article,title,status:{},copy:{},'copy-title':{}};
const selection = {rangeCount:0,removeAllRanges(){},addRange(){}};
const document = {
 getElementById:id=>nodes[id],createRange:()=>({selectNodeContents(){}}),
 addEventListener:(name,fn)=>{listener=fn;},removeEventListener:()=>{listener=null;},
 execCommand:()=>{fallbackCalls++;copied={};listener({preventDefault(){},clipboardData:{setData:(k,v)=>{copied[k]=v;}}});return true;}
};
const navigator = mode==='unavailable'?{}:{clipboard:{
 write:async items=>{if(mode==='denied')throw Error('denied');copied={};for(const [k,v]of Object.entries(items[0].data))copied[k]=await v.text();},
 writeText:async text=>{if(mode==='denied')throw Error('denied');copied={'text/plain':text};}
}};
const window = {getSelection:()=>selection,ClipboardItem:function(data){this.data=data;}};
const context = vm.createContext({document,navigator,window,ClipboardItem:window.ClipboardItem,Blob,URL,location:{origin:'null'},fetch:()=>{fetches++;throw Error('File image must not be fetched');}});
vm.runInContext(source,context);
(async()=>{
 await nodes.copy.onclick();
 assert.equal(fetches,0);
 assert.ok(copied['text/html'].includes('data:image/png;base64,embedded'));
 assert.ok(!copied['text/html'].includes('唯一标题'));
 assert.equal(copied['text/plain'],'正文');
 assert.ok(nodes.status.textContent.includes('正文与图片已复制'));
 await nodes['copy-title'].onclick();
 assert.deepEqual(copied,{'text/plain':'唯一标题'});
 assert.equal(nodes.status.textContent,'标题已复制。');
 assert.equal(fallbackCalls,mode==='modern'?0:2);
 assert.equal(listener,undefined===listener?undefined:null);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        [node, "-e", harness, str(path), clipboard_mode],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_caption_has_smaller_gray_inline_style_but_body_does_not():
    html = render(
        "![方法](figures/method.png)\n\n*图1：外层说明，(a) 延迟，(b) 吞吐。*\n\n正文分析。"
    )
    caption = html.split('data-figure-caption="true"', 1)[0].rsplit("<p ", 1)[1]
    assert "font-size:13px" in caption and "color:#6b7280" in caption
    assert "<em style=" in html
    assert "font-size:16px;color:#3a3a3a" in html
    assert "data-figure-caption" not in render("![图](a.png)\n\n正常段落。")
