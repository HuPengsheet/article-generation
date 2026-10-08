from pipeline.wechat import preview, render


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


def test_caption_has_smaller_gray_inline_style_but_body_does_not():
    html = render(
        "![方法](figures/method.png)\n\n*图1：外层说明，(a) 延迟，(b) 吞吐。*\n\n正文分析。"
    )
    caption = html.split('data-figure-caption="true"', 1)[0].rsplit("<p ", 1)[1]
    assert "font-size:13px" in caption and "color:#6b7280" in caption
    assert "<em>" in html
    assert "font-size:16px;color:#273244" in html
    assert "data-figure-caption" not in render("![图](a.png)\n\n正常段落。")
