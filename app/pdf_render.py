"""Rendu PDF/A via le conteneur WeasyPrint (commande pilotée par config)."""

import shlex
import subprocess
import uuid
from pathlib import Path

from app.config import get_settings


class PdfRenderError(Exception):
    """Échec du rendu PDF/A ; str(exc) = sortie du renderer."""


def render_pdfa(html: str, identifier_hex: str, variant: str = "pdf/a-3b") -> bytes:
    """Rend le HTML en PDF/A (A-3b par défaut) avec /ID épinglé."""
    settings = get_settings()
    exchange = Path(settings.exchange_dir)
    exchange.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    html_file = exchange / f"{token}.html"
    pdf_file = exchange / f"{token}.pdf"
    try:
        html_file.write_text(html, encoding="utf-8")
        command = [
            *shlex.split(settings.pdf_render_command),
            f"/data/{token}.html",
            f"/data/{token}.pdf",
            identifier_hex,
            variant,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not pdf_file.exists():
            raise PdfRenderError(
                f"Rendu PDF échoué (code {result.returncode}) :\n"
                f"{result.stderr[-4000:] or result.stdout[-4000:]}"
            )
        return pdf_file.read_bytes()
    finally:
        html_file.unlink(missing_ok=True)
        pdf_file.unlink(missing_ok=True)
