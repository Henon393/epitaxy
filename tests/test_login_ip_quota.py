"""Plafond de connexions par adresse : pulvérisation et effets de bord.

La fenêtre par couple (adresse, e-mail) protège un compte donné mais
laisse passer la pulvérisation, où un mot de passe courant est essayé sur
de nombreux comptes depuis la même adresse. Un second plafond, par
adresse seule, ferme ce cas.

Ces tests vérifient aussi ses deux effets de bord : un usage légitime
derrière une adresse partagée ne doit pas être freiné, et le titulaire
d'un compte visé doit rester joignable depuis une autre adresse.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.conftest import bearer, signup_tenant


def _client_depuis(adresse: str) -> TestClient:
    """Client dont l'application voit l'adresse indiquée."""
    from app.main import app

    return TestClient(app, client=(adresse, 50000))


def _echec(client: TestClient, tenant_id: str, email: str) -> int:
    return client.post(
        "/auth/login",
        json={"tenant_id": tenant_id, "email": email, "password": "mauvais-mdp!"},
    ).status_code


def _connexion(client: TestClient, tenant_id: str, email: str, mdp: str) -> int:
    return client.post(
        "/auth/login",
        json={"tenant_id": tenant_id, "email": email, "password": mdp},
    ).status_code


@pytest.fixture
def plafond() -> int:
    return get_settings().login_ip_rate_limit_attempts


def test_pulverisation_sur_de_nombreux_comptes_bloquee(client: TestClient, plafond: int) -> None:
    """Le cas que la fenêtre par couple laissait passer intégralement."""
    s = signup_tenant(client)
    attaquant = _client_depuis("198.51.100.20")

    # Un e-mail différent à chaque essai : chaque couple reste sous le
    # seuil de 5, seul le plafond par adresse peut arrêter l'attaque.
    codes = [_echec(attaquant, s["tenant_id"], f"cible{i}@exemple.fr") for i in range(plafond + 3)]

    assert 429 in codes, "la pulvérisation n'est pas freinée"
    assert codes.index(429) == plafond, (
        f"blocage attendu au {plafond + 1}e essai, obtenu au {codes.index(429) + 1}e"
    )


def test_usage_legitime_derriere_une_adresse_partagee_non_bloque(
    client: TestClient, plafond: int
) -> None:
    """Un bureau entier derrière un NAT ne doit pas consommer le quota.

    Seuls les échecs sont comptés : des connexions réussies, même
    nombreuses, laissent le plafond intact.
    """
    s = signup_tenant(client)
    entetes = bearer(s["access_token"])
    bureau = _client_depuis("203.0.113.30")

    # Une vingtaine de comptes légitimes, tous derrière la même adresse.
    collegues = []
    for i in range(plafond + 5):
        email = f"collegue{i}@exemple.fr"
        assert (
            client.post(
                "/users",
                headers=entetes,
                json={"email": email, "password": "mot-de-passe-solide", "role": "comptable"},
            ).status_code
            == 201
        )
        collegues.append(email)

    codes = [
        _connexion(bureau, s["tenant_id"], email, "mot-de-passe-solide") for email in collegues
    ]
    assert codes == [200] * len(collegues), "des connexions légitimes ont été bloquées"

    # Le quota est intact : une faute de frappe passe encore.
    assert _echec(bureau, s["tenant_id"], collegues[0]) == 401


def test_la_fenetre_par_couple_reste_active(client: TestClient) -> None:
    """Le plafond par adresse ne remplace pas la protection par compte.

    Cinq échecs sur un même compte suffisent, bien avant le plafond par
    adresse qui en tolère vingt.
    """
    s = signup_tenant(client)
    seuil = get_settings().login_rate_limit_attempts
    attaquant = _client_depuis("198.51.100.40")

    codes = [_echec(attaquant, s["tenant_id"], s["email"]) for _ in range(seuil + 2)]
    assert codes.index(429) == seuil


def test_le_titulaire_reste_joignable_depuis_une_autre_adresse(
    client: TestClient, plafond: int
) -> None:
    """Aucun verrouillage de compte : l'attaque ne bloque que son auteur.

    C'est la raison pour laquelle aucune fenêtre par e-mail seul n'a été
    ajoutée. Si le plafond portait sur le compte, n'importe qui pourrait
    verrouiller celui d'autrui.
    """
    s = signup_tenant(client)
    attaquant = _client_depuis("198.51.100.50")
    victime = _client_depuis("192.0.2.60")

    # L'attaquant sature son propre quota en visant le compte de la victime.
    for i in range(plafond + 2):
        _echec(attaquant, s["tenant_id"], f"quelconque{i}@exemple.fr")
    assert _echec(attaquant, s["tenant_id"], s["email"]) == 429, "l'attaquant devrait être bloqué"

    # La victime, depuis son adresse, se connecte normalement.
    assert _connexion(victime, s["tenant_id"], s["email"], s["password"]) == 200
