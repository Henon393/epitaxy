"""La transaction est validée avant que la réponse ne parte.

Défaut corrigé : la session était validée dans la sortie de contexte de la
dépendance ``get_db``, qui s'exécute après l'envoi de la réponse. Une route
confirmait donc une création par un ``201`` avant que la transaction ne soit
validée, et une requête enchaînée pouvait ne pas voir l'objet. Sous charge,
une facture créée puis émise aussitôt recevait ``404``.

Deux tests, complémentaires :

- l'invariant, qui interdit qu'un routeur échappe à la règle. C'est lui qui
  protège d'une régression future, car la correction est centrale et un
  nouveau routeur pourrait l'oublier ;
- la régression proprement dite, qui rejoue la séquence fautive contre un
  vrai serveur HTTP. La fenêtre étant d'une milliseconde en temps normal,
  elle est élargie en ralentissant le commit : le décalage devient alors
  certain au lieu d'être probable, ce qu'une suite de tests exige.
"""

import socket
import threading
import time
from collections.abc import Iterator
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from app.main import app
from app.routing import RouteValideeAvantReponse

MDP = "mot-de-passe-solide"
AUJOURD_HUI = date.today()

VENDEUR = {
    "legal_name": "Regression SAS",
    "siren": "482193075",
    "siret": "48219307500017",
    "vat_number": "FR01482193075",
    "address_line1": "1 rue des Essais",
    "postal_code": "75001",
    "city": "Paris",
    "country_code": "FR",
    "legal_form": "SAS",
    "vat_regime": "reel_normal",
}

CLIENT = {
    "name": "Client Regression SARL",
    "email": "client@exemple.fr",
    "siren": "394716286",
    "address_line1": "8 avenue Jean Jaures",
    "postal_code": "35000",
    "city": "Rennes",
    "country_code": "FR",
}


# --- Invariant --------------------------------------------------------------


def _routeurs_inclus() -> list:
    """Routeurs montés sur l'application.

    Les routes des routeurs inclus ne sont pas mises à plat dans
    ``app.routes`` : chaque inclusion conserve son routeur d'origine.
    """
    return [r.original_router for r in app.routes if hasattr(r, "original_router")]


def test_tous_les_routeurs_valident_avant_la_reponse() -> None:
    """Aucun routeur métier ne doit échapper à la classe de route.

    Sans cette garde, un routeur ajouté plus tard rouvrirait la fenêtre sans
    que rien ne le signale.
    """
    routeurs = _routeurs_inclus()
    assert routeurs, "aucun routeur inclus trouvé : le parcours est à revoir"

    manquants = [str(r.tags) for r in routeurs if r.route_class is not RouteValideeAvantReponse]
    assert not manquants, f"routeurs sans validation avant réponse : {manquants}"


def test_toutes_les_routes_metier_portent_la_classe() -> None:
    """Contrôle au niveau des routes, pas seulement des routeurs."""
    routes = [route for routeur in _routeurs_inclus() for route in routeur.routes]
    assert len(routes) >= 30, f"seulement {len(routes)} routes parcourues"

    fautives = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if not isinstance(r, RouteValideeAvantReponse)
    ]
    assert not fautives, f"routes sans validation avant réponse : {fautives}"


# --- Régression -------------------------------------------------------------

_retard = {"actif": False, "secondes": 0.35}


