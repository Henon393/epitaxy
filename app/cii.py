"""Génération du XML CII EN 16931 depuis le snapshot d'une facture émise.

Lecture EXCLUSIVE des colonnes de snapshot (seller_*, buyer_*, totaux,
vat_breakdown, lignes) : jamais customers ni company_profiles — la facture
émise est autoportante (étape 4a).

Aucun recalcul côté XML : les montants sortent du snapshot tels quels,
BR-CO-14/BR-CO-15 sont cohérentes par construction.

Validation systématique et fail-closed : XSD puis Schematron FR-CTC. Tout
échec lève CiiValidationError avec le rapport failed-assert intact — aucun
artefact n'est produit en aval.
"""

from decimal import Decimal
from typing import Any

# Import dur : sans saxonche, factur-x SAUTE la validation Schematron en
# silence (return True). On refuse de démarrer plutôt que de valider à vide.
import saxonche  # noqa: F401
from facturx import generate_cii_xml, xml_check_schematron, xml_check_xsd

from app.models import Invoice, InvoiceLine

# BT-24 — profil EN 16931.
SPECIFICATION_ID = "urn:cen.eu:en16931:2017"
# BT-23 — mode de facturation. BR-FR-08 n'accepte que B1, S1, M1, B2, S2,
# M2, B4, S4, M4, S5, S6, B7, S7 (constaté au Schematron : « A1 » rejeté).
# B1 = dépôt de facture B2B domestique.
BUSINESS_PROCESS = "B1"
# BT-130 — le modèle 4a n'a pas d'unité : C62 (unité générique UN/ECE Rec 20).
DEFAULT_UNIT_CODE = "C62"
# BT-30-1 / BT-47-1 — schéma ICD 0002 = SIREN.
SIREN_SCHEME_ID = "0002"
# BT-34-1 / BT-49-1 — schéma EAS 0002 = SIREN : BR-FR-13 et BR-FR-12 imposent
# une adresse électronique vendeur ET acheteur (constaté au Schematron) ;
# le SIREN sert d'adresse de routage, pratique standard du dispositif FR.
ELECTRONIC_ADDRESS_SCHEME = "0002"

# BR-FR-05 : trois mentions obligatoires dans les notes BG-1, chacune sous
# son code BT-21 (constaté au Schematron). Textes par défaut du dispositif
# légal français ; TODO(config) : les rendre paramétrables par profil
# vendeur quand le besoin apparaîtra.
LEGAL_NOTES = [
    # AAB — conditions d'escompte (ou son absence).
    ("AAB", "Pas d'escompte pour paiement anticipé."),
    # PMD — pénalités de retard.
    ("PMD", "Pénalités de retard : trois fois le taux d'intérêt légal."),
    # PMT — indemnité forfaitaire de recouvrement.
    (
        "PMT",
        "Indemnité forfaitaire pour frais de recouvrement en cas de retard de paiement : 40 EUR.",
    ),
]

CII_XML_KIND = "cii_xml"


class CiiValidationError(Exception):
    """Validation XSD ou Schematron échouée ; str(exc) = rapport intact."""


def _amount(value: Decimal | str) -> str:
    return f"{Decimal(value):.2f}"


def vat_category(rate: Decimal | None) -> tuple[str, str]:
    """Catégorie EN 16931 et taux (BT-151/BT-152, BT-118/BT-119).

    Distinction stricte héritée de 4a, testée sur ``is None`` :
    - None (franchise en base)     -> E, taux 0 (BR-E-05)
    - Decimal("0") au régime réel  -> Z, taux 0 (BR-Z-05)
    - sinon                        -> S, taux réel
    """
    if rate is None:
        return "E", "0.00"
    if rate == Decimal("0"):
        return "Z", "0.00"
    return "S", f"{Decimal(rate):.2f}"


def _line_dict(line: InvoiceLine) -> dict[str, Any]:
    category, rate_str = vat_category(line.vat_rate)
    return {
        "BT-126": str(line.position + 1),
        "BT-153": line.designation,
        "BT-146": str(line.unit_price_ht),
        "BT-129": str(line.quantity),
        "BT-130": DEFAULT_UNIT_CODE,
        "BT-131": _amount(line.total_ht),
        "BT-151": category,
        "BT-152": rate_str,
    }


