#!/usr/bin/env python
"""Démonstration bout en bout d'epitaxy, contre une API en marche.

Parcours : tenant jetable -> profil vendeur et client -> facture émise
(numéro légal) -> XML CII validé (XSD + Schematron FR-CTC) -> Factur-X
validé (veraPDF + structure + round-trip), PDF sauvé sur disque ->
soumission à la PA mock et cycle de vie jusqu'à Encaissée -> chaîne
d'audit vérifiée -> preuves de sécurité (immuabilité, isolation tenant).

Prérequis : infra démarrée (docker compose up -d), migrations appliquées
(alembic upgrade head), API lancée (uvicorn app.main:app), et l'image du
conteneur de rendu construite (docker compose build pdf).

Usage : python scripts/demo.py [--base-url http://localhost:8000]
"""

import argparse
import secrets
import sys
from datetime import date
from pathlib import Path

import httpx

LIGNES = [
    {
        "designation": "Prestation de conseil",
        "quantity": "2",
        "unit_price_ht": "450.00",
        "vat_rate": "20.00",
    },
    {
        "designation": "Formation sur site",
        "quantity": "1",
        "unit_price_ht": "300.00",
        "vat_rate": "10.00",
    },
    {
        "designation": "Documentation imprimée",
        "quantity": "10",
        "unit_price_ht": "12.00",
        "vat_rate": "5.50",
    },
]

_step = 0


def step(message: str) -> None:
    global _step
    _step += 1
    print(f"\n[{_step}] {message}")


def ok(message: str) -> None:
    print(f"    OK  {message}")


def fail(message: str, response: httpx.Response | None = None) -> None:
    detail = (
        f" -> HTTP {response.status_code} : {response.text[:300]}" if response is not None else ""
    )
    print(f"    ECHEC  {message}{detail}", file=sys.stderr)
    sys.exit(1)


