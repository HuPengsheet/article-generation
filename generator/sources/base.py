"""Format-independent, ordered and attributable source material."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ExtractedText:
    ref: str
    text: str
    heading: str = ""


@dataclass
class Figure:
    file: str
    caption_en: str
    order: int
    origin: str = "source"
    panels: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Material:
    title: str
    authors: list[str]
    sections: list[ExtractedText]
    figures: list[Figure]
    metadata: dict
    limitations: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.metadata.setdefault("extracted_at", datetime.now(timezone.utc).isoformat())


class Source:
    def extract(self, path_or_url: str, work_dir: Path) -> Material:
        raise NotImplementedError
