import pytest

from generator.output import save_output
from generator.pipeline import generate
from generator.sources.base import ExtractedText, Material
from generator.writer import validate_generic_article, validate_generic_outline


def test_no_images_stops_before_model_calls_and_preserves_material(tmp_path):
    class UnusedModel:
        def complete(self, *args):
            pytest.fail("Missing images must stop before invoking the model")

    material = Material("Paper", [], [ExtractedText("p.1", "Mechanism.")], [], {})
    with pytest.raises(ValueError, match="可用配图"):
        generate([material], UnusedModel(), tmp_path)
    assert (tmp_path / "materials.json").is_file()
    assert not (tmp_path / "article.md").exists()


def test_model_cannot_omit_available_images_from_outline_or_article():
    facts = [{"ref": "S1:B1", "claim": "Mechanism", "quote": "Mechanism"}]
    figures = [{"file": "figures/method.png"}]
    outline = {
        "title": "Mechanism",
        "sections": [
            {
                "heading": str(i),
                "goal": "Explain",
                "transition": "Next",
                "fact_refs": ["S1:B1"],
                "figures": [],
            }
            for i in range(4)
        ],
    }
    with pytest.raises(ValueError, match="未选择配图"):
        validate_generic_outline(outline, facts, figures)
    with pytest.raises(ValueError, match="缺少实际配图"):
        validate_generic_article("# Mechanism\n\nDescription [S1:B1].", facts, figures)


@pytest.mark.parametrize("failure", ["omitted", "missing", "corrupt", "unlisted"])
def test_export_cannot_publish_undeliverable_images(tmp_path, failure):
    directory = tmp_path / "figures"
    directory.mkdir()
    filename = "figures/method.png"
    article = f"# Mechanism\n\n![Method]({filename})\n"
    evidence = {"figures": [{"file": filename}]}
    if failure == "omitted":
        article = "# Mechanism\n\nDescription.\n"
    elif failure == "corrupt":
        (tmp_path / filename).write_bytes(b"not a png")
    elif failure == "unlisted":
        evidence["figures"] = []
    with pytest.raises(ValueError, match="配图|图片"):
        save_output(article, evidence, tmp_path)
    assert not (tmp_path / "article.md").exists()
    assert not (tmp_path / "wechat.html").exists()
