#!/usr/bin/env python
"""Jeu de données de démonstration, pour explorer l'interface.

Crée un client de démonstration complet et affiche ses identifiants de
connexion : un profil vendeur, six clients fictifs et douze factures qui
couvrent les huit statuts du logiciel, dont une réglée en deux versements.
De quoi voir l'interface remplie sans rien saisir à la main.

Tout passe par l'API publique, comme le ferait un utilisateur : inscription,
profil vendeur, clients, brouillons, émission, génération du XML CII et du
Factur-X, soumission à la plateforme simulée et avancement des statuts.
Aucune écriture directe en base, donc aucune règle métier contournée.

Prérequis, dans cet ordre :
    docker compose up -d
    docker compose build pdf
    alembic upgrade head
    python scripts/serve.py

À ne pas confondre avec deux autres commandes :

- ``scripts/demo.py`` parcourt la chaîne une fois de bout en bout et affiche
  des preuves (validation du format, isolation, immuabilité). Il démontre,
  il ne remplit pas ;
- ``pytest`` EFFACE toutes les données, y compris celles créées ici. Lancer
  les tests après ce script oblige à le relancer.

Chaque exécution crée un client de démonstration distinct, avec un nouvel
identifiant. Les précédents restent en place.

Usage : python scripts/demo_data.py [--base-url http://localhost:8000]
"""

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal

import httpx

# --- Identifiants de démonstration -------------------------------------------
# Volontairement simples et lisibles : ce compte n'a pas vocation à protéger
# quoi que ce soit, il sert à ouvrir l'interface sur un jeu d'essai. Le
# domaine .example est réservé aux exemples et n'appartient à personne.
EMAIL_DEMO = "demo@epitaxy.example"
MOT_DE_PASSE_DEMO = "demonstration"
NOM_TENANT = "Lemoine Ingenierie (demonstration)"

AUJOURD_HUI = date.today()


# --- Identifiants d'entreprise fictifs mais structurellement valides ----------
def cle_luhn(base8: str) -> str:
    """Chiffre de contrôle d'un SIREN, à partir de ses huit premiers chiffres.

    Les SIREN d'exemple doivent passer la validation du logiciel, qui vérifie
    la clé de Luhn. Un numéro inventé au hasard serait refusé.
    """
    total = 0
    for index, caractere in enumerate(base8):
        chiffre = int(caractere)
        # Luhn double un chiffre sur deux en partant de la droite du nombre
        # complet : sur une base de huit, ce sont les index impairs.
        if index % 2 == 1:
            chiffre *= 2
            if chiffre > 9:
                chiffre -= 9
        total += chiffre
    return str((10 - total % 10) % 10)


def siren(base8: str) -> str:
    return base8 + cle_luhn(base8)


def tva_francaise(numero_siren: str) -> str:
    """Numéro de TVA intracommunautaire français, clé de contrôle comprise."""
    cle = (12 + 3 * (int(numero_siren) % 97)) % 97
    return f"FR{cle:02d}{numero_siren}"


def livraison(jours: int) -> str:
    """Date de livraison ou de prestation, située dans le passé."""
    return (AUJOURD_HUI - timedelta(days=jours)).isoformat()


def echeance(delai: int = 30) -> str:
    """Échéance à ``delai`` jours de la DATE DE FACTURE, pas de la livraison.

    La règle Schematron FR-CTC BR-FR-CO-07 impose une échéance postérieure ou
    égale à la date de facture. Les factures étant émises aujourd'hui, une
    échéance calculée depuis une livraison ancienne tomberait dans le passé et
    la validation la refuserait, à juste titre.
    """
    return (AUJOURD_HUI + timedelta(days=delai)).isoformat()


VENDEUR = {
    "legal_name": "Lemoine Ingenierie SAS",
    "siren": siren("48219307"),
    "address_line1": "14 quai de la Fosse",
    "postal_code": "44000",
    "city": "Nantes",
    "country_code": "FR",
    "legal_form": "SAS",
    "vat_regime": "reel_normal",
}
VENDEUR["vat_number"] = tva_francaise(VENDEUR["siren"])