def expect(response: httpx.Response, code: int, message: str) -> httpx.Response:
    if response.status_code != code:
        fail(message, response)
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base_url, timeout=120)

    # --- 0. Sanité -----------------------------------------------------------
    try:
        expect(client.get("/health"), 200, "API injoignable")
    except httpx.ConnectError:
        print(
            f"ECHEC  aucune API sur {args.base_url}.\n"
            "Lancer d'abord : docker compose up -d ; alembic upgrade head ; "
            "uvicorn app.main:app",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- 1. Tenant de démonstration, jetable ----------------------------------
    suffixe = secrets.token_hex(4)
    email = f"demo-{suffixe}@exemple.fr"
    password = secrets.token_urlsafe(16)
    step(f"Création du tenant de démonstration « Demo {suffixe} » et de son admin")
    signup = expect(
        client.post(
            "/auth/signup",
            json={"tenant_name": f"Demo {suffixe}", "email": email, "password": password},
        ),
        201,
        "signup",
    ).json()
    tenant_id = signup["tenant_id"]
    entetes = {"Authorization": f"Bearer {signup['access_token']}"}
    ok(f"tenant {tenant_id[:8]}… créé, admin {email} connecté (JWT reçu)")

    # --- 2. Profil vendeur et client -------------------------------------------
    step("Profil vendeur (raison sociale, SIREN, adresse, TVA, régime réel)")
    expect(
        client.put(
            "/company-profile",
            headers=entetes,
            json={
                "legal_name": "Demo Conseil SAS",
                "siren": "123456789",
                "address_line1": "1 rue de la Paix",
                "postal_code": "75001",
                "city": "Paris",
                "country_code": "FR",
                "vat_number": "FR32123456789",
                "legal_form": "SAS",
                "vat_regime": "reel_normal",
            },
        ),
        200,
        "profil vendeur",
    )
    ok("profil vendeur complet (régime réel normal)")

    step("Client avec SIREN et adresse structurée")
    customer = expect(
        client.post(
            "/customers",
            headers=entetes,
            json={
                "name": "Acheteur Industriel SARL",
                "email": "compta@acheteur.example",
                "siren": "987654321",
                "address_line1": "2 avenue de la République",
                "postal_code": "69001",
                "city": "Lyon",
                "country_code": "FR",
            },
        ),
        201,
        "création client",
    ).json()
    ok(f"client « {customer['name']} », SIREN {customer['siren']}")

    # --- 3. Facture : brouillon puis émission -----------------------------------
    step("Facture de 3 lignes (TVA 20 / 10 / 5,5) en brouillon")
    brouillon = expect(
        client.post(
            "/invoices",
            headers=entetes,
            json={
                "customer_id": customer["id"],
                "operation_category": "prestation_de_services",
                "supply_date": date.today().isoformat(),
                "lines": LIGNES,
            },
        ),
        201,
        "création du brouillon",
    ).json()
    ok(
        f"brouillon créé : HT {brouillon['total_ht']} EUR, "
        f"TVA {brouillon['total_tva']} EUR, TTC {brouillon['total_ttc']} EUR"
    )

    step("Émission : numérotation légale et snapshot figé")
    emise = expect(
        client.post(f"/invoices/{brouillon['id']}/issue", headers=entetes), 200, "émission"
    ).json()
    ok(
        f"facture émise sous le numéro {emise['number']} "
        f"(statut {emise['status']}, vendeur et acheteur figés au snapshot)"
    )

    # --- 4. XML CII ---------------------------------------------------------------
    step("Génération du XML CII EN 16931 (validation fail-closed avant stockage)")
    cii = expect(
        client.post(f"/invoices/{emise['id']}/cii", headers=entetes), 201, "génération CII"
    )
    ok(f"XSD + Schematron FR-CTC : PASS ({len(cii.content)} octets, profil EN 16931)")

    # --- 5. Factur-X -----------------------------------------------------------------
    step("Assemblage du Factur-X (PDF/A-3b + XML embarqué), validé par veraPDF")
    facturx = expect(
        client.post(f"/invoices/{emise['id']}/facturx", headers=entetes),
        201,
        "assemblage Factur-X",
    )
    dossier = Path("demo_output")
    dossier.mkdir(exist_ok=True)
    chemin_pdf = dossier / f"facture-{emise['number']}-{suffixe}.pdf"
    chemin_pdf.write_bytes(facturx.content)
    ok("veraPDF 3b : PASS ; structure Factur-X et round-trip du XML : PASS")
    ok(f"PDF sauvé -> {chemin_pdf.resolve()}")

    # --- 6. Transmission PA et cycle de vie -------------------------------------------
    step("Soumission du Factur-X à la Plateforme Agréée (mock)")
    transmission = expect(
        client.post(f"/invoices/{emise['id']}/submit", headers=entetes), 201, "soumission"
    ).json()
    ok(
        f"transmission acceptée, statut {transmission['current_status']} (code 200), "
        f"routage {transmission['routing_identifier']}"
    )

    step("Cycle de vie : la PA fait avancer les statuts (mock piloté, ingestion réelle)")
    for statut, extra in (
        ("recue", {}),
        (
            "encaissee",
            {"paid_amount": emise["total_ttc"], "paid_at": date.today().isoformat()},
        ),
    ):
        expect(
            client.post(
                f"/transmissions/{transmission['id']}/simulate",
                headers=entetes,
                json={"status": statut, **extra},
            ),
            204,
            f"simulation {statut}",
        )
        expect(
            client.post(f"/transmissions/{transmission['id']}/refresh", headers=entetes),
            200,
            f"refresh {statut}",
        )
    cycle = expect(
        client.get(f"/invoices/{emise['id']}/transmission", headers=entetes), 200, "cycle de vie"
    ).json()
    chaine = " -> ".join(f"{e['status']} ({e['status_code']})" for e in cycle["events"])
    ok(f"cycle de vie : {chaine}")
    paiement = cycle["events"][-1]
    ok(f"encaissée : {paiement['paid_amount']} EUR le {paiement['paid_at']}")

    # --- 7. Chaîne d'audit ---------------------------------------------------------------
    step("Vérification cryptographique de la chaîne d'audit du tenant")
    audit = expect(client.get("/audit/verify", headers=entetes), 200, "vérification d'audit").json()
    if not audit["intact"]:
        fail(f"chaîne d'audit rompue à la position {audit['broken_at_position']}")
    ok(f"chaîne intègre : {audit['entries']} entrées re-vérifiées (HMAC bout à bout)")

    # --- 8. Preuves de sécurité ------------------------------------------------------------
    step("Preuve d'immuabilité : modification de la facture émise")
    modification = client.put(
        f"/invoices/{emise['id']}",
        headers=entetes,
        json={
            "customer_id": customer["id"],
            "operation_category": "mixte",
            "lines": [
                {
                    "designation": "Falsification",
                    "quantity": "1",
                    "unit_price_ht": "1.00",
                    "vat_rate": "20.00",
                }
            ],
        },
    )
    if modification.status_code != 409:
        fail("la facture émise aurait dû être immuable", modification)
    ok("refusée (409) : la facture émise est immuable, numéro et contenu figés")

    step("Preuve d'isolation : un second tenant ne voit rien du premier")
    autre = expect(
        client.post(
            "/auth/signup",
            json={
                "tenant_name": f"Intrus {suffixe}",
                "email": f"intrus-{suffixe}@exemple.fr",
                "password": secrets.token_urlsafe(16),
            },
        ),
        201,
        "signup second tenant",
    ).json()
    entetes_autre = {"Authorization": f"Bearer {autre['access_token']}"}
    liste = expect(
        client.get("/invoices", headers=entetes_autre), 200, "liste second tenant"
    ).json()
    acces_direct = client.get(f"/invoices/{emise['id']}", headers=entetes_autre)
    if liste != [] or acces_direct.status_code != 404:
        fail("l'isolation tenant aurait dû tout masquer", acces_direct)
    ok("liste vide et accès direct 404 : Row-Level Security étanche")

    # --- Récapitulatif ------------------------------------------------------------------------
    print(
        "\n"
        + "=" * 72
        + "\nRECAPITULATIF\n"
        + f"  Facture n° {emise['number']} émise ({emise['total_ttc']} EUR TTC), immuable.\n"
        + "  Factur-X EN 16931 généré et validé (XSD, Schematron FR-CTC, veraPDF) :\n"
        + f"    {chemin_pdf.resolve()}\n"
        + f"  Transmise à la PA mock : {' -> '.join(e['status'] for e in cycle['events'])}.\n"
        + f"  Journal d'audit vérifié intègre : {audit['entries']} entrées.\n"
        + "  Isolation tenant démontrée.\n"
        + "=" * 72
    )


if __name__ == "__main__":
    main()
