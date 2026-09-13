"""Tests 5b : idempotence du poll, ingestion tolérante, worker, découverte."""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from rq import SimpleWorker
from rq.timeouts import TimerDeathPenalty
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.pa import PaStatus, get_pa_connector
from app.redis_client import get_redis
from app.worker import get_queue, list_active_transmissions, poll_transmission
from tests.conftest import set_tenant
from tests.test_audit import _audit_rows
from tests.test_invoices import _setup
from tests.test_pa import (
    _facture_transmissible,
    _programme_puis_refresh,
    _refresh,
    _submit,
)


class _PortableSimpleWorker(SimpleWorker):
    # SimpleWorker sans fork ; TimerDeathPenalty remplace SIGALRM, absent
    # de Windows.
    death_penalty_class = TimerDeathPenalty


def _events(client: TestClient, ctx: dict, invoice_id: str) -> list[dict]:
    reponse = client.get(f"/invoices/{invoice_id}/transmission", headers=ctx["headers"])
    assert reponse.status_code == 200
    return reponse.json()["events"]


# --- Idempotence ---------------------------------------------------------------


def test_deux_polls_du_meme_evenement_un_seul_enregistrement(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()

    get_pa_connector().program_status(transmission["pa_transmission_ref"], PaStatus.recue)
    assert _refresh(client, ctx, transmission["id"]).status_code == 200
    assert len(_events(client, ctx, emise["id"])) == 2

    # Second poll du MÊME événement (même event_ref) : aucune écriture.
    assert _refresh(client, ctx, transmission["id"]).status_code == 200
    assert len(_events(client, ctx, emise["id"])) == 2

    # Et par le chemin worker : issue duplicate explicite.
    outcome = poll_transmission(ctx["tenant_id"], transmission["id"])
    assert outcome == "duplicate"
    assert len(_events(client, ctx, emise["id"])) == 2


def test_paiements_distincts_deux_evenements_meme_paiement_un_seul(
    client: TestClient,
) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)  # total_ttc = 216.65
    transmission = _submit(client, ctx, emise["id"]).json()

    # Paiement 1, pollé deux fois : un seul événement.
    get_pa_connector().program_status(
        transmission["pa_transmission_ref"],
        PaStatus.encaissee,
        paid_amount=Decimal("100.00"),
        paid_at=date(2026, 7, 20),
    )
    _refresh(client, ctx, transmission["id"])
    _refresh(client, ctx, transmission["id"])
    paiements = [e for e in _events(client, ctx, emise["id"]) if e["status"] == "encaissee"]
    assert len(paiements) == 1

    # La transmission reste active : paiement partiel.
    paires = list_active_transmissions()
    assert (uuid.UUID(ctx["tenant_id"]), uuid.UUID(transmission["id"])) in paires

    # Paiement 2 : second événement, avec sa date et son montant.
    get_pa_connector().program_status(
        transmission["pa_transmission_ref"],
        PaStatus.encaissee,
        paid_amount=Decimal("116.65"),
        paid_at=date(2026, 8, 5),
    )
    _refresh(client, ctx, transmission["id"])
    paiements = [e for e in _events(client, ctx, emise["id"]) if e["status"] == "encaissee"]
    assert [(p["paid_amount"], p["paid_at"]) for p in paiements] == [
        ("100.00", "2026-07-20"),
        ("116.65", "2026-08-05"),
    ]

    # Encaissement soldé (100 + 116.65 = TTC) : transmission close, le
    # worker ne la découvre plus.
    paires = list_active_transmissions()
    assert (uuid.UUID(ctx["tenant_id"]), uuid.UUID(transmission["id"])) not in paires


# --- Ingestion tolérante ---------------------------------------------------------


def test_sequence_hors_graphe_consignee_et_signalee(
    client: TestClient, super_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    assert _programme_puis_refresh(client, ctx, transmission, PaStatus.rejetee).status_code == 200

    with caplog.at_level(logging.WARNING, logger="app.security"):
        sortie = _programme_puis_refresh(client, ctx, transmission, PaStatus.recue)

    # Enregistrée, pas rejetée, append-only respecté (position contiguë).
    assert sortie.status_code == 200
    dernier = sortie.json()["events"][-1]
    assert (dernier["status"], dernier["out_of_graph"], dernier["position"]) == ("recue", True, 3)
    # Signalée : audit d'anomalie et log sécurité.
    anomalies = [
        r
        for r in _audit_rows(super_engine, ctx["tenant_id"])
        if r.action == "transmission_anomaly_detected"
    ]
    assert len(anomalies) == 1
    assert (anomalies[0].metadata["from"], anomalies[0].metadata["to"]) == ("rejetee", "recue")
    assert "hors graphe" in caplog.text


def test_trigger_v2_flag_verifie_dans_les_deux_sens(client: TestClient, app_engine: Engine) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    tenant_id = uuid.UUID(ctx["tenant_id"])

    with app_engine.connect() as conn:
        # Transition VALIDE déclarée en anomalie : rejet (vrai positif exigé).
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="anomalie declaree sur une transition valide"):
            conn.execute(
                text(
                    "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                    "status, out_of_graph) VALUES (gen_random_uuid(), :t, :tr, 2, 'recue', true)"
                ),
                {"t": tenant_id, "tr": transmission["id"]},
            )
        conn.rollback()
        # Transition INVALIDE non déclarée : rejet (pas de déviation maquillée).
        set_tenant(conn, tenant_id)
        with pytest.raises(DBAPIError, match="hors graphe non declare"):
            conn.execute(
                text(
                    "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                    "status, out_of_graph) VALUES (gen_random_uuid(), :t, :tr, 2, 'deposee', false)"
                ),
                {"t": tenant_id, "tr": transmission["id"]},
            )
        conn.rollback()
        # Transition invalide DÉCLARÉE : acceptée (c'est l'ingestion tolérante).
        set_tenant(conn, tenant_id)
        conn.execute(
            text(
                "INSERT INTO pa_status_events (id, tenant_id, transmission_id, position, "
                "status, out_of_graph) VALUES (gen_random_uuid(), :t, :tr, 2, 'deposee', true)"
            ),
            {"t": tenant_id, "tr": transmission["id"]},
        )
        conn.commit()


