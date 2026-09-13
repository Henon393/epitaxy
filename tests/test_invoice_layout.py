"""Mise en page de la facture PDF, testée sur le HTML rendu.

Rapide et sans conteneur : c'est le HTML source du PDF/A qui est vérifié.
Le rendu réel (WeasyPrint, veraPDF, couche texte) reste couvert par
tests/test_facturx.py.
"""

import uuid
from datetime import date
from decimal import Decimal

from app.cii import LEGAL_NOTES
from app.invoice_pdf import render_invoice_html
from app.invoicing import MENTION_293B
from app.models import Invoice, InvoiceLine


def _ligne(
    position: int, designation: str, quantite: str, pu: str, taux: str | None
) -> InvoiceLine:
    quantity = Decimal(quantite)
    unit_price_ht = Decimal(pu)
    return InvoiceLine(
        id=uuid.uuid4(),
        position=position,
        designation=designation,
        quantity=quantity,
        unit_price_ht=unit_price_ht,
        vat_rate=None if taux is None else Decimal(taux),
        total_ht=quantity * unit_price_ht,
    )


def _facture_reel() -> Invoice:
    """Facture au régime réel : deux taux, snapshot vendeur/acheteur complet."""
    return Invoice(
        id=uuid.uuid4(),
        number=42,
        status="emise",
        issue_date=date(2026, 9, 10),
        supply_date=date(2026, 9, 8),
        due_date=date(2026, 10, 10),
        operation_category="mixte",
        vat_on_debits=True,
        delivery_address="8 avenue Jean Jaurès, 69007 Lyon",
        total_ht=Decimal("1113.50"),
        total_tva=Decimal("212.04"),
        total_ttc=Decimal("1325.54"),
        vat_breakdown={
            "5.50": {"base": "73.50", "tva": "4.04"},
            "20.00": {"base": "1040.00", "tva": "208.00"},
        },
        seller_legal_name="Atelier Bertin SAS",
        seller_siren="834521700",
        seller_siret="83452170000007",
        seller_address_line1="12 rue des Lilas",
        seller_address_line2="Zone artisanale des Chênes",
        seller_postal_code="44000",
        seller_city="Nantes",
        seller_country_code="FR",
        seller_vat_number="FR59834521700",
        seller_legal_form="SAS",
        seller_vat_regime="reel_normal",
        vat_mention=None,
        buyer_name="Menuiserie Lambert SARL",
        buyer_siren="902183649",
        buyer_address_line1="8 avenue Jean Jaurès",
        buyer_address_line2="Bâtiment C",
        buyer_postal_code="69007",
        buyer_city="Lyon",
        buyer_country_code="FR",
        lines=[
            _ligne(0, "Prestation de conseil — audit technique", "2", "520.00", "20.00"),
            _ligne(1, "Ouvrage documentaire imprimé", "3", "24.50", "5.50"),
        ],
    )


def _facture_franchise() -> Invoice:
    facture = _facture_reel()
    facture.total_tva = Decimal("0.00")
    facture.total_ttc = facture.total_ht
    facture.vat_breakdown = {}
    facture.seller_vat_number = None
    facture.seller_vat_regime = "franchise_en_base"
    facture.vat_mention = MENTION_293B
    for ligne in facture.lines:
        ligne.vat_rate = None
    return facture


def test_l_entete_identifie_le_vendeur_et_le_document() -> None:
    html = render_invoice_html(_facture_reel())
    assert "FACTURE" in html
    assert "n° 42" in html
    assert "Atelier Bertin SAS" in html
    assert "SAS" in html
    assert "12 rue des Lilas" in html
    assert "44000 Nantes" in html
    assert "SIREN : 834521700" in html
    assert "SIRET : 83452170000007" in html
    assert "TVA intracommunautaire : FR59834521700" in html
    # Dates au format français, prises du snapshot et jamais de l'horloge.
    assert "10/09/2026" in html
    assert "08/09/2026" in html
    assert "10/10/2026" in html


def test_le_bloc_client_est_distinct_et_complet() -> None:
    html = render_invoice_html(_facture_reel())
    assert "Facturé à" in html
    assert "Menuiserie Lambert SARL" in html
    assert "Bâtiment C" in html
    assert "69007 Lyon" in html
    assert "SIREN : 902183649" in html


def test_le_tableau_des_lignes_a_ses_colonnes() -> None:
    html = render_invoice_html(_facture_reel())
    for entete in ("Désignation", "Quantité", "PU HT", "Taux TVA", "Total HT"):
        assert entete in html
    assert "Prestation de conseil — audit technique" in html
    assert "Ouvrage documentaire imprimé" in html
    assert "520.00 €" in html
    assert "20.00 %" in html


def test_quantites_et_prix_unitaires_sont_lisibles() -> None:
    """Les colonnes sont stockées en Numeric(12,3) et (12,4) : sans mise en
    forme, une quantité de 2 s'affiche « 2.000 » et un PU « 450.0000 »."""
    facture = _facture_reel()
    facture.lines[0].quantity = Decimal("2.000")
    facture.lines[0].unit_price_ht = Decimal("450.0000")
    facture.lines[1].quantity = Decimal("1.500")
    # PU réellement au dix-millième : affiché sans perte de précision.
    facture.lines[1].unit_price_ht = Decimal("12.3456")
    html = render_invoice_html(facture)

    assert ">2<" in html
    assert "450.00 €" in html
    assert ">1.5<" in html
    assert "12.3456 €" in html
    assert "2.000" not in html
    assert "450.0000" not in html


def test_les_totaux_et_la_ventilation_sont_mis_en_valeur() -> None:
    html = render_invoice_html(_facture_reel())
    assert "Ventilation de la TVA" in html
    assert "Total HT" in html
    assert "Total TVA" in html
    assert "Total TTC" in html
    # Le TTC porte la classe qui le distingue visuellement des sous-totaux.
    assert 'class="grand"' in html


def test_les_mentions_legales_sont_presentes_et_completes() -> None:
    html = render_invoice_html(_facture_reel())
    assert "Mentions légales" in html
    # Les mêmes textes que les BG-1 du XML : cohérence PDF/XML.
    for _code, texte in LEGAL_NOTES:
        assert texte in html


def test_la_mention_de_franchise_est_rendue_en_evidence() -> None:
    html = render_invoice_html(_facture_franchise())
    assert MENTION_293B in html
    assert 'class="mention"' in html
    # Pas de ventilation sans taux applicable, et un tiret pour le taux.
    assert "Ventilation de la TVA" not in html
    assert "—" in html


def test_les_montants_gardent_le_format_decimal_brut() -> None:
    """Les montants du PDF doivent rester octet pour octet ceux du XML CII :
    pas de séparateur de milliers ni de virgule décimale introduits par la
    mise en page."""
    facture = _facture_reel()
    facture.total_ht = Decimal("1113.50")
    facture.total_ttc = Decimal("1325.54")
    html = render_invoice_html(facture)
    assert "1113.50 €" in html
    assert "1325.54 €" in html
    assert "1 325,54" not in html
