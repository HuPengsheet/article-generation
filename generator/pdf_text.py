import io
import logging

from pypdf import PdfReader
from pypdf.errors import PyPdfError


def extract_pages(raw):
    class CaptureWarnings(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    logger = logging.getLogger("pypdf")
    capture = CaptureWarnings()
    logger.addHandler(capture)
    pages = []
    try:
        reader = PdfReader(io.BytesIO(raw))
        for index, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            pages.append(
                {"page": index + 1, "text": text, "extraction_warnings": list(capture.messages)}
            )
            capture.messages.clear()
    except PyPdfError:
        raise ValueError("PDF 无法解析，可能已损坏或需要密码。") from None
    finally:
        logger.removeHandler(capture)
    if not pages or sum(len(page["text"]) for page in pages) < 300:
        raise ValueError("PDF 可提取文字不足，不能仅基于摘要生成文章。")
    return pages
