"""Tests 2b : TOTP, chiffrement au repos, login à deux temps, anti-rejeu."""

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.config import get_settings
from app.mfa import TOTP_STEP_SECONDS, decrypt_totp_secret
from app.rate_limit import enforce_mfa_rate_limit
from tests.conftest import bearer, signup_tenant
from tests.test_audit import _audit_rows


def _enroll(client: TestClient, ctx: dict) -> str:
    """Enrôle et retourne le secret TOTP (extrait de l'URI otpauth)."""
    reponse = client.post("/mfa/enroll", headers=bearer(ctx["access_token"]))
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["qr_svg"].lstrip().startswith("<?xml") or "<svg" in corps["qr_svg"]
    return pyotp.parse_uri(corps["otpauth_uri"]).secret


def _code_suivant(secret: str) -> str:
    """Code du PAS DE TEMPS SUIVANT (dans la fenêtre +1) : toujours plus
    grand que tout pas déjà consommé dans les tests, sans attendre 30 s."""
    timestep = int(time.time()) // TOTP_STEP_SECONDS + 1
    return pyotp.TOTP(secret).at(timestep * TOTP_STEP_SECONDS)


def _activer(client: TestClient, ctx: dict) -> tuple[str, list[str]]:
    secret = _enroll(client, ctx)
    code = pyotp.TOTP(secret).now()
    reponse = client.post("/mfa/activate", headers=bearer(ctx["access_token"]), json={"code": code})
    assert reponse.status_code == 200, reponse.text
    codes = reponse.json()["backup_codes"]
    assert len(codes) == 10
    return secret, codes


def _login(client: TestClient, ctx: dict) -> dict:
    reponse = client.post(
        "/auth/login",
        json={"tenant_id": ctx["tenant_id"], "email": ctx["email"], "password": ctx["password"]},
    )
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _verify(client: TestClient, mfa_token: str, code: str):
    return client.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code})


# --- Chiffrement au repos -----------------------------------------------------