CLIENTS = [
    {
        "name": "Menuiserie Lambert SARL",
        "email": "comptabilite@menuiserie-lambert.example",
        "siren": siren("39471628"),
        "address_line1": "7 rue des Charmilles",
        "postal_code": "35000",
        "city": "Rennes",
        "country_code": "FR",
    },
    {
        "name": "Atelier Bertin",
        "email": "marie.bertin@atelier-bertin.example",
        "siren": siren("51038294"),
        "address_line1": "28 cours Gambetta",
        "postal_code": "69007",
        "city": "Lyon",
        "country_code": "FR",
    },
    {
        "name": "Groupe Vasseur SA",
        "email": "fournisseurs@groupe-vasseur.example",
        "siren": siren("72640518"),
        "address_line1": "3 boulevard de la Liberte",
        "postal_code": "59800",
        "city": "Lille",
        "country_code": "FR",
    },
    {
        "name": "Clinique du Parc SAS",
        "email": "achats@clinique-du-parc.example",
        "siren": siren("60815427"),
        "address_line1": "45 avenue Thiers",
        "postal_code": "33100",
        "city": "Bordeaux",
        "country_code": "FR",
    },
    {
        "name": "Boulangerie Petit EURL",
        "email": "contact@boulangerie-petit.example",
        "siren": siren("83052916"),
        "address_line1": "12 place du Capitole",
        "postal_code": "31000",
        "city": "Toulouse",
        "country_code": "FR",
    },
    {
        "name": "Transports Rochard SAS",
        "email": "facturation@transports-rochard.example",
        "siren": siren("94173605"),
        "address_line1": "88 chemin des Aygalades",
        "postal_code": "13015",
        "city": "Marseille",
        "country_code": "FR",
    },
]

# Chaque entrée décrit une facture et le sort qu'on lui réserve.
#
# `cible` est le statut de transmission visé, None pour ne pas soumettre.
# L'ensemble couvre les huit statuts : brouillon, emise, deposee, recue,
# approuvee, encaissee, refusee, rejetee.
FACTURES = [
    {
        "client": 0,
        "categorie": "prestation_de_services",
        "livraison": livraison(3),
        "echeance": echeance(),
        "emettre": False,
        "facturx": False,
        "cible": None,
        "lignes": [
            ("Etude de faisabilite structure bois", "1", "1850.00", "20.00"),
            ("Releve sur site et metre", "6", "95.00", "20.00"),
        ],
    },
    {
        "client": 4,
        "categorie": "mixte",
        "livraison": livraison(6),
        "echeance": echeance(),
        "emettre": False,
        "facturx": False,
        "cible": None,
        "lignes": [
            ("Maintenance annuelle four a sole", "1", "740.00", "20.00"),
            ("Jeu de plaques refractaires", "4", "128.50", "20.00"),
            ("Deplacement technicien", "2", "65.00", "10.00"),
        ],
    },
    {
        "client": 1,
        "categorie": "prestation_de_services",
        "livraison": livraison(12),
        "echeance": echeance(),
        "emettre": True,
        "facturx": False,
        "cible": None,
        "lignes": [("Accompagnement mise en conformite ERP", "3", "680.00", "20.00")],
    },
    {
        "client": 2,
        "categorie": "mixte",
        "livraison": livraison(18),
        "echeance": echeance(45),
        "emettre": True,
        "facturx": True,
        "cible": None,
        "lignes": [
            ("Fourniture et pose de garde-corps", "1", "3980.00", "20.00"),
            ("Certificat de conformite", "1", "976.60", "20.00"),
        ],
    },
    {
        "client": 3,
        "categorie": "livraison_de_biens",
        "livraison": livraison(25),
        "echeance": echeance(45),
        "emettre": True,
        "facturx": True,
        "cible": "deposee",
        "lignes": [
            ("Mobilier de salle d attente", "12", "640.00", "20.00"),
            ("Livraison et montage", "1", "1960.00", "20.00"),
        ],
    },
    {
        "client": 5,
        "categorie": "prestation_de_services",
        "livraison": livraison(32),
        "echeance": echeance(),
        "emettre": True,
        "facturx": True,
        "cible": "deposee",
        "lignes": [
            ("Audit de flotte et preconisations", "1", "2890.00", "20.00"),
            ("Formation eco-conduite", "8", "195.00", "20.00"),
        ],
    },
    {
        "client": 0,
        "categorie": "prestation_de_services",
        "livraison": livraison(41),
        "echeance": echeance(),
        "emettre": True,
        "facturx": True,
        "cible": "recue",
        "lignes": [
            ("Maitrise d oeuvre extension atelier", "1", "4230.00", "20.00"),
            ("Dossier de permis de construire", "1", "300.00", "20.00"),
        ],
    },
    {
        "client": 1,
        "categorie": "prestation_de_services",
        "livraison": livraison(54),
        "echeance": echeance(),
        "emettre": True,
        "facturx": True,
        "cible": "approuvee",
        "lignes": [("Maintenance technique mensuelle", "1", "2195.00", "20.00")],
    },
    {
        "client": 2,
        "categorie": "mixte",
        "livraison": livraison(68),
        "echeance": echeance(),
        "emettre": True,
        "facturx": True,
        "cible": "encaissee",
        "lignes": [
            ("Rehabilitation reseau air comprime", "1", "7450.00", "20.00"),
            ("Mise en service et essais", "1", "750.00", "20.00"),
        ],
    },
    {
        "client": 3,
        "categorie": "livraison_de_biens",
        "livraison": livraison(84),
        "echeance": echeance(60),
        "emettre": True,
        "facturx": True,
        "cible": "encaissee_partielle",
        "lignes": [
            ("Centrale de traitement d air", "1", "21500.00", "20.00"),
            ("Gaines et accessoires", "1", "1290.92", "20.00"),
        ],
    },
    {
        "client": 5,
        "categorie": "livraison_de_biens",
        "livraison": livraison(118),
        "echeance": echeance(45),
        "emettre": True,
        "facturx": True,
        "cible": "refusee",
        "lignes": [
            ("Groupe froid pour semi-remorque", "1", "9750.00", "20.00"),
            ("Kit de montage et raccordement", "1", "620.00", "20.00"),
        ],
    },
    {
        "client": 4,
        "categorie": "prestation_de_services",
        "livraison": livraison(140),
        "echeance": echeance(60),
        "emettre": True,
        "facturx": True,
        "cible": "rejetee",
        "lignes": [
            ("Mise aux normes electriques du fournil", "1", "3280.00", "20.00"),
            ("Consuel et demarches administratives", "1", "290.00", "20.00"),
        ],
    },
]


