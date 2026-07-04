"""Tests facturation : numérotation, immuabilité, TVA, snapshot, mentions."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

import app.routers.invoices as invoices_module
from app.main import app
from tests.conftest import bearer, set_tenant, signup_tenant
from tests.test_audit import _audit_rows

PROFILE_REEL = {
    "legal_name": "ACME SAS",
    "siren": "123456789",
    "address_line1": "1 rue de la Paix",
    "postal_code": "75001",
    "city": "Paris",
    "country_code": "FR",
    "vat_number": "FR32123456789",
    "legal_form": "SAS",
    "vat_regime": "reel_normal",
}
PROFILE_FRANCHISE = {
    "legal_name": "Jean Dupont EI",
    "siren": "111222333",
    "address_line1": "3 impasse des Lilas",
    "postal_code": "33000",
    "city": "Bordeaux",
    "country_code": "FR",
    "legal_form": "EI",
    "vat_regime": "franchise_en_base",
}
CUSTOMER_COMPLET = {
    "name": "Client SARL",
    "email": "client@exemple.fr",
    "siren": "987654321",
    "address_line1": "2 avenue de la République",
    "postal_code": "69001",
    "city": "Lyon",
    "country_code": "FR",
}
LIGNE_20 = {
    "designation": "Prestation",
    "quantity": "1",
    "unit_price_ht": "100.00",
    "vat_rate": "20.00",
}


def _setup(
    client: TestClient, profile: dict | None = PROFILE_REEL, customer: dict = CUSTOMER_COMPLET
) -> dict:
    s = signup_tenant(client)
    headers = bearer(s["access_token"])
    if profile is not None:
        assert client.put("/company-profile", headers=headers, json=profile).status_code == 200
    created = client.post("/customers", headers=headers, json=customer)
    assert created.status_code == 201, created.text
    return {**s, "headers": headers, "customer_id": created.json()["id"]}


def _draft(client: TestClient, ctx: dict, lines: list[dict] | None = None, **overrides) -> dict:
    body = {
        "customer_id": ctx["customer_id"],
        "operation_category": "prestation_de_services",
        "lines": lines if lines is not None else [LIGNE_20],
        **overrides,
    }
    response = client.post("/invoices", headers=ctx["headers"], json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _issue(client: TestClient, ctx: dict, invoice_id: str):
    return client.post(f"/invoices/{invoice_id}/issue", headers=ctx["headers"])


def _dec(value) -> Decimal:
    return Decimal(str(value))


# --- TVA : totaux, ventilation, arrondis, 0 % vs sans taux -------------------


def test_totaux_multi_taux(client: TestClient) -> None:
    ctx = _setup(client)
    facture = _draft(
        client,
        ctx,
        lines=[
            {"designation": "A", "quantity": "1", "unit_price_ht": "100.00", "vat_rate": "20.00"},
            {"designation": "B", "quantity": "1", "unit_price_ht": "50.00", "vat_rate": "10.00"},
            {"designation": "C", "quantity": "1", "unit_price_ht": "30.00", "vat_rate": "5.50"},
            {"designation": "D", "quantity": "1", "unit_price_ht": "10.00", "vat_rate": "0.00"},
        ],
    )
    assert facture["status"] == "brouillon"
    assert facture["number"] is None
    assert _dec(facture["total_ht"]) == Decimal("190.00")
    assert _dec(facture["total_tva"]) == Decimal("26.65")
    assert _dec(facture["total_ttc"]) == Decimal("216.65")
    assert facture["vat_breakdown"]["20.00"] == {"base": "100.00", "tva": "20.00"}
    assert facture["vat_breakdown"]["10.00"] == {"base": "50.00", "tva": "5.00"}
    assert facture["vat_breakdown"]["5.50"] == {"base": "30.00", "tva": "1.65"}
    # Le taux zéro EST une ventilation (base déclarée, TVA nulle) —
    # à ne jamais confondre avec l'absence de taux de la franchise.
    assert facture["vat_breakdown"]["0.00"] == {"base": "10.00", "tva": "0.00"}


def test_arrondi_half_up_et_par_taux(client: TestClient) -> None:
    ctx = _setup(client)
    # 0.345 → 0.35 : half-up (half-even donnerait 0.34).
    facture = _draft(
        client,
        ctx,
        lines=[
            {"designation": "H", "quantity": "1", "unit_price_ht": "0.345", "vat_rate": "20.00"}
        ],
    )
    assert _dec(facture["total_ht"]) == Decimal("0.35")

    # 3 × 1.01 à 5.5 % : arrondi PAR TAUX = 3.03 × 5.5 % = 0.17 ;
    # un arrondi par ligne donnerait 3 × 0.06 = 0.18.
    ligne = {"designation": "L", "quantity": "1", "unit_price_ht": "1.01", "vat_rate": "5.50"}
    facture = _draft(client, ctx, lines=[ligne, ligne, ligne])
    assert _dec(facture["total_tva"]) == Decimal("0.17")
    assert facture["vat_breakdown"]["5.50"] == {"base": "3.03", "tva": "0.17"}


def test_taux_inconnu_rejete(client: TestClient) -> None:
    ctx = _setup(client)
    reponse = client.post(
        "/invoices",
        headers=ctx["headers"],
        json={
            "customer_id": ctx["customer_id"],
            "operation_category": "prestation_de_services",
            "lines": [
                {"designation": "X", "quantity": "1", "unit_price_ht": "10", "vat_rate": "19.60"}
            ],
        },
    )
    assert reponse.status_code == 422


# --- Numérotation -------------------------------------------------------------


def test_numerotation_sequentielle_sans_trou(client: TestClient) -> None:
    ctx = _setup(client)
    numeros = []
    for _ in range(3):
        facture = _draft(client, ctx)
        emise = _issue(client, ctx, facture["id"])
        assert emise.status_code == 200, emise.text
        numeros.append(emise.json()["number"])
    assert numeros == [1, 2, 3]


def test_emission_echouee_ne_cree_pas_de_trou(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _setup(client)
    premiere = _draft(client, ctx)
    seconde = _draft(client, ctx)

    assert _issue(client, ctx, premiere["id"]).json()["number"] == 1

    # Émission sabotée après la prise du numéro : rollback complet, le
    # numéro 2 est rendu au compteur.
    def record_saboteur(*args, **kwargs):
        raise RuntimeError("audit indisponible")

    monkeypatch.setattr(invoices_module, "record", record_saboteur)
    sans_exception = TestClient(app, raise_server_exceptions=False)
    echec = sans_exception.post(f"/invoices/{seconde['id']}/issue", headers=ctx["headers"])
    assert echec.status_code == 500
    monkeypatch.undo()

    # La facture est restée brouillon et l'émission suivante reprend le
    # numéro rendu : pas de trou.
    assert (
        client.get(f"/invoices/{seconde['id']}", headers=ctx["headers"]).json()["status"]
        == "brouillon"
    )
    assert _issue(client, ctx, seconde["id"]).json()["number"] == 2


def test_emissions_concurrentes_sans_trou_ni_doublon(client: TestClient) -> None:
    ctx = _setup(client)
    brouillons = [_draft(client, ctx)["id"] for _ in range(6)]

    def emettre(invoice_id: str):
        return TestClient(app).post(f"/invoices/{invoice_id}/issue", headers=ctx["headers"])

    with ThreadPoolExecutor(max_workers=3) as pool:
        reponses = list(pool.map(emettre, brouillons))

    assert all(r.status_code == 200 for r in reponses), [r.status_code for r in reponses]
    numeros = sorted(r.json()["number"] for r in reponses)
    assert numeros == [1, 2, 3, 4, 5, 6]


# --- Immuabilité ---------------------------------------------------------------


def _facture_emise(client: TestClient, ctx: dict) -> dict:
    facture = _draft(client, ctx)
    emise = _issue(client, ctx, facture["id"])
    assert emise.status_code == 200, emise.text
    return emise.json()


def test_facture_emise_immuable_via_api(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx)

    modification = client.put(
        f"/invoices/{emise['id']}",
        headers=ctx["headers"],
        json={
            "customer_id": ctx["customer_id"],
            "operation_category": "mixte",
            "lines": [LIGNE_20],
        },
    )
    assert modification.status_code == 409
    assert client.delete(f"/invoices/{emise['id']}", headers=ctx["headers"]).status_code == 409


def test_facture_emise_immuable_en_sql_direct(client: TestClient, app_engine: Engine) -> None:
    """La barrière base : les triggers, hors de portée d'app_user."""
    ctx = _setup(client)
    emise = _facture_emise(client, ctx)
    tenant_id = uuid.UUID(ctx["tenant_id"])
    ligne_id = emise["lines"][0]["id"]

    with app_engine.connect() as conn:
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="immuable"):
            conn.execute(
                text("UPDATE invoices SET buyer_name = 'falsifie' WHERE id = :id"),
                {"id": emise["id"]},
            )
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="immuable"):
            conn.execute(
                text("UPDATE invoices SET number = 999 WHERE id = :id"), {"id": emise["id"]}
            )
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="immuable"):
            conn.execute(text("DELETE FROM invoices WHERE id = :id"), {"id": emise["id"]})
        conn.rollback()

        # Les lignes : modification, suppression, ajout — tout est refusé.
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="intouchables"):
            conn.execute(
                text("UPDATE invoice_lines SET designation = 'falsifie' WHERE id = :id"),
                {"id": ligne_id},
            )
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="intouchables"):
            conn.execute(text("DELETE FROM invoice_lines WHERE id = :id"), {"id": ligne_id})
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="intouchables"):
            conn.execute(
                text(
                    "INSERT INTO invoice_lines (id, tenant_id, invoice_id, position, "
                    "designation, quantity, unit_price_ht, total_ht) "
                    "VALUES (gen_random_uuid(), :t, :f, 99, 'intrus', 1, 1, 1)"
                ),
                {"t": tenant_id, "f": emise["id"]},
            )