def test_secret_totp_chiffre_en_base(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    secret = _enroll(client, ctx)

    with super_engine.connect() as conn:
        stocke = conn.execute(
            text("SELECT mfa_secret FROM users WHERE id = :id"), {"id": ctx["user_id"]}
        ).scalar_one()
    # Illisible sans la clé : le secret base32 n'apparaît nulle part.
    assert secret not in stocke
    assert stocke != secret
    # Et c'est bien du Fernet : la clé le déchiffre exactement.
    assert decrypt_totp_secret(stocke) == secret


# --- Enrôlement à activation prouvée -------------------------------------------


def test_activation_refusee_sans_code_valide(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _enroll(client, ctx)

    refus = client.post(
        "/mfa/activate", headers=bearer(ctx["access_token"]), json={"code": "000000"}
    )
    assert refus.status_code == 422
    # L'échec est audité malgré le 4xx (session d'audit dédiée).
    echecs = [r for r in _audit_rows(super_engine, ctx["tenant_id"]) if r.action == "mfa_failed"]
    assert len(echecs) == 1 and echecs[0].metadata["context"] == "activation"
    # Pas activé : le login reste à un temps.
    assert "access_token" in _login(client, ctx)


def test_activation_avec_code_valide(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _activer(client, ctx)
    actions = {r.action for r in _audit_rows(super_engine, ctx["tenant_id"])}
    assert {"mfa_enrolled", "mfa_activated"} <= actions
    # Ré-enrôlement une fois actif : refusé.
    assert client.post("/mfa/enroll", headers=bearer(ctx["access_token"])).status_code == 409


# --- Login à deux temps ----------------------------------------------------------


def test_login_a_deux_temps(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    secret, _ = _activer(client, ctx)

    # Mot de passe seul : authentification partielle, aucun token d'accès.
    challenge = _login(client, ctx)
    assert challenge["mfa_required"] is True
    assert "access_token" not in challenge
    mfa_token = challenge["mfa_token"]
    # Aucun login_succeeded tant que le second facteur n'est pas passé.
    assert not [
        r
        for r in _audit_rows(super_engine, ctx["tenant_id"])
        if r.action == "login_succeeded" and r.metadata.get("mfa") == "true"
    ]

    # Le jeton intermédiaire n'ouvre RIEN.
    assert client.get("/customers", headers=bearer(mfa_token)).status_code == 401
    assert client.get("/me", headers=bearer(mfa_token)).status_code == 401

    # Code invalide : 401 + audit mfa_failed.
    assert _verify(client, mfa_token, "000000").status_code == 401
    assert [r for r in _audit_rows(super_engine, ctx["tenant_id"]) if r.action == "mfa_failed"]

    # Code valide : session complète, accès effectif.
    succes = _verify(client, mfa_token, _code_suivant(secret))
    assert succes.status_code == 200
    tokens = succes.json()
    assert client.get("/customers", headers=bearer(tokens["access_token"])).status_code == 200
    reussites = [
        r
        for r in _audit_rows(super_engine, ctx["tenant_id"])
        if r.action == "login_succeeded" and r.metadata.get("mfa") == "true"
    ]
    assert len(reussites) == 1 and reussites[0].metadata["method"] == "totp"


def test_mfa_token_usage_unique_et_expiration(client: TestClient) -> None:
    ctx = signup_tenant(client)
    secret, _ = _activer(client, ctx)

    challenge = _login(client, ctx)
    jeton = challenge["mfa_token"]
    assert _verify(client, jeton, _code_suivant(secret)).status_code == 200
    # Rejeu du jeton déjà échangé, même avec un code encore valide : 401.
    assert _verify(client, jeton, _code_suivant(secret)).status_code == 401

    # Jeton expiré (forgé exp passé, même clé) : 401.
    claims = pyjwt.decode(jeton, options={"verify_signature": False})
    claims["exp"] = datetime.now(UTC) - timedelta(minutes=1)
    claims["iat"] = datetime.now(UTC) - timedelta(minutes=10)
    claims["jti"] = str(uuid.uuid4())
    expire = pyjwt.encode(claims, get_settings().jwt_secret, algorithm="HS256")
    assert _verify(client, expire, _code_suivant(secret)).status_code == 401


# --- Anti-rejeu --------------------------------------------------------------------


def test_code_consomme_non_rejouable(client: TestClient) -> None:
    ctx = signup_tenant(client)
    secret, _ = _activer(client, ctx)

    code = _code_suivant(secret)
    assert _verify(client, _login(client, ctx)["mfa_token"], code).status_code == 200
    # Le MÊME code, encore dans sa fenêtre de validité : refusé.
    assert _verify(client, _login(client, ctx)["mfa_token"], code).status_code == 401


# --- Codes de secours -----------------------------------------------------------


def test_codes_de_secours_usage_unique(client: TestClient, super_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _, codes = _activer(client, ctx)

    premier = _verify(client, _login(client, ctx)["mfa_token"], codes[0])
    assert premier.status_code == 200
    # Événement dédié : second facteur perdu ou indisponible.
    assert [
        r for r in _audit_rows(super_engine, ctx["tenant_id"]) if r.action == "mfa_backup_code_used"
    ]

    # Le code consommé ne resert pas ; un autre code fonctionne.
    assert _verify(client, _login(client, ctx)["mfa_token"], codes[0]).status_code == 401
    assert _verify(client, _login(client, ctx)["mfa_token"], codes[1]).status_code == 200


# --- Invalidation des sessions -------------------------------------------------


def test_activation_invalide_les_sessions_anterieures(client: TestClient) -> None:
    ctx = signup_tenant(client)
    # Session ouverte AVANT l'activation (login simple, sans MFA).
    avant = _login(client, ctx)
    assert "access_token" in avant

    _activer(client, ctx)

    # Le refresh émis avant l'activation est rejeté : impossible de
    # continuer sans jamais passer le MFA.
    refus = client.post("/auth/refresh", json={"refresh_token": avant["refresh_token"]})
    assert refus.status_code == 401


def test_desactivation_invalide_aussi_les_sessions(client: TestClient) -> None:
    ctx = signup_tenant(client)
    secret, codes = _activer(client, ctx)
    session_mfa = _verify(client, _login(client, ctx)["mfa_token"], _code_suivant(secret)).json()

    # Code de secours pour la désactivation : le code TOTP de la fenêtre
    # vient d'être consommé par le login, l'anti-rejeu le refuserait (voulu).
    desactivation = client.post(
        "/mfa/disable",
        headers=bearer(session_mfa["access_token"]),
        json={"code": codes[0]},
    )
    assert desactivation.status_code == 204

    # La session MFA d'avant la désactivation meurt au rafraîchissement...
    refus = client.post("/auth/refresh", json={"refresh_token": session_mfa["refresh_token"]})
    assert refus.status_code == 401
    # ...et le login redevient à un temps.
    assert "access_token" in _login(client, ctx)


# --- Rate limiting ----------------------------------------------------------------


def test_rate_limit_par_utilisateur_independant_de_l_ip(client: TestClient) -> None:
    """Un brute-force distribué sur plusieurs IP ne contourne pas la
    fenêtre par utilisateur seul."""
    from fastapi import HTTPException

    settings = get_settings()
    user_id = str(uuid.uuid4())
    limite = settings.mfa_user_rate_limit_attempts
    for i in range(limite):
        enforce_mfa_rate_limit(f"10.0.0.{i}", user_id)  # IP différente à chaque essai
    with pytest.raises(HTTPException) as excinfo:
        enforce_mfa_rate_limit(f"10.0.1.{limite}", user_id)
    assert excinfo.value.status_code == 429


def test_rate_limit_sur_la_verification_via_l_api(client: TestClient) -> None:
    ctx = signup_tenant(client)
    _activer(client, ctx)
    jeton = _login(client, ctx)["mfa_token"]
    limite = get_settings().mfa_rate_limit_attempts
    for _ in range(limite):
        assert _verify(client, jeton, "000000").status_code == 401
    assert _verify(client, jeton, "000000").status_code == 429


# --- Isolation et fail-closed ----------------------------------------------------


def test_codes_de_secours_isoles_par_tenant(client: TestClient, app_engine: Engine) -> None:
    ctx = signup_tenant(client)
    _activer(client, ctx)

    from tests.conftest import set_tenant

    with app_engine.connect() as conn:
        set_tenant(conn, uuid.uuid4())  # autre tenant
        assert conn.execute(text("SELECT id FROM mfa_backup_codes")).fetchall() == []
    with app_engine.connect() as conn:
        set_tenant(conn, uuid.UUID(ctx["tenant_id"]))
        assert len(conn.execute(text("SELECT id FROM mfa_backup_codes")).fetchall()) == 10


def test_fail_closed_de_l_audit_sur_l_activation(
    client: TestClient, super_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit saboté pendant l'activation : l'activation est annulée avec
    lui, pas de MFA activé sans sa trace."""
    import app.routers.mfa as mfa_module
    from app.main import app

    ctx = signup_tenant(client)
    secret = _enroll(client, ctx)

    def record_saboteur(*args, **kwargs):
        raise RuntimeError("audit indisponible")

    monkeypatch.setattr(mfa_module, "record", record_saboteur)
    sans_exception = TestClient(app, raise_server_exceptions=False)
    reponse = sans_exception.post(
        "/mfa/activate",
        headers=bearer(ctx["access_token"]),
        json={"code": pyotp.TOTP(secret).now()},
    )
    assert reponse.status_code == 500
    monkeypatch.undo()

    with super_engine.connect() as conn:
        row = conn.execute(
            text("SELECT mfa_enabled, session_version FROM users WHERE id = :id"),
            {"id": ctx["user_id"]},
        ).one()
    assert row.mfa_enabled is False
    assert row.session_version == 0
    # Et aucun code de secours orphelin.
    with super_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM mfa_backup_codes WHERE user_id = :id"),
            {"id": ctx["user_id"]},
        ).scalar_one()
    assert count == 0
