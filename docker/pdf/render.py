"""Rend un HTML en PDF/A avec identifiant épinglé (déterminisme).

Usage : render.py <in.html> <out.pdf> <identifier_hex> [variant]
variant par défaut : pdf/a-3b — jamais A-1 ni A-2, l'embarquement
Factur-X exige un conteneur A-3.
"""

import sys

from weasyprint import HTML


def main() -> None:
    html_path, pdf_path, identifier_hex = sys.argv[1], sys.argv[2], sys.argv[3]
    variant = sys.argv[4] if len(sys.argv) > 4 else "pdf/a-3b"
    HTML(filename=html_path).write_pdf(
        pdf_path,
        # "none" = PDF ordinaire, sans OutputIntent ni pdfaid (utilisé par
        # le test de rejet d'un conteneur non PDF/A).
        pdf_variant=None if variant == "none" else variant,
        # /ID épinglé sur une empreinte stable (sha256 du XML CII) :
        # pas d'identifiant aléatoire dans le fichier.
        pdf_identifier=bytes.fromhex(identifier_hex),
    )


if __name__ == "__main__":
    main()
