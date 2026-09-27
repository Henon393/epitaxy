# Conventions du projet

Ce document fixe les règles que je me suis données pour epitaxy : ce que le
projet cherche à démontrer, la pile retenue, les invariants d'architecture à
ne jamais enfreindre, et la méthode de travail. Le README présente le projet
à un lecteur extérieur ; ce document-ci s'adresse à celui qui écrit le code.

## Objectif

Projet personnel d'apprentissage, non destiné à la production commerciale.
Les priorités sont pédagogiques et assumées comme telles : le multi-tenant, la
sécurité pensée dès la conception, et un format Factur-X réellement conforme à
la norme plutôt qu'approximé.

Statut réglementaire visé : Solution Compatible adossée à une Plateforme
Agréée, et non plateforme immatriculée. Le connecteur vers la plateforme est
un mock ; une vraie sandbox sera branchée en fin de parcours.

## Pile technique

- Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2.
- PostgreSQL 16 avec Row-Level Security, Alembic pour les migrations.
- Redis 7 pour le rate limiting, les familles de jetons de rafraîchissement et
  la révocation de session.
- RQ pour le travail asynchrone, retenu plutôt que Celery : plus léger, avec
  les reprises sur échec intégrées et sans ordonnanceur séparé à exploiter.
- Docker et Docker Compose pour l'infrastructure et les conteneurs d'outils.
- pytest pour les tests, ruff pour le lint et le formatage.

L'interface web est servie par l'application elle-même, depuis `app/static`,
sur la même origine que l'API. Aucun CORS n'est donc nécessaire, et aucun ne
doit être ajouté.

Le démarrage passe par `scripts/serve.py`, qui coupe la prise en compte des
en-têtes de proxy. Lancée directement, uvicorn les accepte et traite
l'adresse de bouclage comme une source de confiance, ce qui permet
d'échapper à la limitation des tentatives de connexion en forgeant
`X-Forwarded-For`. Le cas du proxy inverse est traité dans
[deploiement.md](deploiement.md).

## Commandes

```bash
docker compose up -d                          # PostgreSQL et Redis
docker compose build pdf                      # image de rendu WeasyPrint
alembic upgrade head                          # appliquer les migrations
python scripts/serve.py --reload              # API et interface, sur le port 8000
pytest                                        # toute la suite
pytest tests/test_tenant_isolation.py         # isolation tenant seule
alembic revision --autogenerate -m "message"  # créer une migration
ruff check . && ruff format .                 # lint et formatage
```

Deux points à connaître avant de lancer la suite complète :

- elle exige l'infrastructure Docker. La RLS ne se teste pas sur SQLite, et
  les tests Factur-X invoquent réellement les conteneurs de rendu et de
  validation ;
- les fixtures exécutent `TRUNCATE customers, tenants CASCADE`, ce qui efface
  **toutes** les données de la base, y compris un jeu de démonstration.
  Regénérer les données de démonstration après les tests, jamais avant.

## Invariants d'architecture

Ces points ne se négocient pas. Les enfreindre pour aller plus vite vide le
projet de son intérêt.

**L'isolation repose sur la base, pas sur l'applicatif.** Chaque table métier
porte une colonne `tenant_id` et une politique RLS. Le rôle applicatif n'est
pas propriétaire des tables et ne dispose pas de `BYPASSRLS` : la politique
s'applique donc réellement à lui, ce que `FORCE ROW LEVEL SECURITY` garantit.

**Aucune requête ne s'exécute hors contexte tenant.** Un middleware pose le
contexte en début de requête via `SET LOCAL`, et la couche d'accès aux données
refuse toute requête qui en serait dépourvue.

**Le connecteur de plateforme est derrière une interface.** `PaConnector`
définit le contrat, le mock l'implémente. Une vraie plateforme se branchera
par une nouvelle implémentation et une valeur de configuration, sans
réécriture du code appelant.

**Le format est validé avant toute soumission.** La génération passe par la
bibliothèque `factur-x`, et la validation XSD puis Schematron est
systématique. Une validation silencieusement sautée serait pire que pas de
validation du tout.

## Règles de sécurité

**Ne jamais désactiver ni contourner la RLS**, même temporairement, même pour
un test. Si un test a besoin d'un accès transverse, il passe par un rôle
distinct et l'assume explicitement.

**Aucun secret en clair.** Ni dans le code, ni dans les images Docker, ni dans
les fichiers versionnés. Les secrets applicatifs n'ont volontairement aucune
valeur de repli : leur absence fait échouer le démarrage plutôt que de laisser
tourner l'application avec une clé par défaut.

**Aucune requête SQL hors contexte tenant.**

**Aucune transmission vers une vraie plateforme** tant que le mock n'a pas été
remplacé volontairement et en connaissance de cause.

**Le journal d'audit est en ajout seul.** Les droits de modification et de
suppression sont révoqués pour le rôle applicatif, et les politiques RLS
n'autorisent aucune mise à jour. Toute action sensible doit y laisser une
trace : une action non journalisée est un angle mort que le chaînage
cryptographique ne peut pas rattraper, puisqu'une entrée jamais écrite ne rompt
aucune chaîne.

**Toute mutation est journalisée dans la même transaction qu'elle.** Si
l'écriture d'audit échoue, la mutation est annulée avec elle. Pas de mutation
sans trace.

## Méthode de travail

- Explorer, puis planifier, puis coder, puis commiter. Sur les étapes
  structurantes, poser le plan avant d'écrire la première ligne.
- Écrire les tests d'isolation tenant en premier. Ne pas avancer tant qu'ils
  ne sont pas verts.
- Traiter la sécurité comme un fil rouge, jamais comme une étape finale :
  secrets hors du code, chiffrement au repos, journal d'audit vérifiable,
  contrôle des rôles, second facteur, obligations de l'article 32 du RGPD.
- Livrer chaque étape avec ses tests avant de passer à la suivante.
- Commits atomiques, messages à l'impératif.
- N'ajouter aucune dépendance lourde sans justification écrite, de préférence
  dans le commentaire qui accompagne la dépendance dans `pyproject.toml`.

## Conventions d'écriture

- Commentaires et docstrings en français.
- Les commentaires expliquent le **pourquoi**, pas le **quoi** : le code dit
  déjà ce qu'il fait. Un commentaire utile est celui qui documente un
  compromis, un piège, ou la raison d'un choix contre-intuitif.
- Pas de tiret cadratin, ponctuation classique.
- `ruff check` et `ruff format` passent avant chaque commit.
