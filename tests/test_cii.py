"""Tests 4b-1 : XML CII EN 16931 validé XSD + Schematron FR-CTC."""

import hashlib
import uuid

import pytest
from facturx import generate_cii_xml
from fastapi.testclient import TestClient
from lxml import etree
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

import app.cii as cii_module
from app.cii import CiiValidationError, generate_and_validate
from app.db import session_for_tenant
from app.models import Invoice
from tests.conftest import set_tenant
from tests.test_invoices import (
    PROFILE_FRANCHISE,
    _draft,
    _issue,
    _setup,
)

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
}

LIGNES_MULTI_TAUX = [
    {"designation": "A", "quantity": "1", "unit_price_ht": "100.00", "vat_rate": "20.00"},
    {"designation": "B", "quantity": "1", "unit_price_ht": "50.00", "vat_rate": "10.00"},
    {"designation": "C", "quantity": "1", "unit_price_ht": "30.00", "vat_rate": "5.50"},
    {"designation": "D", "quantity": "1", "unit_price_ht": "10.00", "vat_rate": "0.00"},
]


def _facture_emise(client: TestClient, ctx: dict, lines: list[dict]) -> dict:
    facture = _draft(client, ctx, lines=lines)
    emise = _issue(client, ctx, facture["id"])
    assert emise.status_code == 200, emise.text
    return emise.json()


def _post_cii(client: TestClient, ctx: dict, invoice_id: str):
    return client.post(f"/invoices/{invoice_id}/cii", headers=ctx["headers"])


def _xpath(root, expression: str) -> list[str]:
    return [node.text for node in root.xpath(expression, namespaces=NS)]


def test_reel_multi_taux_xml_valide_categories_s_et_z(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)

    # Le 201 prouve à lui seul XSD + Schematron FR-CTC verts (validation
    # fail-closed AVANT stockage).
    reponse = _post_cii(client, ctx, emise["id"])
    assert reponse.status_code == 201, reponse.text
    assert reponse.headers["content-type"].startswith("application/xml")

    root = etree.fromstring(reponse.content)
    # BT-24 : profil EN 16931.
    assert _xpath(
        root,
        "//ram:GuidelineSpecifiedDocumentContextParameter/ram:ID",
    ) == ["urn:cen.eu:en16931:2017"]
    # Catégories de ligne (BT-151) : S pour 20/10/5.5, Z pour la ligne à 0 %.
    categories_lignes = _xpath(
        root,
        "//ram:IncludedSupplyChainTradeLineItem//ram:ApplicableTradeTax/ram:CategoryCode",
    )
    assert categories_lignes == ["S", "S", "S", "Z"]
    # Ventilation (BG-23) : S aux trois taux + Z, jamais de E au régime réel.
    ventilation = root.xpath(
        "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", namespaces=NS
    )
    categories = {
        (
            v.findtext("ram:CategoryCode", namespaces=NS),
            v.findtext("ram:RateApplicablePercent", namespaces=NS),
        )
        for v in ventilation
    }
    assert categories == {("S", "20.00"), ("S", "10.00"), ("S", "5.50"), ("Z", "0.00")}


def test_franchise_xml_valide_categorie_e_et_mention(client: TestClient) -> None:
    ctx = _setup(client, profile=PROFILE_FRANCHISE)
    emise = _facture_emise(
        client,
        ctx,
        [{"designation": "Coaching", "quantity": "2", "unit_price_ht": "300.00"}],
    )
    reponse = _post_cii(client, ctx, emise["id"])
    assert reponse.status_code == 201, reponse.text

    root = etree.fromstring(reponse.content)
    assert _xpath(
        root, "//ram:IncludedSupplyChainTradeLineItem//ram:ApplicableTradeTax/ram:CategoryCode"
    ) == ["E"]
    ventilation = root.xpath(
        "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", namespaces=NS
    )
    assert len(ventilation) == 1
    assert ventilation[0].findtext("ram:CategoryCode", namespaces=NS) == "E"
    # Mention 293 B en motif d'exonération (BT-120).
    assert (
        ventilation[0].findtext("ram:ExemptionReason", namespaces=NS)
        == "TVA non applicable, art. 293 B du CGI"
    )
    # Constat empirique BR-E-02 : sans BT-31, le SIREN sert d'enregistrement
    # fiscal vendeur (BT-32, schemeID FC).
    seller = root.xpath("//ram:SellerTradeParty", namespaces=NS)[0]
    registrations = {
        n.get("schemeID"): n.text
        for n in seller.xpath("ram:SpecifiedTaxRegistration/ram:ID", namespaces=NS)
    }
    assert registrations == {"FC": PROFILE_FRANCHISE["siren"]}


