"""Contrôle PDF/A-3b par veraPDF (conteneur JVM, commande pilotée par config).

Fail-closed, même contrat que le Schematron en 4b-1 : échec ou verdict
non-PASS = exception avec le rapport intact, aucun artefact en aval.
"""

import shlex
import subprocess
import uuid
from pathlib import Path

from app.config import get_settings


class PdfAValidationError(Exception):
    """Le PDF n'est pas conforme PDF/A-3b ; str(exc) = rapport veraPDF."""


def assert_pdfa3b(pdf_bytes: bytes) -> None:
    settings = get_settings()
    exchange = Path(settings.exchange_dir)
    exchange.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    pdf_file = exchange / f"{token}.pdf"
    try:
        pdf_file.write_bytes(pdf_bytes)
        command = [
            *shlex.split(settings.verapdf_command),
            "--flavour",
            "3b",
            "--format",
            "text",
            f"/data/{token}.pdf",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        # veraPDF : code 0 + ligne PASS = conforme ; tout le reste échoue.
        if result.returncode != 0 or not result.stdout.strip().startswith("PASS"):
            raise PdfAValidationError(
                "PDF non conforme PDF/A-3b (veraPDF) :\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
            )
    finally:
        pdf_file.unlink(missing_ok=True)