# --- Snapshot ------------------------------------------------------------------


def test_snapshot_acheteur_fige_apres_emission(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx)
    assert emise["buyer_name"] == CUSTOMER_COMPLET["name"]
    assert emise["buyer_siren"] == CUSTOMER_COMPLET["siren"]

    # Le customer déménage et change de nom après l'émission.
    modification = client.put(
        f"/customers/{ctx['customer_id']}",
        headers=ctx["headers"],
        json={
            **CUSTOMER_COMPLET,
            "name": "Client Renomme",
            "address_line1": "99 nouvelle adresse",
            "postal_code": "13001",
            "city": "Marseille",
        },
    )
    assert modification.status_code == 200

    relue = client.get(f"/invoices/{emise['id']}", headers=ctx["headers"]).json()
    assert relue["buyer_name"] == CUSTOMER_COMPLET["name"]
    assert relue["buyer_address_line1"] == CUSTOMER_COMPLET["address_line1"]
    assert relue["buyer_postal_code"] == CUSTOMER_COMPLET["postal_code"]
    assert relue["buyer_city"] == CUSTOMER_COMPLET["city"]
    assert relue["buyer_country_code"] == CUSTOMER_COMPLET["country_code"]


def test_snapshot_vendeur_fige_apres_emission(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx)
    assert emise["seller_legal_name"] == PROFILE_REEL["legal_name"]
    assert emise["seller_vat_number"] == PROFILE_REEL["vat_number"]

    nouveau_profil = {
        **PROFILE_REEL,
        "legal_name": "ACME Renommee SAS",
        "address_line1": "8 boulevard Haussmann",
        "city": "Nanterre",
    }
    assert (
        client.put("/company-profile", headers=ctx["headers"], json=nouveau_profil).status_code
        == 200
    )

    relue = client.get(f"/invoices/{emise['id']}", headers=ctx["headers"]).json()
    assert relue["seller_legal_name"] == PROFILE_REEL["legal_name"]
    assert relue["seller_address_line1"] == PROFILE_REEL["address_line1"]
    assert relue["seller_city"] == PROFILE_REEL["city"]
    assert relue["seller_country_code"] == PROFILE_REEL["country_code"]


