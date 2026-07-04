"""Assemblage du Factur-X final : PDF/A-3 lisible + XML CII embarqué.

Le PDF lisible est rendu depuis le MÊME snapshot que le XML (cohérence
légale) ; le XML n'est JAMAIS régénéré : c'est l'artefact cii_xml validé en
4b-1 qui est embarqué tel quel.

Quatre contrôles fail-closed avant tout stockage :
1. structure Factur-X (nom factur-x.xml, AFRelationship, MIME, XMP) — angle
   mort de veraPDF, qui valide le conteneur PDF/A mais pas la spec Factur-X ;
2. veraPDF flavour 3b (conteneur JVM) ;
3. round-trip : le XML réextrait du PDF est octet pour octet l'artefact ;
4. (en amont : le XML embarqué a déjà passé XSD + Schematron FR-CTC en 4b-1).
"""

import hashlib
import io
import re
from decimal import Decimal

from facturx import generate_from_binary, get_facturx_xml_from_pdf
from jinja2 import Environment, PackageLoader, select_autoescape
from pypdf import PdfReader

from app.cii import LEGAL_NOTES, SPECIFICATION_ID
from app.models import Invoice
from app.pdf_render import render_pdfa
from app.verapdf import assert_pdfa3b

FACTURX_PDF_KIND = "facturx_pdf"

# Spec Factur-X (FNFE-MPE / FeRD, publication commune 1.09) : le fichier
# embarqué se nomme exactement factur-x.xml ; AFRelationship vaut « Data »
# pour MINIMUM et BASIC WL seulement — pour BASIC, EN 16931 et EXTENDED, le
# XML est une représentation alternative de la facture : « Alternative »
# (valeur exigée notamment côté allemand, défaut « data » de la lib non
# conforme pour notre profil). Vérifié contre la spec, pas contre la lib.
FACTURX_XML_FILENAME = "factur-x.xml"
FACTURX_AFRELATIONSHIP = "alternative"
# fx:ConformanceLevel du XMP pour le profil EN 16931 (BT-24
# urn:cen.eu:en16931:2017).
XMP_CONFORMANCE_LEVEL = "EN 16931"

OPERATION_LABELS = {
    "livraison_de_biens": "Livraison de biens",
    "prestation_de_services": "Prestation de services",
    "mixte": "Mixte (biens et services)",
}

_jinja = Environment(
    loader=PackageLoader("app", "templates"),
    autoescape=select_autoescape(["html"]),
)


class FacturxStructureError(Exception):
    """Structure Factur-X non conforme à la spec FNFE-MPE."""


def render_invoice_html(invoice: Invoice) -> str:
    breakdown = sorted(invoice.vat_breakdown.items(), key=lambda item: Decimal(item[0]))
    return _jinja.get_template("invoice.html.j2").render(
        invoice=invoice,
        breakdown=breakdown,
        operation_label=OPERATION_LABELS.get(
            invoice.operation_category, invoice.operation_category
        ),
        # Les MÊMES notes légales que les BG-1 du XML : cohérence PDF/XML.
        legal_notes=LEGAL_NOTES,
    )


def _assert_facturx_structure(pdf_bytes: bytes, cii_xml: bytes) -> None:
    """Assertions sur la spec Factur-X que veraPDF ne couvre pas."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    catalog = reader.trailer["/Root"]
    try:
        names = catalog["/Names"]["/EmbeddedFiles"]["/Names"]
    except KeyError as exc:
        raise FacturxStructureError("Aucun fichier embarqué dans le PDF.") from exc

    filespec = None
    embedded_names = [str(names[i]) for i in range(0, len(names), 2)]
    for position, embedded_name in enumerate(embedded_names):
        if embedded_name == FACTURX_XML_FILENAME:
            filespec = names[position * 2 + 1].get_object()
    if filespec is None:
        raise FacturxStructureError(
            f"Fichier embarqué {FACTURX_XML_FILENAME!r} introuvable (présents : {embedded_names})."
        )

    afrelationship = str(filespec.get("/AFRelationship", "")).lstrip("/").lower()
    if afrelationship != FACTURX_AFRELATIONSHIP:
        raise FacturxStructureError(
            f"AFRelationship {afrelationship!r} au lieu de "
            f"{FACTURX_AFRELATIONSHIP!r} (spec Factur-X, profil EN 16931)."
        )

    stream = filespec["/EF"]["/F"].get_object()
    subtype = str(stream.get("/Subtype", "")).lstrip("/").replace("#2F", "/").lower()
    if subtype != "text/xml":
        raise FacturxStructureError(f"Type MIME {subtype!r} au lieu de 'text/xml'.")

    xmp = catalog["/Metadata"].get_object().get_data().decode("utf-8", errors="replace")
    match = re.search(r"ConformanceLevel(?:>\s*([^<]+?)\s*<|=\"([^\"]+)\")", xmp)
    conformance = (match.group(1) or match.group(2)).strip() if match else None
    if conformance != XMP_CONFORMANCE_LEVEL:
        raise FacturxStructureError(
            f"fx:ConformanceLevel {conformance!r} au lieu de {XMP_CONFORMANCE_LEVEL!r}."
        )
    # Cohérence XMP <-> BT-24 du XML embarqué.
    if SPECIFICATION_ID.encode() not in cii_xml:
        raise FacturxStructureError(
            "BT-24 du XML embarqué incohérent avec le fx:ConformanceLevel du XMP."
        )


def _assert_round_trip(pdf_bytes: bytes, cii_xml: bytes) -> None:
    filename, extracted = get_facturx_xml_from_pdf(
        io.BytesIO(pdf_bytes), check_xsd=False, check_schematron=False
    )
    if isinstance(extracted, str):
        extracted = extracted.encode("utf-8")
    if extracted != cii_xml:
        raise FacturxStructureError("Le XML réextrait du PDF diffère de l'artefact cii_xml stocké.")


def assemble_facturx(invoice: Invoice, cii_xml: bytes) -> bytes:
    """PDF Factur-X final, quadruple contrôle fail-closed."""
    html = render_invoice_html(invoice)
    base_pdf = render_pdfa(html, identifier_hex=hashlib.sha256(cii_xml).hexdigest())
    facturx_pdf = generate_from_binary(
        base_pdf,
        cii_xml,
        flavor="factur-x",
        level="en16931",
        # Déjà validé en 4b-1 : pas de double validation ici.
        check_xsd=False,
        check_schematron=False,
        afrelationship=FACTURX_AFRELATIONSHIP,
        lang="fr-FR",
        # Métadonnées fixes, dérivées du snapshot : rien de volatil.
        pdf_metadata={
            "author": invoice.seller_legal_name,
            "title": f"Facture {invoice.number}",
            "subject": (
                f"Facture {invoice.number} de {invoice.seller_legal_name} pour {invoice.buyer_name}"
            ),
            "keywords": "Facture, Factur-X",
        },
    )
    _assert_facturx_structure(facturx_pdf, cii_xml)
    assert_pdfa3b(facturx_pdf)
    _assert_round_trip(facturx_pdf, cii_xml)
    return facturx_pdf
