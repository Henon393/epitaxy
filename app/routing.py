"""Classe de route qui valide la transaction avant d'envoyer la réponse.

Problème corrigé : la session de requête était validée dans la sortie de
contexte de la dépendance ``get_db``. Or cette sortie s'exécute après que la
réponse a été envoyée au client. Une route pouvait donc confirmer une
création par un ``201`` alors que sa transaction n'était pas encore validée,
et une requête enchaînée ne voyait pas l'objet. Mesuré sous charge, le cas
se produisait systématiquement : une facture créée puis émise aussitôt
recevait ``404 Facture introuvable``.

Emplacement retenu : le gestionnaire de route. Il reprend la main après la
sérialisation de la réponse, donc les objets ont déjà été lus dans la
transaction ouverte, et avant que la réponse ne parte. C'est la seule
fenêtre qui satisfait les deux contraintes.

Effet second, volontairement conservé : un échec du commit lui-même se
traduit désormais par une erreur serveur. Auparavant il survenait après
l'envoi du ``200``, et le client était informé d'un succès pour une
transaction annulée sans que rien ne le signale.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from app.db import session_de_la_requete


class RouteValideeAvantReponse(APIRoute):
    """APIRoute validant la transaction de la requête avant de répondre."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        gestionnaire_initial = super().get_route_handler()

        async def gestionnaire(requete: Request) -> Response:
            reponse = await gestionnaire_initial(requete)

            session = session_de_la_requete(requete.scope)
            # Pas de session pour les routes qui n'accèdent pas aux données,
            # et pas de transaction ouverte pour celles qui n'ont fait que
            # lire sans rien émettre.
            if session is not None and session.in_transaction():
                # Hors boucle d'événements : le pilote est synchrone, et un
                # commit peut s'allonger sous contention du journal d'audit.
                await run_in_threadpool(session.commit)

            return reponse

        return gestionnaire
