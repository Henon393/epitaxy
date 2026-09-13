"""Exemples pré-remplis des formulaires de /docs.

Objectif : pouvoir actionner chaque route sans inventer de valeurs. Les
exemples doivent donc être réalistes, valides et cohérents entre eux.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.invoicing import ALLOWED_VAT_RATES, compute_totals
from app.schemas import EX_BUYER_SIREN, EX_SELLER_SIREN, EX_SELLER_SIRET, EX_SELLER_VAT


def _schema(client: TestClient) -> dict:
    reponse = client.get("/openapi.json")
    assert reponse.status_code == 200
    return reponse.json()


def _exemple(schema: dict, nom: str) -> dict:
    corps = schema["components"]["schemas"][nom]
    # Forme OpenAPI 3.1 (tableau) et forme 3.0 (singulier) : les deux sont
    # émises, elles doivent rester identiques.
    assert corps["examples"][0] == corps["example"], nom
    return corps["examples"][0]


def _luhn_valide(numero: str) -> bool:
    total = 0
    for rang, caractere in enumerate(reversed(numero)):
        chiffre = int(caractere)
        if rang % 2 == 1:
            chiffre *= 2
            if chiffre > 9:
                chiffre -= 9
        total += chiffre
    return total % 10 == 0


def test_chaque_formulaire_d_entree_porte_un_exemple(client: TestClient) -> None:
    schema = _schema(client)
    for nom in (
        "CustomerCreate",
        "CompanyProfileIn",
        "InvoiceCreate",
        "InvoiceLineIn",
        "SignupRequest",
        "LoginRequest",
        "RefreshRequest",
        "UserCreate",
        "MfaVerifyIn",
        "MfaCodeIn",
        "SimulateStatusIn",
    ):
        assert _exemple(schema, nom), nom


def test_les_exemples_ne_sont_plus_generiques(client: TestClient) -> None:
    """Le défaut de Swagger UI (« string », user@example.com, pays aléatoire)
    est ce qu'on cherche justement à faire disparaître."""
    schema = _schema(client)
    client_exemple = _exemple(schema, "CustomerCreate")
    vendeur = _exemple(schema, "CompanyProfileIn")

    assert client_exemple["country_code"] == "FR"
    assert vendeur["country_code"] == "FR"
    assert not client_exemple["email"].endswith("@example.com")
    assert "string" not in {client_exemple["name"], vendeur["legal_name"]}


def test_les_identifiants_d_exemple_sont_structurellement_valides() -> None:
    """SIREN et SIRET à clé de Luhn correcte, TVA à clé française correcte :
    les exemples doivent passer les validations, pas seulement les patterns."""
    assert _luhn_valide(EX_SELLER_SIREN)
    assert _luhn_valide(EX_BUYER_SIREN)
    assert _luhn_valide(EX_SELLER_SIRET)
    assert EX_SELLER_SIRET.startswith(EX_SELLER_SIREN)

    cle = (12 + 3 * (int(EX_SELLER_SIREN) % 97)) % 97
    assert EX_SELLER_VAT == f"FR{cle:02d}{EX_SELLER_SIREN}"


def test_les_exemples_sont_coherents_entre_formulaires(client: TestClient) -> None:
    """Un parcours enchaîné doit tomber juste : les taux de la facture sont
    des taux réels, et le montant encaissé du mock PA est bien son TTC."""
    schema = _schema(client)
    facture = _exemple(schema, "InvoiceCreate")
    encaissement = _exemple(schema, "SimulateStatusIn")

    lignes = [
        (Decimal(ligne["quantity"]), Decimal(ligne["unit_price_ht"]), Decimal(ligne["vat_rate"]))
        for ligne in facture["lines"]
    ]
    assert {taux for _, _, taux in lignes} <= ALLOWED_VAT_RATES
    totaux = compute_totals(lignes)
    assert Decimal(encaissement["paid_amount"]) == totaux.total_ttc
    assert encaissement["paid_at"] == facture["due_date"]