# --- Acteurs -----------------------------------------------------------------------


def test_acteur_systeme_pour_le_worker_utilisateur_pour_le_manuel(
    client: TestClient, super_engine: Engine
) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()

    # Ingestion par le worker : acteur système (NULL + source pa_worker).
    get_pa_connector().program_status(transmission["pa_transmission_ref"], PaStatus.recue)
    assert poll_transmission(ctx["tenant_id"], transmission["id"]) == "recorded"

    # Ingestion manuelle : acteur = utilisateur authentifié.
    get_pa_connector().program_status(transmission["pa_transmission_ref"], PaStatus.approuvee)
    assert _refresh(client, ctx, transmission["id"]).status_code == 200

    changements = [
        r
        for r in _audit_rows(super_engine, ctx["tenant_id"])
        if r.action == "transmission_status_changed"
    ]
    par_source = {r.metadata["source"]: r for r in changements}
    assert set(par_source) == {"pa_worker", "manual"}
    assert par_source["pa_worker"].actor_id is None  # convention : NULL = système
    assert str(par_source["manual"].actor_id) == ctx["user_id"]


# --- Robustesse et découverte -------------------------------------------------------


def test_decouverte_base_first_apres_vidage_redis(client: TestClient) -> None:
    ctx = _setup(client)
    active = _facture_transmissible(client, ctx)
    transmission_active = _submit(client, ctx, active["id"]).json()

    ctx_close = _setup(client)
    close = _facture_transmissible(client, ctx_close)
    transmission_close = _submit(client, ctx_close, close["id"]).json()
    _programme_puis_refresh(client, ctx_close, transmission_close, PaStatus.refusee)

    # Redis intégralement vidé : la base reste la source de vérité.
    get_redis().flushdb()
    paires = list_active_transmissions()
    assert (uuid.UUID(ctx["tenant_id"]), uuid.UUID(transmission_active["id"])) in paires
    assert (
        uuid.UUID(ctx_close["tenant_id"]),
        uuid.UUID(transmission_close["id"]),
    ) not in paires


def test_reprise_apres_echec_sans_doublon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    connector = get_pa_connector()
    connector.program_status(transmission["pa_transmission_ref"], PaStatus.recue)

    # PA injoignable au premier poll : le job échoue (RQ le rejouerait avec
    # backoff), puis la reprise réussit SANS doublon.
    reel = connector.get_status
    appels = {"n": 0}

    def flaky(transmission_ref):
        appels["n"] += 1
        if appels["n"] == 1:
            raise ConnectionError("PA injoignable")
        return reel(transmission_ref)

    monkeypatch.setattr(connector, "get_status", flaky)

    with pytest.raises(ConnectionError):
        poll_transmission(ctx["tenant_id"], transmission["id"])
    assert poll_transmission(ctx["tenant_id"], transmission["id"]) == "recorded"
    assert poll_transmission(ctx["tenant_id"], transmission["id"]) == "duplicate"
    assert len(_events(client, ctx, emise["id"])) == 2


def test_job_execute_par_le_worker_rq(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    get_pa_connector().program_status(transmission["pa_transmission_ref"], PaStatus.recue)

    queue = get_queue()
    queue.enqueue(poll_transmission, ctx["tenant_id"], transmission["id"])
    _PortableSimpleWorker([queue], connection=queue.connection).work(burst=True)

    events = _events(client, ctx, emise["id"])
    assert [e["status"] for e in events] == ["deposee", "recue"]


def test_simulate_pilote_le_mock_en_dev_seulement(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'endpoint de simulation (démonstration) programme le mock en dev,
    et disparaît (404) hors dev."""
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()

    reponse = client.post(
        f"/transmissions/{transmission['id']}/simulate",
        headers=ctx["headers"],
        json={"status": "recue"},
    )
    assert reponse.status_code == 204
    rafraichi = _refresh(client, ctx, transmission["id"])
    assert rafraichi.json()["current_status"] == "recue"

    monkeypatch.setattr(get_settings(), "environment", "prod")
    refus = client.post(
        f"/transmissions/{transmission['id']}/simulate",
        headers=ctx["headers"],
        json={"status": "approuvee"},
    )
    assert refus.status_code == 404


def test_refreshs_concurrents_sans_doublon(client: TestClient) -> None:
    ctx = _setup(client)
    emise = _facture_transmissible(client, ctx)
    transmission = _submit(client, ctx, emise["id"]).json()
    get_pa_connector().program_status(transmission["pa_transmission_ref"], PaStatus.recue)

    from app.main import app

    def refresh_via_nouveau_client(_):
        return TestClient(app).post(
            f"/transmissions/{transmission['id']}/refresh", headers=ctx["headers"]
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        reponses = list(pool.map(refresh_via_nouveau_client, range(4)))

    assert all(r.status_code == 200 for r in reponses)
    assert len(_events(client, ctx, emise["id"])) == 2
