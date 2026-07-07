"""Tests 5a : connecteur PA mock, machine à états, append-only, audit."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.pa import PaStatus, get_pa_connector
from tests.conftest import set_tenant
from tests.test_audit import _audit_rows
from tests.test_cii import LIGNES_MULTI_TAUX, _facture_emise, _post_cii
from tests.test_facturx import _post_facturx
from tests.test_invoices import _draft, _issue, _setup


def _facture_transmissible(client: TestClient, ctx: dict) -> dict:
    """Facture émise avec ses artefacts cii_xml et facturx_pdf."""
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)
    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    assert _post_facturx(client, ctx, emise["id"]).status_code == 201
    return emise


def _submit(client: TestClient, ctx: dict, invoice_id: str):
    return client.post(f"/invoices/{invoice_id}/submit", headers=ctx["headers"])


def _refresh(client: TestClient, ctx: dict, transmission_id: str):
    return client.post(f"/transmissions/{transmission_id}/refresh", headers=ctx["headers"])


def _programme_puis_refresh(
    client: TestClient, ctx: dict, transmission: dict, status: PaStatus, **payload
):
    get_pa_connector().program_status(transmission["pa_transmission_ref"], status, **payload)
    return _refresh(client, ctx, transmission["id"])


# --- Préconditions de soumission ---------------------------------------------


def test_soumission_exige_le_facturx(client: TestClient, super_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_emise(client, ctx, LIGNES_MULTI_TAUX)

    refus = _submit(client, ctx, emise["id"])
    assert refus.status_code == 409
    assert "facturx_pdf" in refus.json()["detail"]

    assert _post_cii(client, ctx, emise["id"]).status_code == 201
    assert _post_facturx(client, ctx, emise["id"]).status_code == 201
    reponse = _submit(client, ctx, emise["id"])
    assert reponse.status_code == 201, reponse.text
    corps = reponse.json()
    assert corps["current_status"] == "deposee"
    assert corps["routing_identifier"] == emise["buyer_siren"]  # proxy SIREN
    assert corps["events"][0]["status_code"] == "200"

    # Double soumission pendant une transmission en cours : refusée.
    assert _submit(client, ctx, emise["id"]).status_code == 409

    # Audit : invoice_submitted avec acteur.
    rows = [
        r for r in _audit_rows(super_engine, ctx["tenant_id"]) if r.action == "invoice_submitted"
    ]
    assert len(rows) == 1
    assert str(rows[0].actor_id) == ctx["user_id"]
    assert rows[0].metadata["routing_identifier"] == emise["buyer_siren"]


# --- Cycle nominal, statut courant, encaissée répétable ----------------------


def test_cycle_nominal_et_encaissements_multiples(client: TestClient, super_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()

    assert _programme_puis_refresh(client, ctx, transmission, PaStatus.recue).status_code == 200
    assert _programme_puis_refresh(client, ctx, transmission, PaStatus.approuvee).status_code == 200
    premier_paiement = _programme_puis_refresh(
        client,
        ctx,
        transmission,
        PaStatus.encaissee,
        paid_amount=Decimal("100.00"),
        paid_at=date(2026, 7, 20),
    )
    assert premier_paiement.status_code == 200
    second_paiement = _programme_puis_refresh(
        client,
        ctx,
        transmission,
        PaStatus.encaissee,
        paid_amount=Decimal("116.65"),
        paid_at=date(2026, 8, 5),
    )
    assert second_paiement.status_code == 200

    corps = second_paiement.json()
    assert [e["status"] for e in corps["events"]] == [
        "deposee",
        "recue",
        "approuvee",
        "encaissee",
        "encaissee",
    ]
    assert [e["position"] for e in corps["events"]] == [1, 2, 3, 4, 5]
    # Statut courant = dernier événement, jamais un champ mutable.
    assert corps["current_status"] == "encaissee"
    # Chaque encaissement porte sa date et son montant.
    paiements = [e for e in corps["events"] if e["status"] == "encaissee"]
    assert [(p["paid_amount"], p["paid_at"]) for p in paiements] == [
        ("100.00", "2026-07-20"),
        ("116.65", "2026-08-05"),
    ]

    # Audit : un transmission_status_changed par transition, avec acteur.
    changements = [
        r
        for r in _audit_rows(super_engine, ctx["tenant_id"])
        if r.action == "transmission_status_changed"
    ]
    assert [(r.metadata["from"], r.metadata["to"]) for r in changements] == [
        ("deposee", "recue"),
        ("recue", "approuvee"),
        ("approuvee", "encaissee"),
        ("encaissee", "encaissee"),
    ]
    assert all(str(r.actor_id) == ctx["user_id"] for r in changements)


def test_encaissee_sans_montant_refusee(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    reponse = _programme_puis_refresh(client, ctx, transmission, PaStatus.encaissee)
    assert reponse.status_code == 422
    assert "montant" in reponse.json()["detail"]


def test_statut_inchange_sans_ecriture(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    # Le mock renvoie toujours deposee : refresh sans nouveau statut.
    reponse = _refresh(client, ctx, transmission["id"])
    assert reponse.status_code == 200
    assert len(reponse.json()["events"]) == 1


# --- Terminaux et distinction technique / commercial -------------------------


def test_rejet_technique_est_un_puits_du_modele_attendu(client: TestClient) -> None:
    """Depuis 5b, le puits terminal est un référentiel d'attendu, plus une
    porte : une sortie rapportée par la PA est consignée en anomalie, pas
    refusée (la PA fait foi)."""
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()

    rejet = _programme_puis_refresh(
        client, ctx, transmission, PaStatus.rejetee, reason="PA indisponible"
    )
    assert rejet.status_code == 200
    assert rejet.json()["current_status"] == "rejetee"
    assert rejet.json()["events"][-1]["out_of_graph"] is False

    # Sortie d'état terminal rapportée par la PA : enregistrée ET signalée.
    sortie = _programme_puis_refresh(client, ctx, transmission, PaStatus.recue)
    assert sortie.status_code == 200
    dernier = sortie.json()["events"][-1]
    assert dernier["status"] == "recue"
    assert dernier["out_of_graph"] is True


def test_refus_commercial_direct_et_apres_reception(client: TestClient) -> None:
    ctx = _setup(client)
    # Chemin 1 : refus commercial direct depuis deposee (PA minimale qui ne
    # pose pas les statuts recommandés).
    facture_1 = _facture_transmissible(client, ctx)
    transmission_1 = _submit(client, ctx, facture_1["id"]).json()
    refus = _programme_puis_refresh(
        client, ctx, transmission_1, PaStatus.refusee, reason="marchandise non conforme"
    )
    assert refus.status_code == 200
    assert refus.json()["current_status"] == "refusee"
    # Terminal aussi : une sortie est consignée en anomalie (5b).
    sortie = _programme_puis_refresh(client, ctx, transmission_1, PaStatus.recue)
    assert sortie.status_code == 200
    assert sortie.json()["events"][-1]["out_of_graph"] is True

    # Chemin 2 : refus après réception (deposee → recue → refusee).
    facture_2 = _draft(client, ctx, lines=LIGNES_MULTI_TAUX)
    emise_2 = _issue(client, ctx, facture_2["id"]).json()
    assert _post_cii(client, ctx, emise_2["id"]).status_code == 201
    assert _post_facturx(client, ctx, emise_2["id"]).status_code == 201
    transmission_2 = _submit(client, ctx, emise_2["id"]).json()
    assert _programme_puis_refresh(client, ctx, transmission_2, PaStatus.recue).status_code == 200
    assert _programme_puis_refresh(client, ctx, transmission_2, PaStatus.refusee).status_code == 200

    # Chemin croisé hors modèle : un rejet TECHNIQUE après réception n'existe
    # pas dans le graphe (rejetee ne sort que de deposee) — rapporté par la
    # PA, il est consigné en anomalie (5b), pas refusé.
    facture_3 = _draft(client, ctx, lines=LIGNES_MULTI_TAUX)
    emise_3 = _issue(client, ctx, facture_3["id"]).json()
    assert _post_cii(client, ctx, emise_3["id"]).status_code == 201
    assert _post_facturx(client, ctx, emise_3["id"]).status_code == 201
    transmission_3 = _submit(client, ctx, emise_3["id"]).json()
    assert _programme_puis_refresh(client, ctx, transmission_3, PaStatus.recue).status_code == 200
    croise = _programme_puis_refresh(client, ctx, transmission_3, PaStatus.rejetee)
    assert croise.status_code == 200
    assert croise.json()["events"][-1]["out_of_graph"] is True
    # Retour arrière : hors graphe lui aussi, consigné de même.
    arriere = _programme_puis_refresh(client, ctx, transmission_3, PaStatus.deposee)
    assert arriere.status_code == 200
    assert arriere.json()["events"][-1]["out_of_graph"] is True


# --- Re-soumission et frontière immuabilité -----------------------------------


def test_resoumission_apres_rejet_transport_seulement(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)

    premiere = _submit(client, ctx, emise["id"]).json()
    _programme_puis_refresh(client, ctx, premiere, PaStatus.rejetee, reason="acheminement")

    # Rejet (transport) : re-soumission du MÊME artefact autorisée.
    seconde = _submit(client, ctx, emise["id"])
    assert seconde.status_code == 201
    assert seconde.json()["id"] != premiere["id"]
    assert seconde.json()["events"][0]["position"] == 1  # nouvel historique

    # Refus commercial : pas de re-soumission (l'avoir est hors périmètre).
    _programme_puis_refresh(client, ctx, seconde.json(), PaStatus.refusee)
    assert _submit(client, ctx, emise["id"]).status_code == 409


def test_facture_rejetee_reste_emise_et_immuable(
    client: TestClient, app_engine: Engine, super_engine: Engine
) -> None:
    """La transmission échoue, pas la facture : émise, numérotée, immuable,
    sans libération de numéro ni réémission."""
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    _programme_puis_refresh(client, ctx, transmission, PaStatus.rejetee, reason="transport")

    # (a) La facture d'origine est intacte.
    relue = client.get(f"/invoices/{emise['id']}", headers=ctx["headers"]).json()
    assert relue["status"] == "emise"
    assert relue["number"] == emise["number"] == 1
    assert relue["total_ttc"] == emise["total_ttc"]
    assert relue["seller_legal_name"] == emise["seller_legal_name"]

    # (b) Toujours immuable en SQL direct (trigger 4a).
    with app_engine.connect() as conn:
        set_tenant(conn, uuid.UUID(ctx["tenant_id"]))
        with pytest.raises(DBAPIError, match="immuable"):
            conn.execute(
                text("UPDATE invoices SET status = 'brouillon' WHERE id = :id"),
                {"id": emise["id"]},
            )

    # (c) Aucune réémission : toujours une seule facture pour le tenant...
    factures = client.get("/invoices", headers=ctx["headers"]).json()
    assert len(factures) == 1

    # ...et le compteur n'a pas bougé : la facture suivante prend le numéro
    # suivant, sans trou ni réutilisation.
    suivante = _draft(client, ctx, lines=LIGNES_MULTI_TAUX)
    assert _issue(client, ctx, suivante["id"]).json()["number"] == 2


# --- Append-only, trigger SQL direct, isolation --------------------------------


def test_historique_append_only_et_trigger(client: TestClient, app_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    tenant_id = uuid.UUID(ctx["tenant_id"])

    with app_engine.connect() as conn:
        set_tenant(conn, tenant_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE pa_status_events SET status = 'encaissee'"))
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM pa_status_events"))
        conn.rollback()
        set_tenant(conn, tenant_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE pa_transmissions SET routing_identifier = 'x'"))
        conn.rollback()

        # Trigger : transition invalide en SQL direct (INSERT est permis,
        # mais le graphe est encodé côté base aussi).
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="transition de statut invalide"):
            conn.execute(
                text(
                    "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                    "status) VALUES (gen_random_uuid(), :t, :tr, 2, 'deposee')"
                ),
                {"t": tenant_id, "tr": transmission["id"]},
            )
        conn.rollback()
        # Position non contiguë.
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="non contigue"):
            conn.execute(
                text(
                    "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                    "status) VALUES (gen_random_uuid(), :t, :tr, 5, 'recue')"
                ),
                {"t": tenant_id, "tr": transmission["id"]},
            )
        conn.rollback()
        # Encaissée sans montant/date, côté trigger.
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="montant et date"):
            conn.execute(
                text(
                    "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                    "status) VALUES (gen_random_uuid(), :t, :tr, 2, 'encaissee')"
                ),
                {"t": tenant_id, "tr": transmission["id"]},
            )


def test_isolation_tenant_des_transmissions(client: TestClient, app_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    _submit(client, ctx, emise["id"])

    with app_engine.connect() as conn:
        set_tenant(conn, uuid.uuid4())  # autre tenant
        assert conn.execute(text("SELECT id FROM pa_transmissions")).fetchall() == []
        assert conn.execute(text("SELECT id FROM pa_status_events")).fetchall() == []
    with app_engine.connect() as conn:
        set_tenant(conn, uuid.UUID(ctx["tenant_id"]))
        assert len(conn.execute(text("SELECT id FROM pa_transmissions")).fetchall()) == 1
