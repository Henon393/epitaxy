"""Tests 4b-2 : Factur-X complet (PDF/A-3 + XML embarqué), veraPDF, structure."""

import io
import re
import uuid

import pytest
from facturx import get_facturx_xml_from_pdf
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import Engine, text

import app.invoice_pdf as invoice_pdf_module
from tests.conftest import set_tenant
from tests.test_cii import LIGNES_MULTI_TAUX, _facture_emise, _post_cii
from tests.test_invoices import PROFILE_FRANCHISE, PROFILE_REEL, _setup


def _post_facturx(client: TestClient, ctx: dict, invoice_id: str):
    return client.post(f"/invoices/{invoice_id}/facturx", headers=ctx["headers"])


def _artifact_count(super_engine: Engine, invoice_id: str, kind: str) -> int:
    with super_engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM invoice_artifacts WHERE invoice_id = :i AND kind = :k"),
            {"i": invoice_id, "k": kind},
        ).scalar_one()


def _texte_du_pdf(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_facturx_complet_reel(client: TestClient, super_engine: Engine) -> None:
    """Un seul parcours réel complet : veraPDF (via le 201), structure
    Factur-X explicite, round-trip octet à octet, couche texte, idempotence."""
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    cii_xml = client.get(f"/invoices/{emise['id']}/cii", headers=ctx["headers"]).content

    # 201 = rendu A-3b + generate_from_binary + structure + veraPDF 3b +
    # round-trip interne, tous passés (pipeline fail-closed).
    reponse = _post_facturx(client, ctx, emise["id"])
    assert reponse.status_code == 201, reponse.text
    assert reponse.headers["content-type"].startswith("application/pdf")
    pdf = reponse.content

    # --- Structure Factur-X, re-vérifiée explicitement ici (angle mort
    # veraPDF : un PDF/A-3 valide peut être un Factur-X non conforme). ---
    reader = PdfReader(io.BytesIO(pdf))
    catalog = reader.trailer["/Root"]
    names = catalog["/Names"]["/EmbeddedFiles"]["/Names"]
    embedded = {str(names[i]): names[i + 1].get_object() for i in range(0, len(names), 2)}
    # Nom exact prescrit par la spec.
    assert list(embedded) == ["factur-x.xml"]
    filespec = embedded["factur-x.xml"]
    # AFRelationship prescrit par la spec FNFE-MPE pour EN 16931 :
    # Alternative (Data n'est admis que pour MINIMUM et BASIC WL).
    assert str(filespec["/AFRelationship"]) == "/Alternative"
    # Type MIME text/xml sur le flux embarqué.
    subtype = str(filespec["/EF"]["/F"].get_object()["/Subtype"])
    assert subtype.lstrip("/").replace("#2F", "/").lower() == "text/xml"
    # XMP : fx:ConformanceLevel EN 16931, cohérent avec le BT-24 du XML.
    xmp = catalog["/Metadata"].get_object().get_data().decode("utf-8", errors="replace")
    match = re.search(r"ConformanceLevel(?:>\s*([^<]+?)\s*<|=\"([^\"]+)\")", xmp)
    assert match is not None
    assert (match.group(1) or match.group(2)).strip() == "EN 16931"
    assert b"urn:cen.eu:en16931:2017" in cii_xml

    # --- Round-trip : XML réextrait == artefact cii_xml, octet pour octet.
    _, extrait = get_facturx_xml_from_pdf(io.BytesIO(pdf), check_xsd=False, check_schematron=False)
    if isinstance(extrait, str):
        extrait = extrait.encode("utf-8")
    assert extrait == cii_xml

    # --- Couche texte : présence des champs clés (pas d'égalité stricte).
    texte = _texte_du_pdf(pdf)
    assert str(emise["number"]) in texte
    assert emise["total_ttc"] in texte  # "216.65"
    assert PROFILE_REEL["legal_name"] in texte  # identité vendeur
    assert "Client SARL" in texte  # identité acheteur
    for entry in emise["vat_breakdown"].values():  # montants de TVA par taux
        assert entry["tva"] in texte

    # --- Idempotence : 200 ensuite, contenu identique, GET sert le même.
    second = _post_facturx(client, ctx, emise["id"])
    assert second.status_code == 200
    assert second.content == pdf
    assert client.get(f"/invoices/{emise['id']}/facturx", headers=ctx["headers"]).content == pdf


def test_facturx_franchise_mention_293b(client: TestClient) -> None:
    ctx = _setup(client, profile=PROFILE_FRANCHISE)
    emise = _facture_emise(
        client, ctx, [{"designation": "Coaching", "quantity": "2", "unit_price_ht": "300.00"}]
    )
    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    reponse = _post_facturx(client, ctx, emise["id"])
    assert reponse.status_code == 201, reponse.text

    texte = _texte_du_pdf(reponse.content)
    assert "293 B" in texte
    assert "TVA non applicable" in texte
    assert PROFILE_FRANCHISE["legal_name"] in texte


def test_facturx_exige_l_artefact_cii(client: TestClient, super_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    reponse = _post_facturx(client, ctx, emise["id"])
    assert reponse.status_code == 409
    assert "cii_xml" in reponse.json()["detail"]
    assert _artifact_count(super_engine, emise["id"], "facturx_pdf") == 0


def test_pdf_de_base_non_pdfa_rejete_sans_artefact(
    client: TestClient, super_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un PDF de base non conforme est rejeté par veraPDF 3b, rien n'est
    stocké.

    Constat empirique documenté (docs/facturx-4b-reference.md) : un corps
    A-2b saboté est « réparé » par generate_from_binary, qui réécrit le XMP
    avec pdfaid:part=3 — or A-3 = A-2 + fichiers embarqués, le résultat est
    donc légitimement conforme A-3b. Le sabotage discriminant est un PDF
    ordinaire, sans OutputIntent : le XMP menteur (part=3) ne suffit pas à
    tromper veraPDF."""
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, emise["id"]).status_code == 201

    reel_render = invoice_pdf_module.render_pdfa

    def render_non_pdfa(html, identifier_hex, variant="pdf/a-3b"):
        return reel_render(html, identifier_hex, variant="none")

    monkeypatch.setattr(invoice_pdf_module, "render_pdfa", render_non_pdfa)

    reponse = _post_facturx(client, ctx, emise["id"])
    assert reponse.status_code == 422
    assert "PDF/A-3b" in reponse.json()["detail"]
    assert _artifact_count(super_engine, emise["id"], "facturx_pdf") == 0
    assert client.get(f"/invoices/{emise['id']}/facturx", headers=ctx["headers"]).status_code == 404


def test_facturx_brouillon_409(client: TestClient) -> None:
    ctx = _setup(client)
    from tests.test_invoices import _draft

    brouillon = _draft(client, ctx, lines=LIGNES_MULTI_TAUX)
    assert _post_facturx(client, ctx, brouillon["id"]).status_code == 409


def test_facturx_append_only(client: TestClient, app_engine: Engine) -> None:
    # Le régime append-only est celui de la table entière ; on vérifie que le
    # kind facturx_pdf y est bien soumis.
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    assert _post_facturx(client, ctx, emise["id"]).status_code == 201

    from sqlalchemy.exc import ProgrammingError

    with app_engine.connect() as conn:
        set_tenant(conn, uuid.UUID(ctx["tenant_id"]))
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM invoice_artifacts WHERE kind = 'facturx_pdf'"))