# --- Régime de TVA ---------------------------------------------------------------


def test_franchise_en_base_emission_complete(client: TestClient) -> None:
    ctx = _setup(client, profile=PROFILE_FRANCHISE)
    facture = _draft(
        client,
        ctx,
        lines=[
            {"designation": "Coaching", "quantity": "2", "unit_price_ht": "300.00"},
            {"designation": "Support", "quantity": "1", "unit_price_ht": "150.00"},
        ],
    )
    emise = _issue(client, ctx, facture["id"])
    assert emise.status_code == 200, emise.text
    corps = emise.json()
    assert _dec(corps["total_tva"]) == Decimal("0.00")
    assert _dec(corps["total_ttc"]) == _dec(corps["total_ht"]) == Decimal("750.00")
    # Pas de taux → AUCUNE ventilation (≠ taux zéro qui en aurait une).
    assert corps["vat_breakdown"] == {}
    assert corps["vat_mention"] == "TVA non applicable, art. 293 B du CGI"
    assert corps["seller_vat_regime"] == "franchise_en_base"
    assert corps["seller_vat_number"] is None

    # Le passage ultérieur au réel ne réécrit pas l'histoire.
    assert (
        client.put("/company-profile", headers=ctx["headers"], json=PROFILE_REEL).status_code == 200
    )
    relue = client.get(f"/invoices/{corps['id']}", headers=ctx["headers"]).json()
    assert relue["vat_mention"] == "TVA non applicable, art. 293 B du CGI"
    assert relue["seller_vat_regime"] == "franchise_en_base"


