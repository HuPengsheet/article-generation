import gzip
import io
import tarfile

import pymupdf
import pytest

import pipeline.figures as figures
import pipeline.html_figures as html_figures


def test_recursive_sources_render_pdf_and_nested_caption(tmp_path, source_tar, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return source_tar()

    monkeypatch.setattr(figures, "request_bytes", fetch)
    warnings = []
    root = figures.fetch_source({"version_id": "2610.00001v1"}, tmp_path, warnings)
    result = figures.extract_figures(root, tmp_path, warnings)
    assert calls == ["https://arxiv.org/e-print/2610.00001v1"]
    assert not warnings
    assert len(result) == 1
    assert result[0]["caption_en"] == "Method {overview} with KV cache."
    assert result[0]["label"] == "fig:method" and result[0]["order"] == 1
    assert (tmp_path / result[0]["file"]).read_bytes().startswith(b"\x89PNG")
    assert figures.fetch_source({"version_id": "2610.00001v1"}, tmp_path) == root
    assert len(calls) == 1


@pytest.mark.parametrize("compress", [False, True])
def test_single_tex_sources(tmp_path, compress):
    raw = rb"\documentclass{article}\begin{document}No figures\end{document}"
    figures.unpack_source(gzip.compress(raw) if compress else raw, tmp_path)
    assert (tmp_path / "main.tex").read_bytes() == raw
    warnings = []
    assert figures.extract_figures(tmp_path, tmp_path / "out", warnings) == []
    assert any("纯文字" in w for w in warnings)


@pytest.mark.parametrize(
    "name,link", [("../escaped.tex", False), ("/escaped.tex", False), ("link", True)]
)
def test_source_archive_rejects_traversal_and_links_before_writes(tmp_path, name, link):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        valid = tarfile.TarInfo("main.tex")
        valid.size = 4
        archive.addfile(valid, io.BytesIO(b"test"))
        bad = tarfile.TarInfo(name)
        if link:
            bad.type = tarfile.SYMTYPE
            bad.linkname = "/etc/passwd"
        archive.addfile(bad)
    with pytest.raises(ValueError):
        figures.unpack_source(buffer.getvalue(), tmp_path / "source")
    assert not (tmp_path / "source/main.tex").exists()


def test_missing_source_degrades_with_limitation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        figures, "request_bytes", lambda url: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    monkeypatch.setattr(
        html_figures, "request_bytes", lambda url: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    result, warnings = figures.collect_figures({"version_id": "2610.00001v1"}, tmp_path)
    assert result == [] and any("源码获取" in w for w in warnings)


def test_subfigures_share_outer_caption_and_skip_unsupported(tmp_path, source_tar):
    with pymupdf.open() as doc:
        doc.new_page(width=50, height=50)
        png = doc[0].get_pixmap().tobytes("png")
    raw = source_tar(
        {
            "main.tex": rb"""\documentclass{article}
\graphicspath{{assets/}}\begin{document}
% \begin{figure}\includegraphics{wrong}\caption{Comment}\end{figure}
\begin{figure*}
\begin{subfigure}{.5\linewidth}\includegraphics{one}\caption{Inner}\end{subfigure}
\includegraphics{two.jpg}\caption{Outer {caption} with 99\% coverage.}\label{fig:pair}
\end{figure*}
\begin{figure}\includegraphics{plot.eps}\caption{Unsupported}\end{figure}
\end{document}""",
            "assets/one.png": png,
            "assets/two.jpg": b"not-an-image",
            "assets/plot.eps": b"%!PS-Adobe",
        }
    )
    root = figures.unpack_source(raw, tmp_path / "source")
    warnings = []
    result = figures.extract_figures(root, tmp_path, warnings)
    assert len(result) == 1
    assert result[0]["caption_en"] == "Outer {caption} with 99% coverage."
    assert result[0]["order"] == 1
    assert any("转换失败" in w for w in warnings)
    assert any("不支持" in w for w in warnings)


def test_include_cycles_and_missing_files_are_reported(tmp_path, source_tar):
    root = figures.unpack_source(
        source_tar(
            {
                "main.tex": rb"""\documentclass{article}
\begin{document}\input{loop}\input{missing}\end{document}""",
                "loop.tex": rb"\input{main}",
            }
        ),
        tmp_path / "source",
    )
    warnings = []
    assert figures.extract_figures(root, tmp_path, warnings) == []
    assert any("循环" in w for w in warnings) and any("missing" in w for w in warnings)


def test_multi_panel_figure_merges_legend_and_preserves_outer_number(tmp_path, source_tar):
    with pymupdf.open() as doc:
        doc.new_page(width=60, height=30)
        png = doc[0].get_pixmap().tobytes("png")
    root = figures.unpack_source(
        source_tar(
            {
                "main.tex": rb"""\documentclass{article}\begin{document}
\begin{wrapfigure}{r}{.5\textwidth}\TableMacro\end{wrapfigure}
\begin{figure}\includegraphics{legend}\includegraphics{one}\includegraphics{two}
\caption{Outer caption}\label{fig:multi}\end{figure}\end{document}""",
                "legend.png": png,
                "one.png": png,
                "two.png": png,
            }
        ),
        tmp_path / "source",
    )
    warnings = []
    result = figures.extract_figures(root, tmp_path, warnings)
    assert len(result) == 1 and result[0]["order"] == 1 and result[0]["panel_count"] == 3
    assert result[0]["file"] == "figures/figure-01.png"
    assert (tmp_path / result[0]["file"]).read_bytes().startswith(b"\x89PNG")
    assert any("表格宏" in w for w in warnings)


def test_top_bottom_caption_keeps_panels_in_vertical_order(tmp_path, source_tar):
    with pymupdf.open() as doc:
        doc.new_page(width=60, height=30)
        png = doc[0].get_pixmap().tobytes("png")
    root = figures.unpack_source(
        source_tar(
            {
                "main.tex": rb"""\documentclass{article}\begin{document}
\begin{figure}\includegraphics{one}\includegraphics{two}
\caption{Latency (top) and throughput (bottom).}\end{figure}\end{document}""",
                "one.png": png,
                "two.png": png,
            }
        ),
        tmp_path / "source",
    )
    result = figures.extract_figures(root, tmp_path)
    assert len(result) == 1
    image = pymupdf.Pixmap(str(tmp_path / result[0]["file"]))
    assert image.height > image.width


def _png(width=80, height=40):
    with pymupdf.open() as doc:
        doc.new_page(width=width, height=height)
        return doc[0].get_pixmap().tobytes("png")


def test_caption_cleans_citations_refs_and_math_without_losing_content():
    caption = figures._plain_caption(
        r"Cost $O(n)$ and \textbf{\emph{KV cache}} \citep[see][p. 2]{key,{other}} \ref{fig:x}, 99\% and \(x+y\)."
    )
    assert caption == "Cost O(n) and KV cache , 99% and x+y."
    assert "\\cite" not in caption and "fig:x" not in caption


@pytest.mark.parametrize(
    "markup",
    [
        rb"\begin{subfigure}{.5\linewidth}\includegraphics{a}\caption{Latency $O(n)$}\end{subfigure}\begin{subfigure}{.5\linewidth}\includegraphics{b}\caption{Throughput}\end{subfigure}",
        rb"\subfloat[Latency $O(n)$]{\includegraphics{a}}\subfigure[Throughput]{\includegraphics{b}}",
    ],
)
def test_merged_panels_keep_subcaptions_labels_and_positions(tmp_path, source_tar, markup):
    tex = (
        rb"\documentclass{article}\begin{document}\begin{figure}"
        + markup
        + rb"\caption{Outer}\end{figure}\end{document}"
    )
    root = figures.unpack_source(
        source_tar({"main.tex": tex, "a.png": _png(), "b.png": _png()}), tmp_path / "source"
    )
    result = figures.extract_figures(root, tmp_path)
    assert result[0]["caption_en"] == "Outer"
    assert [(p["label"], p["caption_en"], p["position"]) for p in result[0]["panels"]] == [
        ("(a)", "Latency O(n)", "row 1, column 1"),
        ("(b)", "Throughput", "row 1, column 2"),
    ]


def test_long_panels_use_single_column(tmp_path, source_tar):
    root = figures.unpack_source(
        source_tar(
            {
                "main.tex": rb"\documentclass{article}\begin{document}\begin{figure}\includegraphics{a}\includegraphics{b}\caption{Two plots}\end{figure}\end{document}",
                "a.png": _png(240, 40),
                "b.png": _png(60, 60),
            }
        ),
        tmp_path / "source",
    )
    result = figures.extract_figures(root, tmp_path)
    assert [p["position"] for p in result[0]["panels"]] == ["row 1, column 1", "row 2, column 1"]


def test_source_failures_distinguish_download_from_unpack(tmp_path, monkeypatch):
    monkeypatch.setattr(figures, "request_bytes", lambda url: b"not tex")
    warnings = []
    assert figures.fetch_source({"version_id": "2610.00001v1"}, tmp_path, warnings) is None
    assert "解包拒绝" in warnings[0]


def test_html_fallback_keeps_subcaptions_and_ignores_page_logo(tmp_path, monkeypatch):
    html = b"""<img src="logo.png"><figure id="S1.F2" class="ltx_figure">
<figure class="ltx_figure ltx_figure_panel"><img src="2610.00001v1/a.png"><figcaption>(a) Latency</figcaption></figure>
<figure class="ltx_figure ltx_figure_panel"><img src="2610.00001v1/b.png"><figcaption>(b) Throughput</figcaption></figure>
<figcaption>Figure 2: Measurements.</figcaption></figure>"""
    calls = []

    def fetch(url):
        calls.append(url)
        return html if url.endswith("v1") else _png()

    monkeypatch.setattr(
        figures, "request_bytes", lambda url: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    monkeypatch.setattr(html_figures, "request_bytes", fetch)
    result, warnings = figures.collect_figures({"version_id": "2610.00001v1"}, tmp_path)
    assert len(result) == 1 and result[0]["order"] == 2 and result[0]["origin"] == "arxiv_html"
    assert [p["caption_en"] for p in result[0]["panels"]] == ["(a) Latency", "(b) Throughput"]
    assert len(calls) == 3 and all("logo" not in url for url in calls)
    assert any("源码获取" in w for w in warnings) and not any("纯文字" in w for w in warnings)
    assert (tmp_path / result[0]["file"]).is_file()


@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/x.png",
        "/html/2610.00001v2/x.png",
        "2610.00001v1/%2e%2e/x.png",
        "http://arxiv.org/html/2610.00001v1/x.png",
    ],
)
def test_html_assets_must_belong_to_pinned_arxiv_version(src):
    with pytest.raises(ValueError):
        html_figures._asset_url(src, "https://arxiv.org/html/2610.00001v1", "2610.00001v1")


def test_html_recovers_only_missing_source_figures(tmp_path, source_tar, monkeypatch):
    raw = source_tar(
        {
            "main.tex": rb"\documentclass{article}\begin{document}\begin{figure}\includegraphics{a}\caption{Good source}\end{figure}\begin{figure}\tikzdraw\caption{Missing source}\end{figure}\end{document}",
            "a.png": _png(),
        }
    )
    monkeypatch.setattr(figures, "request_bytes", lambda url: raw)
    html = b'<figure class="ltx_figure"><img src="2610.00001v1/a.png"><figcaption>Figure 1: Good HTML</figcaption></figure><figure class="ltx_figure"><img src="2610.00001v1/b.png"><figcaption>Figure 2: Rendered TikZ</figcaption></figure>'
    monkeypatch.setattr(
        html_figures, "request_bytes", lambda url: html if url.endswith("v1") else _png()
    )
    result, warnings = figures.collect_figures({"version_id": "2610.00001v1"}, tmp_path)
    assert [(f["order"], f["origin"]) for f in result] == [(1, "latex"), (2, "arxiv_html")]
    assert result[0]["caption_en"] == "Good source"


def test_html_partial_figure_is_not_exposed_as_complete(tmp_path, monkeypatch):
    html = b'<figure class="ltx_figure"><img src="2610.00001v1/a.png"><img src="https://evil.example/b.png"><figcaption>Figure 3: Two panels</figcaption></figure>'
    monkeypatch.setattr(
        html_figures, "request_bytes", lambda url: html if url.endswith("v1") else _png()
    )
    warnings = []
    assert (
        html_figures.collect_html_figures({"version_id": "2610.00001v1"}, tmp_path, warnings) == []
    )
    assert any("子图 2" in w for w in warnings)


def test_literal_caption_macros_are_expanded_without_executing_tex(tmp_path, source_tar):
    root = figures.unpack_source(
        source_tar(
            {
                "main.tex": rb"\documentclass{article}\newcommand{\mosaic}{\textsc{Mosaic}\xspace}\begin{document}\begin{figure}\includegraphics{a}\caption{\mosaic with $O(n)$ complexity \cite{prior}.}\end{figure}\end{document}",
                "a.png": _png(),
            }
        ),
        tmp_path / "source",
    )
    result = figures.extract_figures(root, tmp_path)
    assert result[0]["caption_en"] == "Mosaic with O(n) complexity ."
