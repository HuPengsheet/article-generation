import io
import tarfile

import pymupdf
import pytest


@pytest.fixture
def source_tar():
    def make(files=None):
        with pymupdf.open() as doc:
            page = doc.new_page(width=160, height=100)
            page.insert_text((15, 40), "Method diagram")
            pdf = doc.tobytes()
        entries = (
            files
            if files is not None
            else {
                "main.tex": rb"\documentclass{article}\begin{document}\input{sections/method}\end{document}",
                "sections/method.tex": rb"""\begin{figure}
\includegraphics[width=\linewidth]{assets/method}
\caption[Short]{Method {overview} with \textbf{KV cache}.}\label{fig:method}
\end{figure}""",
                "assets/method.pdf": pdf,
            }
        )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return buffer.getvalue()

    return make
