"""Regression checks for citation namespaces and failed figure export."""

import zipfile

import pymupdf
import pytest

from generator import writer
from generator.evidence import merge_materials, publication_copy
from generator.legacy import _legacy_publication_copy, _legacy_section_evidence
from generator.output import save_output
from generator.sources.base import ExtractedText, Figure, Material


def evidence(filename):
    return {
        "sources": [],
        "sections": [],
        "facts": [],
        "figures": [{"file": filename, "origin": "local", "caption_en": "Diagram"}],
        "limitations": [],
        "review": {
            "rewrite_attempted": False,
            "rewrite_applied": False,
            "lint_before": [],
            "issues": [],
            "lint_after": [],
        },
        "model": "test-model",
    }


def test_legacy_citation_helpers_are_isolated_from_generic_writer():
    article = "# Title\n\nPDF fact [PDF p.7].\n\nGeneric fact [S2:B3].\n"
    legacy_text, legacy_citations = _legacy_publication_copy(article)
    generic_text, generic_citations = publication_copy(article)
    assert "[PDF p.7]" not in legacy_text and "[S2:B3]" in legacy_text
    assert legacy_citations[0]["pages"] == [7]
    assert "[S2:B3]" not in generic_text and "[PDF p.7]" in generic_text
    assert generic_citations[0]["refs"] == ["S2:B3"]
    assert not hasattr(writer, "publication_copy")
    assert not hasattr(writer, "section_evidence")
    facts = [{"page": 7, "claim": "a", "quote": "a"}, {"page": 8, "claim": "b", "quote": "b"}]
    assert (
        _legacy_section_evidence({"sections": [{"fact_pages": [7]}]}, facts)[0]["fact_quotes"]
        == facts[:1]
    )


def test_conversion_failure_identifies_material_and_figure(tmp_path):
    source = tmp_path / "source"
    (source / "figures").mkdir(parents=True)
    (source / "figures/broken.png").write_bytes(b"not an image")
    first = Material("First source", [], [ExtractedText("paragraph 1", "First fact.")], [], {})
    second = Material(
        "Second source",
        [],
        [ExtractedText("paragraph 1", "Second fact.")],
        [Figure("figures/broken.png", "Diagram", 1)],
        {"work_dir": str(source)},
    )
    with pytest.raises(ValueError, match="图片转换失败") as error:
        merge_materials([first, second], tmp_path / "output")
    assert "S2" in str(error.value)
    assert "Second source" in str(error.value)
    assert "figures/broken.png" in str(error.value)
    assert error.value.__cause__ is not None


def test_missing_export_image_is_reported_before_publishing(tmp_path):
    previous = tmp_path / "images.zip"
    previous.write_bytes(b"previous export")
    with pytest.raises(ValueError, match="图片文件缺失.*figures/missing.png"):
        save_output("# Title\n\nFact.\n", evidence("figures/missing.png"), tmp_path)
    assert previous.read_bytes() == b"previous export"
    assert not (tmp_path / "article.md").exists()


@pytest.mark.parametrize(
    "filename",
    ["../private.png", "/tmp/private.png", "figures/nested/private.png", "figures/private.pdf"],
)
def test_export_rejects_unregistered_path_shapes(tmp_path, filename):
    with pytest.raises(ValueError, match="图片路径无效"):
        save_output("# Title\n\nFact.\n", evidence(filename), tmp_path)
    assert not (tmp_path / "images.zip").exists()


def test_export_rejects_image_symlink_outside_article_directory(tmp_path):
    root = tmp_path / "article"
    (root / "figures").mkdir(parents=True)
    private = tmp_path / "private.png"
    private.write_bytes(b"private bytes")
    (root / "figures/linked.png").symlink_to(private)
    with pytest.raises(ValueError, match="图片路径越界.*linked.png"):
        save_output("# Title\n\nFact.\n", evidence("figures/linked.png"), root)
    assert not (root / "images.zip").exists()


def test_zip_read_failure_names_image_and_preserves_previous_archive(tmp_path, monkeypatch):
    figures = tmp_path / "figures"
    figures.mkdir()
    with pymupdf.open() as doc:
        doc.new_page(width=100, height=50)
        doc[0].get_pixmap().save(figures / "diagram.png")
    previous = tmp_path / "images.zip"
    previous.write_bytes(b"previous export")

    def fail_read(*args, **kwargs):
        raise PermissionError("controlled read failure")

    monkeypatch.setattr(zipfile.ZipFile, "write", fail_read)
    with pytest.raises(ValueError, match="图片读取失败.*figures/diagram.png") as error:
        save_output("# Title\n\nFact.\n", evidence("figures/diagram.png"), tmp_path)
    assert isinstance(error.value.__cause__, PermissionError)
    assert previous.read_bytes() == b"previous export"
    assert not list(tmp_path.glob(".images-*"))