def test_totaux_xml_egaux_au_snapshot(client: TestClient) -> None:
    """BR-CO-14/15 : les montants du XML sont ceux du snapshot 4a, sans
    recalcul divergent, vérifié textuellement en plus du Schematron."""
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    root = etree.fromstring(_post_cii(client, ctx, emise["id"]).content)

    somme = root.xpath("//ram:SpecifiedTradeSettlementHeaderMonetarySummation", namespaces=NS)[0]
    assert somme.findtext("ram:LineTotalAmount", namespaces=NS) == emise["total_ht"]
    assert somme.findtext("ram:TaxBasisTotalAmount", namespaces=NS) == emise["total_ht"]
    assert somme.findtext("ram:TaxTotalAmount", namespaces=NS) == emise["total_tva"]
    assert somme.findtext("ram:GrandTotalAmount", namespaces=NS) == emise["total_ttc"]
    # Ventilation XML = vat_breakdown du snapshot, entrée par entrée.
    for v in root.xpath(
        "//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", namespaces=NS
    ):
        taux = v.findtext("ram:RateApplicablePercent", namespaces=NS)
        assert v.findtext("ram:BasisAmount", namespaces=NS) == emise["vat_breakdown"][taux]["base"]
        assert (
            v.findtext("ram:CalculatedAmount", namespaces=NS) == emise["vat_breakdown"][taux]["tva"]
        )


def test_bt130_et_bt131_presents(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_emise(
        client,
        ctx,
        [{"designation": "A", "quantity": "3", "unit_price_ht": "10.00", "vat_rate": "20.00"}],
    )
    root = etree.fromstring(_post_cii(client, ctx, emise["id"]).content)
    quantites = root.xpath("//ram:BilledQuantity", namespaces=NS)
    assert [q.get("unitCode") for q in quantites] == ["C62"]  # BT-130 par défaut
    # BT-131 = total_ht de la ligne calculé en 4a.
    assert _xpath(
        root, "//ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount"
    ) == ["30.00"]


def test_generation_deterministe(client: TestClient) -> None:
    """Même snapshot => mêmes octets, à chaque génération (reproductibilité,
    archivage probant). Distinct de l'idempotence de l'endpoint : ici on
    régénère vraiment deux fois."""
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)

    with session_for_tenant(uuid.UUID(ctx["tenant_id"])) as session:
        invoice = session.get(Invoice, uuid.UUID(emise["id"]))
        dict_1 = cii_module.build_en16931_dict(invoice)
        xml_1 = generate_cii_xml(dict_1, level="en16931", check_xsd=False, check_schematron=False)
        dict_2 = cii_module.build_en16931_dict(invoice)
        xml_2 = generate_cii_xml(dict_2, level="en16931", check_xsd=False, check_schematron=False)
    assert dict_1 == dict_2
    assert xml_1 == xml_2


def test_incoherence_rejetee_sans_artefact(
    client: TestClient, super_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)

    reel_build = cii_module.build_en16931_dict

    def build_corrompu(invoice):
        data = reel_build(invoice)
        data["BT-112"] = "999999.99"  # total TTC incohérent avec HT + TVA
        return data

    monkeypatch.setattr(cii_module, "build_en16931_dict", build_corrompu)

    # Service : l'erreur porte le failed-assert BR-CO-15, pas avalé.
    with session_for_tenant(uuid.UUID(ctx["tenant_id"])) as session:
        invoice = session.get(Invoice, uuid.UUID(emise["id"]))
        with pytest.raises(CiiValidationError, match="BR-CO-15"):
            generate_and_validate(invoice)

    # Endpoint : 422 avec le rapport, et AUCUN artefact produit.
    reponse = _post_cii(client, ctx, emise["id"])
    assert reponse.status_code == 422
    assert "BR-CO-15" in reponse.json()["detail"]
    with super_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM invoice_artifacts WHERE invoice_id = :i"),
            {"i": emise["id"]},
        ).scalar_one()
    assert count == 0
    assert client.get(f"/invoices/{emise['id']}/cii", headers=ctx["headers"]).status_code == 404


def test_generation_hors_transaction_emission_et_idempotence(
    client: TestClient, super_engine: Engine
) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)

    # L'émission n'a produit AUCUN artefact : la génération est bien hors
    # de la transaction d'émission.
    with super_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM invoice_artifacts WHERE invoice_id = :i"),
            {"i": emise["id"]},
        ).scalar_one()
    assert count == 0

    premier = _post_cii(client, ctx, emise["id"])
    assert premier.status_code == 201
    second = _post_cii(client, ctx, emise["id"])
    assert second.status_code == 200
    assert hashlib.sha256(second.content).hexdigest() == hashlib.sha256(premier.content).hexdigest()
    # Et le GET sert le même contenu.
    lu = client.get(f"/invoices/{emise['id']}/cii", headers=ctx["headers"])
    assert lu.status_code == 200
    assert lu.content == premier.content


def test_brouillon_sans_cii(client: TestClient) -> None:
    ctx = _setup(client)
    brouillon = _draft(client, ctx, lines=LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, brouillon["id"]).status_code == 409
    assert client.get(f"/invoices/{brouillon['id']}/cii", headers=ctx["headers"]).status_code == 404


def test_artifacts_append_only_et_isoles(client: TestClient, app_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    tenant_id = uuid.UUID(ctx["tenant_id"])

    with app_engine.connect() as conn:
        set_tenant(conn, tenant_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE invoice_artifacts SET content = 'falsifie'"))
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM invoice_artifacts"))
        conn.rollback()

        # Isolation : un autre tenant ne voit rien.
        autre_tenant = uuid.uuid4()
        set_tenant(conn, autre_tenant)
        rows = conn.execute(text("SELECT id FROM invoice_artifacts")).fetchall()
        assert rows == []
