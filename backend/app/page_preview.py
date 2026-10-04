from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_PAGE_PREVIEW_BYTES = 100 * 1024 * 1024


class PagePreviewError(Exception):
    """A user-actionable DOCX page preview conversion error."""


def render_docx_to_pdf(source: bytes) -> bytes:
    """Render DOCX bytes to a local PDF using an isolated LibreOffice profile."""
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if executable is None:
        raise PagePreviewError(
            "Install LibreOffice to preview DOCX pages. The supported-text preview is still available."
        )

    try:
        with tempfile.TemporaryDirectory(prefix="blot-docx-preview-") as temporary_directory:
            work_dir = Path(temporary_directory)
            input_path = work_dir / "document.docx"
            output_path = work_dir / "document.pdf"
            input_path.write_bytes(source)
            result = subprocess.run(
                [
                    executable,
                    "--headless",
                    f"-env:UserInstallation={(work_dir / 'profile').as_uri()}",
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(work_dir),
                    str(input_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if result.returncode != 0 or not output_path.is_file():
                raise PagePreviewError("LibreOffice could not lay out this DOCX for page preview.")
            if output_path.stat().st_size > MAX_PAGE_PREVIEW_BYTES:
                raise PagePreviewError("The rendered page preview exceeds the 100 MB limit.")
            pdf = output_path.read_bytes()
            if not pdf.startswith(b"%PDF-"):
                raise PagePreviewError("LibreOffice returned an invalid page preview.")
            return pdf
    except subprocess.TimeoutExpired as exc:
        raise PagePreviewError("DOCX page rendering took too long. The text preview is still available.") from exc
    except OSError as exc:
        raise PagePreviewError("LibreOffice could not render this DOCX page preview.") from exc