class Api:
    """Client HTTP minimal, qui s'arrête net sur une réponse inattendue."""

    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=180)
        self.jeton: str | None = None

    def appel(self, methode: str, chemin: str, attendu: tuple[int, ...], **kw) -> httpx.Response:
        entetes = {"Authorization": f"Bearer {self.jeton}"} if self.jeton else {}
        reponse = self.http.request(methode, chemin, headers=entetes, **kw)
        if reponse.status_code not in attendu:
            raise SystemExit(
                f"\nArrêt : {methode} {chemin} a répondu {reponse.status_code}, "
                f"attendu {attendu}.\n{reponse.text[:400]}"
            )
        return reponse


def verifier_api(api: Api, base_url: str) -> None:
    try:
        api.appel("GET", "/health", (200,))
    except httpx.HTTPError:
        raise SystemExit(
            f"Arrêt : aucune application ne répond sur {base_url}.\n"
            "Lancez d'abord : python scripts/serve.py"
        ) from None


def creer_tenant(api: Api) -> dict:
    inscription = api.appel(
        "POST",
        "/auth/signup",
        (201,),
        json={
            "tenant_name": NOM_TENANT,
            "email": EMAIL_DEMO,
            "password": MOT_DE_PASSE_DEMO,
        },
    ).json()
    api.jeton = inscription["access_token"]
    return inscription


def avancer_statut(api: Api, transmission_id: str, statut: str, **extra) -> None:
    """Pilote la plateforme simulée, puis relit le statut côté application."""
    api.appel(
        "POST",
        f"/transmissions/{transmission_id}/simulate",
        (204,),
        json={"status": statut, **extra},
    )
    api.appel("POST", f"/transmissions/{transmission_id}/refresh", (200,))


def deposer_et_avancer(api: Api, facture: dict, cible: str, paye_le: str) -> None:
    transmission = api.appel("POST", f"/invoices/{facture['id']}/submit", (201,)).json()
    tid = transmission["id"]

    if cible == "deposee":
        return
    if cible == "rejetee":
        avancer_statut(api, tid, "rejetee", reason="Identifiant de routage inconnu.")
        return

    avancer_statut(api, tid, "recue")
    if cible == "recue":
        return
    if cible == "refusee":
        avancer_statut(api, tid, "refusee", reason="Materiel non conforme au bon de commande.")
        return

    avancer_statut(api, tid, "approuvee")
    if cible == "approuvee":
        return

    total = Decimal(facture["total_ttc"])
    if cible == "encaissee":
        avancer_statut(api, tid, "encaissee", paid_amount=str(total), paid_at=paye_le)
    elif cible == "encaissee_partielle":
        # Deux versements successifs : le statut encaissee est répétable, et
        # l'interface additionne les montants réellement payés.
        acompte = (total / 2).quantize(Decimal("0.01"))
        avancer_statut(api, tid, "encaissee", paid_amount=str(acompte), paid_at=paye_le)
        avancer_statut(api, tid, "encaissee", paid_amount=str(total - acompte), paid_at=paye_le)


