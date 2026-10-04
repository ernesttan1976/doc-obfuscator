from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import page_preview
from backend.app.page_preview import PagePreviewError, render_docx_to_pdf


def test_docx_page_render_reports_missing_libreoffice(monkeypatch):
    monkeypatch.setattr(page_preview.shutil, "which", lambda _: None)

    with pytest.raises(PagePreviewError, match="Install LibreOffice"):
        render_docx_to_pdf(b"docx")


def test_docx_page_render_uses_isolated_profile_and_returns_pdf(monkeypatch):
    monkeypatch.setattr(page_preview.shutil, "which", lambda _: "/usr/bin/soffice")

    def fake_run(arguments, **kwargs):
        work_dir = Path(arguments[arguments.index("--outdir") + 1])
        (work_dir / "document.pdf").write_bytes(b"%PDF-1.7\npreview")
        assert any(argument.startswith("-env:UserInstallation=file:") for argument in arguments)
        assert kwargs["timeout"] == 60
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(page_preview.subprocess, "run", fake_run)

    assert render_docx_to_pdf(b"docx") == b"%PDF-1.7\npreview"


def test_docx_page_render_rejects_invalid_pdf_output(monkeypatch):
    monkeypatch.setattr(page_preview.shutil, "which", lambda _: "/usr/bin/soffice")

    def fake_run(arguments, **kwargs):
        work_dir = Path(arguments[arguments.index("--outdir") + 1])
        (work_dir / "document.pdf").write_bytes(b"not a pdf")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(page_preview.subprocess, "run", fake_run)

    with pytest.raises(PagePreviewError, match="invalid page preview"):
        render_docx_to_pdf(b"docx")