def test_franchise_refuse_les_lignes_avec_taux(client: TestClient) -> None:
    ctx = _setup(client, profile=PROFILE_FRANCHISE)
    facture = _draft(client, ctx, lines=[LIGNE_20])
    reponse = _issue(client, ctx, facture["id"])
    assert reponse.status_code == 422
    assert "franchise" in reponse.json()["detail"]


def test_reel_refuse_les_lignes_sans_taux(client: TestClient) -> None:
    ctx = _setup(client)
    facture = _draft(
        client, ctx, lines=[{"designation": "X", "quantity": "1", "unit_price_ht": "10.00"}]
    )
    reponse = _issue(client, ctx, facture["id"])
    assert reponse.status_code == 422
    assert "réel" in reponse.json()["detail"]


def test_vat_number_requis_au_reel_optionnel_en_franchise(client: TestClient) -> None:
    s = signup_tenant(client)
    headers = bearer(s["access_token"])
    sans_tva = {k: v for k, v in PROFILE_REEL.items() if k != "vat_number"}
    assert client.put("/company-profile", headers=headers, json=sans_tva).status_code == 422
    # Le même profil passe en franchise.
    assert (
        client.put(
            "/company-profile",
            headers=headers,
            json={**sans_tva, "vat_regime": "franchise_en_base"},
        ).status_code
        == 200
    )


# --- Mentions obligatoires et validations ---------------------------------------


def test_emission_refusee_sans_profil_vendeur(client: TestClient) -> None:
    ctx = _setup(client, profile=None)
    facture = _draft(client, ctx)
    reponse = _issue(client, ctx, facture["id"])
    assert reponse.status_code == 422
    assert "Profil entreprise" in reponse.json()["detail"]


def test_emission_refusee_customer_incomplet_puis_acceptee(client: TestClient) -> None:
    ctx = _setup(client, customer={"name": "Incomplet", "email": "i@exemple.fr"})
    facture = _draft(client, ctx)
    refus = _issue(client, ctx, facture["id"])
    assert refus.status_code == 422
    assert "siren" in refus.json()["detail"]

    assert (
        client.put(
            f"/customers/{ctx['customer_id']}", headers=ctx["headers"], json=CUSTOMER_COMPLET
        ).status_code
        == 200
    )
    assert _issue(client, ctx, facture["id"]).status_code == 200


def test_formats_rejetes_a_la_saisie(client: TestClient) -> None:
    ctx = _setup(client)
    # SIREN mal formé.
    assert (
        client.post(
            "/customers",
            headers=ctx["headers"],
            json={**CUSTOMER_COMPLET, "email": "x@exemple.fr", "siren": "12345"},
        ).status_code
        == 422
    )
    # Code pays hors ISO 3166-1 alpha-2 (minuscules) : refusé à la saisie,
    # pas à la validation Schematron de 4b.
    assert (
        client.post(
            "/customers",
            headers=ctx["headers"],
            json={**CUSTOMER_COMPLET, "email": "y@exemple.fr", "country_code": "fr"},
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/company-profile", headers=ctx["headers"], json={**PROFILE_REEL, "country_code": "FRA"}
        ).status_code
        == 422
    )
    # Catégorie d'opération inconnue.
    assert (
        client.post(
            "/invoices",
            headers=ctx["headers"],
            json={
                "customer_id": ctx["customer_id"],
                "operation_category": "troc",
                "lines": [LIGNE_20],
            },
        ).status_code
        == 422
    )


def test_mentions_reforme_persistees(client: TestClient) -> None:
    ctx = _setup(client)
    facture = _draft(
        client,
        ctx,
        operation_category="livraison_de_biens",
        vat_on_debits=True,
        delivery_address="Quai 4, 14 rue du Port, 76600 Le Havre",
    )
    emise = _issue(client, ctx, facture["id"]).json()
    assert emise["operation_category"] == "livraison_de_biens"
    assert emise["vat_on_debits"] is True
    assert emise["delivery_address"] == "Quai 4, 14 rue du Port, 76600 Le Havre"
    assert emise["buyer_siren"] == CUSTOMER_COMPLET["siren"]


# --- Audit ------------------------------------------------------------------------


def test_audit_invoice_created_et_issued(client: TestClient, super_engine: Engine) -> None:
    ctx = _setup(client)
    facture = _draft(client, ctx)
    _issue(client, ctx, facture["id"])

    rows = [
        r for r in _audit_rows(super_engine, ctx["tenant_id"]) if r.action.startswith("invoice")
    ]
    assert [(r.action, r.target_type, str(r.target_id)) for r in rows] == [
        ("invoice_created", "invoice", facture["id"]),
        ("invoice_issued", "invoice", facture["id"]),
    ]
    assert all(str(r.actor_id) == ctx["user_id"] for r in rows)