def construire_factures(api: Api, clients: list[dict]) -> None:
    print(f"\n[4] Factures ({len(FACTURES)})")
    for rang, spec in enumerate(FACTURES, start=1):
        client = clients[spec["client"]]
        brouillon = api.appel(
            "POST",
            "/invoices",
            (201,),
            json={
                "customer_id": client["id"],
                "operation_category": spec["categorie"],
                "supply_date": spec["livraison"],
                "due_date": spec["echeance"],
                "lines": [
                    {
                        "designation": designation,
                        "quantity": quantite,
                        "unit_price_ht": prix,
                        "vat_rate": taux,
                    }
                    for (designation, quantite, prix, taux) in spec["lignes"]
                ],
            },
        ).json()

        if not spec["emettre"]:
            print(
                f"    {rang:>2}. brouillon           "
                f"{brouillon['total_ttc']:>11} EUR  {client['name']}"
            )
            continue

        facture = api.appel("POST", f"/invoices/{brouillon['id']}/issue", (200,)).json()

        if not spec["facturx"]:
            print(
                f"    {rang:>2}. emise n°{facture['number']:<11} "
                f"{facture['total_ttc']:>11} EUR  sans Factur-X"
            )
            continue

        api.appel("POST", f"/invoices/{facture['id']}/cii", (200, 201))
        api.appel("POST", f"/invoices/{facture['id']}/facturx", (200, 201))

        if spec["cible"] is None:
            print(
                f"    {rang:>2}. emise n°{facture['number']:<11} "
                f"{facture['total_ttc']:>11} EUR  Factur-X genere"
            )
            continue

        # Un encaissement constaté est forcément passé : on le place peu après
        # la livraison, sans jamais dépasser aujourd'hui.
        paye_le = min(
            date.fromisoformat(spec["livraison"]) + timedelta(days=25), AUJOURD_HUI
        ).isoformat()
        deposer_et_avancer(api, facture, spec["cible"], paye_le)
        print(
            f"    {rang:>2}. emise n°{facture['number']:<11} "
            f"{facture['total_ttc']:>11} EUR  -> {spec['cible']}"
        )


def main() -> int:
    parseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parseur.add_argument("--base-url", default="http://localhost:8000")
    arguments = parseur.parse_args()

    api = Api(arguments.base_url)
    verifier_api(api, arguments.base_url)

    print(f"Application joignable sur {arguments.base_url}.")

    print(f"\n[1] Client de demonstration « {NOM_TENANT} »")
    inscription = creer_tenant(api)
    tenant_id = inscription["tenant_id"]
    print(f"    identifiant : {tenant_id}")

    print("\n[2] Profil vendeur")
    api.appel("PUT", "/company-profile", (200,), json=VENDEUR)
    print(f"    {VENDEUR['legal_name']}, SIREN {VENDEUR['siren']}, TVA {VENDEUR['vat_number']}")

    print(f"\n[3] Clients ({len(CLIENTS)})")
    clients = []
    for fiche in CLIENTS:
        cree = api.appel("POST", "/customers", (201,), json=fiche).json()
        clients.append(cree)
        print(
            f"    {cree['name']:<26} SIREN {cree['siren']}  {fiche['postal_code']} {fiche['city']}"
        )

    construire_factures(api, clients)

    verification = api.appel("GET", "/audit/verify", (200,)).json()
    print(
        f"\n[5] Journal d'audit : {verification['entries']} entrees, "
        f"chaine intacte : {verification['intact']}"
    )

    largeur = 68
    print("\n" + "=" * largeur)
    print("  IDENTIFIANTS DE DEMONSTRATION")
    print("=" * largeur)
    print(f"  Identifiant du tenant : {tenant_id}")
    print(f"  Adresse e-mail        : {EMAIL_DEMO}")
    print(f"  Mot de passe          : {MOT_DE_PASSE_DEMO}")
    print("=" * largeur)
    print(f"  Interface             : {arguments.base_url}/")
    print()
    print("  Compte jetable, destine a l'essai du logiciel. Ne pas le")
    print("  reutiliser ailleurs, et ne pas s'en servir en production.")
    print()
    print("  Rappel : pytest efface toutes les donnees, y compris celles-ci.")
    print("  Relancer ce script apres avoir joue les tests.")
    print("=" * largeur)
    return 0


if __name__ == "__main__":
    sys.exit(main())