@pytest.fixture
def commit_ralenti(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """Élargit la fenêtre en allongeant chaque commit.

    En temps normal un commit dure moins d'une milliseconde, soit moins que
    l'aller-retour du client : le décalage existe mais se manifeste rarement.
    Le ralentir rend le scénario déterministe sans rien changer à la logique.
    """
    commit_initial = Session.commit

    def commit_lent(self: Session, *args: object, **kwargs: object) -> None:
        if _retard["actif"]:
            time.sleep(_retard["secondes"])
        return commit_initial(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", commit_lent)
    _retard["actif"] = False
    try:
        yield _retard
    finally:
        _retard["actif"] = False


@pytest.fixture
def serveur_http(apply_migrations: None) -> Iterator[str]:
    """Vrai serveur HTTP, dans un fil.

    ``TestClient`` exécute tout en processus et ne rejoue pas l'ordonnancement
    entre l'envoi de la réponse et la sortie de contexte des dépendances. La
    démonstration exige donc un serveur réel.
    """
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    serveur = uvicorn.Server(config)
    fil = threading.Thread(target=serveur.run, daemon=True)
    fil.start()

    limite = time.monotonic() + 20
    while not serveur.started and time.monotonic() < limite:
        time.sleep(0.05)
    assert serveur.started, "le serveur de test n'a pas démarré"

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        serveur.should_exit = True
        fil.join(timeout=10)


def _entetes(jeton: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton}"}


def _corps_facture(customer_id: str) -> dict:
    return {
        "customer_id": customer_id,
        "operation_category": "prestation_de_services",
        "supply_date": (AUJOURD_HUI - timedelta(days=10)).isoformat(),
        "due_date": (AUJOURD_HUI + timedelta(days=30)).isoformat(),
        "lines": [
            {
                "designation": "Prestation de regression",
                "quantity": "1",
                "unit_price_ht": "100.00",
                "vat_rate": "20.00",
            }
        ],
    }


def test_une_creation_confirmee_est_vue_par_la_requete_suivante(
    serveur_http: str, commit_ralenti: dict
) -> None:
    """La séquence exacte qui échouait, rejouée avec la fenêtre élargie.

    Avant correction, chacun des deux enchaînements renvoyait ``404`` de
    façon reproductible : l'objet venait d'être confirmé mais sa transaction
    n'était pas encore validée.
    """
    http = httpx.Client(base_url=serveur_http, timeout=60)

    # Montage, à vitesse normale : ce n'est pas ce qu'on mesure.
    inscription = http.post(
        "/auth/signup",
        json={
            "tenant_name": "Regression commit",
            "email": "regression@exemple.fr",
            "password": MDP,
        },
    )
    assert inscription.status_code == 201, inscription.text
    entetes = _entetes(inscription.json()["access_token"])
    assert http.put("/company-profile", headers=entetes, json=VENDEUR).status_code == 200

    # À partir d'ici, chaque commit dure plus longtemps que l'aller-retour
    # du client : si la validation avait lieu après la réponse, la requête
    # suivante ne verrait rien.
    commit_ralenti["actif"] = True

    # 1. Un client confirmé doit être utilisable aussitôt.
    creation_client = http.post("/customers", headers=entetes, json=CLIENT)
    assert creation_client.status_code == 201, creation_client.text
    customer_id = creation_client.json()["id"]

    creation_facture = http.post("/invoices", headers=entetes, json=_corps_facture(customer_id))
    assert creation_facture.status_code == 201, (
        "le client vient d'être confirmé mais la facture ne le voit pas : "
        f"{creation_facture.status_code} {creation_facture.text}"
    )
    invoice_id = creation_facture.json()["id"]

    # 2. Une facture confirmée doit être émissible aussitôt.
    emission = http.post(f"/invoices/{invoice_id}/issue", headers=entetes)
    assert emission.status_code == 200, (
        "la facture vient d'être confirmée mais l'émission ne la voit pas : "
        f"{emission.status_code} {emission.text}"
    )

    # 3. Une émission confirmée doit permettre la génération d'artefact.
    cii = http.post(f"/invoices/{invoice_id}/cii", headers=entetes)
    assert cii.status_code in (200, 201), (
        "l'émission vient d'être confirmée mais la génération ne la voit pas : "
        f"{cii.status_code} {cii.text}"
    )

    # Les valeurs répondues restent justes : la sérialisation a lu les objets
    # dans la transaction ouverte, avant le commit.
    emise = emission.json()
    assert emise["status"] == "emise"
    assert emise["number"] == 1
    assert emise["total_ttc"] == "120.00"

    http.close()
