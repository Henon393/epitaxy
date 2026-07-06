# Référence Étape 5 — Connecteur Plateforme Agréée (PA)

Document de référence pour l'étape 5. À déposer dans `docs/`.
Référence normative : norme AFNOR XP Z12-012 (formats et profils des messages Factures et Statuts de cycle de vie), spécifications externes DGFiP version 3.1 (31 octobre 2025), applicable au démarrage du 1er septembre 2026. Les versions 2.x sont périmées.

## Découpage

- **5a** : interface `PaConnector`, implémentation mock, machine à états du cycle de vie, soumission et interrogation de statut en synchrone. Cœur structurant.
- **5b** : asynchrone, worker Redis, polling automatique des statuts, retry et robustesse.
- **5c** : e-reporting (données B2C, international, encaissement), distinct de l'e-invoicing.

## Référentiel des statuts du cycle de vie

### Statuts obligatoires (imposés à toute PA)

| Statut | Code | Déclenché par | Nature | Terminal |
|---|---|---|---|---|
| Déposée | 200 | PA émettrice | Facture validée techniquement et entrée dans le flux | non |
| Rejetée | 213 | PA (émettrice ou réceptrice) | Rejet **technique** : format invalide, mention manquante, Factur-X corrompu, destinataire introuvable | **oui, irréversible** |
| Refusée | 210 | Destinataire (acheteur) | Refus **commercial** sur une facture techniquement conforme : prix, quantité, prestation non réalisée, double facturation | **oui, irréversible** |
| Encaissée | 212 | Fournisseur | Paiement reçu. Porte date et montant. Répétable (encaissements partiels). Obligatoire en TVA sur les encaissements | non |

### Statuts recommandés (facultatifs, suivi fin)

Émise, Reçue, Mise à disposition, Prise en charge, Approuvée, Approuvée partiellement, En litige, Suspendue, Paiement transmis. À modéliser a minima, sans les rendre obligatoires.

## Règles de la machine à états

- **Rejetée et Refusée sont des puits terminaux irréversibles.** Une fois atteints, aucune transition sortante. La facture est fiscalement morte : la suite (annulation comptable, réémission, avoir) est hors périmètre technique de la transmission.
- **Distinguer strictement rejet technique (Rejetée, par la PA) et refus commercial (Refusée, par l'acheteur).** Ce sont deux chemins et deux causes différents. Confondre les deux est l'erreur la plus coûteuse de la réforme. Un litige commercial n'est pas un refus fiscal.
- **Historique append-only.** Les statuts forment une suite d'événements horodatés, jamais un champ mutable. Le statut courant est le dernier événement. Même régime d'inaltérabilité que l'audit et les artefacts (REVOKE UPDATE/DELETE, RLS SELECT/INSERT).
- **Encaissée** porte des métadonnées de paiement (date, montant) et peut apparaître plusieurs fois.
- Les messages de statut circulent au format CDAR, transmis dans un délai de 24 heures ; quatre statuts remontent au concentrateur d'e-reporting du PPF. Ces échanges inter-PA relèvent de 5b et 5c, pas de 5a.

## Séparation facture immuable / transmission

La facture émise reste immuable (étape 4a). Les statuts de transmission ne la modifient jamais. Ils vivent dans une table de transmission rattachée à la facture par clé étrangère, avec ses événements de statut. Aucun champ de statut n'est ajouté à `invoices`.

## Routage — SIREN contre SIRET

En 4b-1, l'adresse électronique du destinataire (BT-49) a été mappée sur le SIREN en schéma EAS 0002, ce qui satisfait le Schematron. Mais dans le modèle CTC, l'adresse de routage sert à la PA à livrer la facture au bon **établissement**, et une société (SIREN) peut en compter plusieurs (SIRET distincts). Le vrai niveau de routage est donc le SIRET. En 5a, modéliser un identifiant de routage du destinataire, en actant que le SIREN est un proxy acceptable pour le mock et que le SIRET est le niveau cible pour une vraie PA.

## Ce que le mock simule, ce qui restera à brancher

Le `MockPaConnector` simule les réponses de la PA : attribution d'un identifiant de transmission et statut Déposée à la soumission, puis statuts suivants restituables à la demande pour exercer la machine à états. Ce qui restera à brancher sur une vraie PA : l'authentification, le format d'échange réel, l'annuaire des destinataires, et le canal de réception des statuts (relève de 5b).