def _tax_breakdown(invoice: Invoice) -> list[dict[str, Any]]:
    if invoice.seller_vat_regime == "franchise_en_base":
        # Franchise : ventilation unique en catégorie E, base = total HT,
        # motif BT-120 = mention 293 B figée au snapshot.
        return [
            {
                "BT-116": _amount(invoice.total_ht),
                "BT-117": "0.00",
                "BT-118": "E",
                "BT-119": "0.00",
                "BT-120": invoice.vat_mention,
            }
        ]
    breakdown = []
    # Tri par taux croissant : ordre stable pour un XML déterministe.
    for rate_key in sorted(invoice.vat_breakdown, key=Decimal):
        entry = invoice.vat_breakdown[rate_key]
        category, rate_str = vat_category(Decimal(rate_key))
        breakdown.append(
            {
                "BT-116": _amount(entry["base"]),
                "BT-117": _amount(entry["tva"]),
                "BT-118": category,
                "BT-119": rate_str,
            }
        )
    return breakdown


def build_en16931_dict(invoice: Invoice) -> dict[str, Any]:
    """Dict EN 16931 (clés BT/BG de la lib factur-x) depuis le seul snapshot."""
    if invoice.status != "emise":
        raise ValueError("Seule une facture émise a un snapshot complet.")

    data: dict[str, Any] = {
        "BT-1": str(invoice.number),
        "BT-2": invoice.issue_date,
        "BT-3": "380",
        "BT-5": "EUR",
        "BT-23": BUSINESS_PROCESS,
        "BT-24": SPECIFICATION_ID,
        # BR-FR-05 : notes légales obligatoires (escompte, pénalités,
        # indemnité de recouvrement).
        "BG-1": [{"BT-21": code, "BT-22": texte} for code, texte in LEGAL_NOTES],
        # --- Vendeur (BG-4 / BG-5), snapshot uniquement ---
        "BT-27": invoice.seller_legal_name,
        "BT-30": invoice.seller_siren,
        "BT-30-1": SIREN_SCHEME_ID,
        "BT-31": invoice.seller_vat_number,
        # BR-E-02 : une facture avec des lignes en catégorie E exige BT-31,
        # BT-32 ou BT-63. En franchise, pas de BT-31 : le SIREN sert
        # d'enregistrement fiscal vendeur (BT-32, schemeID FC via la lib).
        "BT-32": invoice.seller_siren if invoice.seller_vat_number is None else None,
        "BT-33": invoice.seller_legal_form,
        "BT-34": invoice.seller_siren,
        "BT-34-1": ELECTRONIC_ADDRESS_SCHEME,
        "BT-35": invoice.seller_address_line1,
        "BT-36": invoice.seller_address_line2,
        "BT-37": invoice.seller_city,
        "BT-38": invoice.seller_postal_code,
        "BT-40": invoice.seller_country_code,
        # --- Acheteur (BG-7 / BG-8), snapshot uniquement ---
        "BT-44": invoice.buyer_name,
        "BT-47": invoice.buyer_siren,
        "BT-47-1": SIREN_SCHEME_ID,
        "BT-49": invoice.buyer_siren,
        "BT-49-1": ELECTRONIC_ADDRESS_SCHEME,
        "BT-50": invoice.buyer_address_line1,
        "BT-51": invoice.buyer_address_line2,
        "BT-52": invoice.buyer_city,
        "BT-53": invoice.buyer_postal_code,
        "BT-55": invoice.buyer_country_code,
        # --- Ventilation et totaux : snapshot tel quel, zéro recalcul ---
        "BG-23": _tax_breakdown(invoice),
        "BT-106": _amount(invoice.total_ht),
        "BT-109": _amount(invoice.total_ht),
        "BT-111": _amount(invoice.total_tva),
        "BT-111-1": "EUR",
        "BT-112": _amount(invoice.total_ttc),
        "BT-115": _amount(invoice.total_ttc),
        # --- Lignes ---
        "BG-25": [_line_dict(line) for line in invoice.lines],
    }
    if invoice.due_date is not None:
        data["BT-9"] = invoice.due_date
    # BT-72 systématique (constaté au XSD) : la lib émet toujours
    # ApplicableHeaderTradeDelivery et le XSD rejette l'élément vide. Sans
    # date de livraison saisie, la prestation est réputée du jour d'émission.
    data["BT-72"] = invoice.supply_date or invoice.issue_date
    # Champs à None retirés : la lib teste get(), autant garder un dict net.
    return {key: value for key, value in data.items() if value is not None}


def generate_and_validate(invoice: Invoice) -> bytes:
    """XML CII validé XSD + Schematron FR-CTC, ou CiiValidationError."""
    data = build_en16931_dict(invoice)
    # Les validations de la lib sont désactivées ici pour être exécutées
    # nous-mêmes juste après : surface d'erreur contrôlée, rapport intact.
    xml_bytes = generate_cii_xml(data, level="en16931", check_xsd=False, check_schematron=False)
    try:
        xml_check_xsd(xml_bytes, flavor="factur-x", level="en16931")
        xml_check_schematron(xml_bytes, flavor="factur-x", level="en16931", check_option="fr-ctc")
    except Exception as exc:
        raise CiiValidationError(str(exc)) from exc
    return xml_bytes
