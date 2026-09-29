#!/usr/bin/env python
"""Démarre l'application avec des réglages de sécurité sûrs par défaut.

Raison d'être de ce script : uvicorn honore les en-têtes de proxy par
défaut, et considère l'adresse de bouclage comme une source de confiance.
Lancée directement, l'application croit donc tout `X-Forwarded-For` reçu
depuis la machine locale, et réécrit l'adresse cliente en conséquence.

Cette adresse sert à limiter les tentatives de connexion. Un attaquant
qui fait tourner l'en-tête présente une adresse différente à chaque essai
et échappe entièrement au blocage. Le réglage par défaut est donc le
mauvais dès que rien ne se trouve devant l'application.

Ce script impose l'inverse : aucun en-tête de proxy n'est cru, sauf
quand on déclare explicitement l'adresse du proxy qui se trouve devant.
Voir docs/deploiement.md pour ce cas.

Usage :
    python scripts/serve.py                      # local, aucun proxy
    python scripts/serve.py --reload             # avec rechargement
    python scripts/serve.py --forwarded-allow-ips 10.0.0.7   # derrière un proxy
"""

import argparse
import sys

import uvicorn

APPLICATION = "app.main:app"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Démarre l'API et l'interface avec des réglages sûrs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="défaut : 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="défaut : 8000")
    parser.add_argument(
        "--reload", action="store_true", help="recharge à chaque modification du code"
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default=None,
        metavar="ADRESSES",
        help=(
            "adresses du ou des proxys inverses autorisés à fixer X-Forwarded-For, "
            "séparées par des virgules. Sans cette option, aucun en-tête de proxy "
            "n'est pris en compte."
        ),
    )
    args = parser.parse_args()

    derriere_proxy = args.forwarded_allow_ips is not None
    if derriere_proxy:
        adresses = [a.strip() for a in args.forwarded_allow_ips.split(",") if a.strip()]
        if not adresses:
            parser.error("--forwarded-allow-ips attend au moins une adresse.")
        if "*" in adresses:
            # Faire confiance à tout le monde revient à ne pas faire
            # confiance du tout : n'importe quel client pourrait alors
            # choisir l'adresse sur laquelle il est compté.
            parser.error(
                "--forwarded-allow-ips n'accepte pas '*' : indiquer l'adresse précise "
                "du proxy inverse."
            )
        posture = "Proxy inverse déclaré, en-têtes de transfert acceptés depuis : " + ", ".join(
            adresses
        )
    else:
        posture = "Aucun proxy déclaré : les en-têtes de transfert sont ignorés."

    # Vidage immédiat : uvicorn journalise sur la sortie d'erreur, non
    # tamponnée. Sans cela, la posture de sécurité s'afficherait après ses
    # propres lignes, là où personne ne la lit.
    print(posture, flush=True)
    print(f"Application sur http://{args.host}:{args.port}", flush=True)
    uvicorn.run(
        APPLICATION,
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Le drapeau et la liste vont de pair : sans proxy déclaré, la prise
        # en compte des en-têtes est coupée, et non simplement restreinte.
        proxy_headers=derriere_proxy,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )


if __name__ == "__main__":
    sys.exit(main())
