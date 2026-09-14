# epitaxy

Application de facturation électronique multi-tenant, conçue selon le modèle Solution Compatible de la réforme française de la facturation électronique de septembre 2026. Elle génère des factures Factur-X au profil EN 16931, validées XSD, Schematron FR-CTC et PDF/A-3b, modélise le cycle de vie de transmission d'une Plateforme Agréée, et tient un journal d'audit en ajout seul dont toute falsification est détectable par chaînage cryptographique.

Positionnement réglementaire : epitaxy est une Solution Compatible destinée à s'adosser à une Plateforme Agréée, pas une plateforme immatriculée, et n'a fait l'objet d'aucune attestation de conformité. Statut : projet personnel d'apprentissage, non destiné à la production commerciale. Le connecteur de transmission est un mock : aucune transmission réelle n'a jamais eu lieu.

## Ce que ce projet démontre

- Isolation multi-tenant stricte au niveau de la base, appliquée et testée, pas seulement filtrée en applicatif.
- Authentification durcie : hachage argon2, JWT à vérification d'algorithme explicite, rotation des jetons de rafraîchissement avec détection de vol, RBAC.
- Génération Factur-X au profil EN 16931 de bout en bout, validée à quatre étages indépendants.
- Journal d'audit en ajout seul au niveau base, dont la falsification est détectable par chaînage cryptographique, avec une frontière de garantie explicitement documentée.
- RGPD pris en compte dès la conception, y compris ses tensions avec un journal en ajout seul.
- Discipline plan avant code sur chaque étape, et une suite de tests centrée sur la sécurité, l'isolation et la concurrence.

## Démarrage rapide

Prérequis :

- Docker et Docker Compose (PostgreSQL, Redis, et les deux conteneurs d'outils : rendu PDF WeasyPrint et validation veraPDF, invoqués à la demande).
- Python 3.12 ou plus récent.

Installation et lancement :

```bash
# 1. Secrets locaux (jamais commités)
cp .env.example .env
# éditer .env : remplacer chaque valeur change-me par un secret fort

# 2. Infrastructure (PostgreSQL 16 sur le port 5433, Redis 7)
docker compose up -d

# 3. Image du conteneur de rendu PDF (l'image veraPDF est tirée au premier usage)
docker compose build pdf

# 4. Environnement Python et dépendances
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows : .venv\Scripts\pip

# 5. Migrations (rôle migrator, propriétaire du schéma)
alembic upgrade head

# 6. API en développement
uvicorn app.main:app --reload

# 7. Tests
pytest
```

Variables d'environnement (voir `.env.example`) :

| Variable | Rôle | Défaut |
|---|---|---|
| `POSTGRES_SUPER_PASSWORD`, `POSTGRES_MIGRATOR_PASSWORD`, `POSTGRES_APP_PASSWORD` | Mots de passe des rôles PostgreSQL, injectés à la première initialisation du volume | Aucun : docker compose refuse de démarrer sans eux |
| `APP_DATABASE_URL` | Connexion de l'application, rôle `app_user` (non propriétaire, sans BYPASSRLS) | Aucun : l'API refuse de démarrer |
| `APP_JWT_SECRET` | Clé de signature des JWT (HS256) | Aucun : l'API refuse de démarrer |
| `APP_AUDIT_HMAC_KEY` | Clé HMAC du chaînage d'audit, tenue hors base | Aucun : l'API refuse de démarrer |
| `MIGRATOR_DATABASE_URL` | Connexion des migrations Alembic, rôle `migrator` | Aucun : `alembic upgrade` échoue |
| `SUPER_DATABASE_URL` | Connexion superutilisateur, réservée aux fixtures de test | Aucun : requis pour `pytest` seulement |
| `APP_ENVIRONMENT` | `dev`, `test` ou `prod` | `dev` |
| `APP_TENANT_HEADER_ENABLED` | Résolution du tenant par en-tête `X-Tenant-Id`, tolérée en dev uniquement (rejetée au démarrage hors dev) | `false` |
| `APP_REDIS_URL` | Rate limiting et révocation des refresh tokens | `redis://localhost:6379/0` |
| `APP_PDF_RENDER_COMMAND`, `APP_VERAPDF_COMMAND`, `APP_EXCHANGE_DIR` | Pilotage des conteneurs d'outils | `docker compose run ...`, `.exchange` |
| `APP_PA_CONNECTOR` | Connecteur Plateforme Agréée | `mock` (seule valeur admise) |

Les quatre secrets applicatifs (`APP_DATABASE_URL`, `APP_JWT_SECRET`, `APP_AUDIT_HMAC_KEY`, `MIGRATOR_DATABASE_URL`) n'ont volontairement aucune valeur par défaut dans le code : leur absence fait échouer le démarrage plutôt que de fonctionner avec une clé de repli.

## Démonstration

Un scénario reproductible déroule le parcours complet contre l'API en marche, sur un tenant jetable créé à la volée :

```bash
# infra, migrations et API lancées (voir Démarrage rapide), puis :
python scripts/demo.py
```

Le script ([scripts/demo.py](scripts/demo.py)) prouve la chaîne de bout en bout : création du tenant et de son admin, profil vendeur et client avec SIREN, facture multi-taux émise avec son numéro légal, XML CII validé XSD et Schematron FR-CTC, Factur-X validé par veraPDF et sauvé dans `demo_output/` (ouvrable dans un lecteur PDF, le XML est embarqué), transmission à la PA mock avec cycle de vie jusqu'à Encaissée, chaîne d'audit re-vérifiée cryptographiquement, et deux preuves de sécurité : la modification d'une facture émise est refusée, et un second tenant ne voit rien du premier. Chaque étape affiche une ligne de résultat, et un récapitulatif clôt le parcours.

## Architecture

epitaxy est une API construite autour d'une base PostgreSQL dont l'isolation multi-tenant est portée par le moteur lui-même. Le parcours d'une facture suit une ligne claire : création d'un brouillon, émission qui fige les données et attribue un numéro légal, génération du Factur-X à partir des données figées, transmission via le connecteur PA avec suivi du cycle de vie des statuts, chaque opération sensible étant tracée dans un journal d'audit chaîné.

Deux traitements lourds sont volontairement sortis du flux synchrone principal et confiés à des conteneurs dédiés : le rendu du PDF et la validation PDF/A. La génération du Factur-X est menée hors de la transaction d'émission, qui doit rester courte.

```
                 ┌──────────────────────────────┐
 client ──JWT──► │  API FastAPI (app/)          │
                 │  middleware tenant + identité │
                 └──────┬──────────┬────────────┘
                        │          │
        SET LOCAL       │          │ rate limit, familles de refresh
        app.tenant_id   ▼          ▼
        ┌────────────────────┐   ┌─────────┐
        │ PostgreSQL 16      │   │ Redis 7 │
        │ RLS FORCE partout  │   └─────────┘
        │ triggers, audit    │
        │ chaîné, artefacts  │
        └────────────────────┘
                        │ artefacts cii_xml / facturx_pdf
                        ▼
        ┌────────────────────┐   ┌──────────────────┐
        │ conteneur pdf      │   │ conteneur verapdf │
        │ WeasyPrint PDF/A-3 │   │ contrôle flavour  │
        │ (docker/pdf)       │   │ 3b (JVM)          │
        └────────────────────┘   └──────────────────┘
                        │
                        ▼
        ┌────────────────────┐
        │ connecteur PA      │  mock aujourd'hui, worker
        │ (app/pa.py)        │  asynchrone Redis à venir
        └────────────────────┘
```

Le diagramme d'architecture complet (Mermaid) est dans [docs/architecture.md](docs/architecture.md).

## Décisions de conception

Cette section est le coeur du projet. Chaque choix répond à un problème précis et assume ses compromis.

### Isolation multi-tenant

Isolation par Row-Level Security PostgreSQL avec FORCE ROW LEVEL SECURITY, et un rôle applicatif non propriétaire des tables et sans BYPASSRLS. Pourquoi les deux ensemble : PostgreSQL n'applique pas la RLS au propriétaire d'une table par défaut, et FORCE plus un rôle non propriétaire garantissent que la politique s'applique réellement au code applicatif. L'isolation est vérifiée par un test de non-fuite du pool de connexions, qui prouve qu'aucun contexte de tenant ne subsiste d'une requête à la suivante sur une connexion réutilisée.

### Numérotation légale des factures

Numérotation continue et sans trou par tenant, obligation légale française, obtenue par un compteur verrouillé au niveau ligne dans la transaction d'émission. Pourquoi pas une séquence PostgreSQL : une séquence est non transactionnelle, donc un rollback consommerait tout de même le numéro et créerait un trou, ce qui est éliminatoire. Le verrou de ligne sérialise les émissions concurrentes d'un même tenant : transactionnel donc sans trou, verrouillé donc sans doublon, avec une contrainte d'unicité en filet indépendant.

### Immuabilité de la facture émise

Une facture émise est un document autoportant et figé. À l'émission, toutes les données qui figurent sur la facture, côté vendeur comme côté acheteur, sont figées dans un instantané. Pourquoi : relire les données du client ou du vendeur à l'affichage rendrait les factures déjà émises rétroactivement mutables si ces données changeaient. L'immuabilité est portée à deux niveaux : des déclencheurs de base figent la structure, en-tête et lignes, et l'instantané fige le contenu, de sorte que la facture ne dépend plus d'aucune table mutable.

### Régime de TVA et catégories

Distinction stricte entre une ligne à taux zéro et une ligne sans taux, testée sur l'absence de valeur et jamais sur la valeur elle-même. Cette frontière se prolonge dans les catégories de TVA du Factur-X : taux standard, taux zéro, et exonération en franchise en base avec la mention légale de l'article 293 B. Le cas de la franchise en base, courant chez les indépendants, est pris en charge de bout en bout.

### Validation Factur-X à quatre étages

Le Factur-X produit est validé à quatre niveaux distincts : conformité du conteneur PDF/A-3 par veraPDF, structure Factur-X, validité du XML par rapport au schéma XSD, et règles métier par le Schematron incluant les règles françaises FR-CTC. Le XML validé est celui qui est embarqué dans le PDF, jamais une nouvelle génération susceptible de diverger.

### Authentification et sessions

Hachage argon2, jetons JWT signés avec vérification explicite de l'algorithme au décodage pour fermer la classe des attaques de confusion d'algorithme, protection contre l'énumération de comptes par égalisation du temps de réponse, et rotation des jetons de rafraîchissement avec détection de réutilisation qui révoque toute la lignée de session à la première anomalie. Les secrets de signature sont lus hors de la base et n'ont pas de valeur par défaut.

### Audit en ajout seul et vérifiable

Le journal d'audit est en ajout seul au niveau base grâce au contrôle d'accès, révocation des droits de modification et de suppression pour le rôle applicatif et politiques RLS sans autorisation de mise à jour, et il est vérifiable grâce à un chaînage cryptographique. Chaque entrée porte le hash de la précédente, calculé en HMAC-SHA256 avec une clé tenue hors de la base. Le contrôle d'accès empêche, le chaînage détecte, ce sont deux menaces distinctes. La portée exacte de ce que le dispositif détecte et ne détecte pas est documentée séparément.

### Cycle de vie de transmission

Le cycle de vie des statuts est modélisé comme un historique en ajout seul, le statut courant étant le dernier événement et jamais un champ mutable. La machine à états distingue le rejet technique par la plateforme du refus commercial par l'acheteur, et verrouille les états terminaux par deux barrières, applicative et base. La facture et sa transmission sont strictement séparées : une transmission peut échouer sans que la facture bouge.

### Maîtrise de la concurrence

Un même motif, ordre plus contrainte d'unicité, dompte la concurrence à trois endroits : la numérotation des factures, la position des événements de statut, et la tête de la chaîne d'audit. À chaque fois, la source de vérité est la donnée elle-même, jamais un index maintenu à côté.

## Sécurité

Le modèle de menace couvert s'articule en cinq volets. L'isolation entre tenants est portée par PostgreSQL lui-même (RLS forcée, rôle applicatif sans privilège de contournement) et testée jusque dans la réutilisation des connexions du pool. L'authentification ferme les classes d'attaques connues sur les JWT (confusion d'algorithme, altération de claims, rejeu de refresh volé) et sur le login (énumération de comptes, force brute freinée par rate limiting Redis). Les secrets (signature JWT, clé HMAC d'audit, mots de passe des rôles) vivent hors de la base et hors du code, sans valeur de repli. Le journal d'audit est tamper-evident : le contrôle d'accès empêche la modification, le chaînage HMAC la détecte, y compris face à un rôle à pleins droits qui ne détient pas la clé. Le RGPD est traité dès la conception : minimisation des données d'audit, note de rétention, et tension entre purge et inaltérabilité résolue au niveau conception par une purge par segments ré-ancrés.

Points saillants : l'isolation testée par non-fuite du pool, la détection de vol de session, l'audit vérifiable dont la frontière de garantie est énoncée sans détour, et la prise en compte du RGPD, y compris la tension documentée entre l'inaltérabilité de l'audit et l'obligation de purge. Les frontières de garantie exactes sont dans [docs/audit-chaine.md](docs/audit-chaine.md).

## Conformité à la réforme 2026

epitaxy vise le profil EN 16931 du Factur-X, porte les mentions obligatoires dont les nouvelles mentions de la réforme, et gère les statuts du cycle de vie de la facture. Il se positionne en Solution Compatible adossée à une Plateforme Agréée, et ne prétend pas au statut de plateforme immatriculée, qui relève d'un processus d'immatriculation distinct.

Références suivies :

- Factur-X version 1.09 (publication commune FNFE-MPE / FeRD), profil EN 16931, identifiant de spécification `urn:cen.eu:en16931:2017`, schémas XSD et Schematrons officiels embarqués par la bibliothèque factur-x 5.x, règles françaises FR-CTC comprises.
- Spécifications externes DGFiP version 3.1 (31 octobre 2025), applicables au démarrage du 1er septembre 2026, pour la nomenclature des statuts du cycle de vie (les quatre statuts obligatoires 200, 210, 212, 213 et les recommandés 202, 205).
- Norme AFNOR XP Z12-012 pour les formats et profils des messages Factures et Statuts.

Le détail du mapping et les constats de validation sont dans [docs/facturx-4b-reference.md](docs/facturx-4b-reference.md) et [docs/etape-5-reference.md](docs/etape-5-reference.md).

## Périmètre et limites

Cette section est assumée et fait partie de la démonstration.

- Le connecteur vers la Plateforme Agréée est un mock. L'interface est conçue pour qu'une vraie plateforme se branche sans réécriture, mais aucune transmission réelle n'a lieu.
- Le routage de transmission utilise le SIREN comme approximation. Le vrai niveau de routage est le SIRET de l'établissement, prévu comme évolution.
- Hors périmètre à ce stade : les avoirs et la réémission après rejet ou refus, l'e-reporting, la récupération d'accès en cas de perte du second facteur sans code de secours, l'ancrage externe de la chaîne d'audit et la purge de rétention par segments ré-ancrés.
- À l'activation ou à la désactivation du second facteur, les sessions ouvertes sont invalidées au prochain rafraîchissement ; les jetons d'accès déjà émis restent valides jusqu'à leur expiration courte, compromis assumé d'un middleware sans accès base.
- La vérifiabilité de l'audit établit une ligne de base de confiance à l'instant de sa mise en place et ne certifie pas rétroactivement l'antériorité. Le détail des garanties figure dans le dossier docs/.

## Stack technique

Versions issues de `pyproject.toml` (bornes minimales) et de l'infrastructure :

- Langage et API : Python 3.12+, FastAPI 0.115+, Uvicorn 0.30+, Pydantic 2.7+ et pydantic-settings 2.3+.
- Données : PostgreSQL 16 (docker compose), SQLAlchemy 2.0.30+, psycopg 3.2+, Alembic 1.13+ pour les migrations.
- Cache et jetons : Redis 7 (docker compose), client redis 5.0+, PyJWT 2.8+, argon2-cffi 23.1+.
- Factur-X : factur-x 3.1+ (5.1 installée), saxonche 12+ (exécution Schematron, dépendance dure : sans elle la validation serait silencieusement sautée), Jinja2 3.1+ (template du PDF lisible), pypdf 4+ (contrôles de structure).
- Rendu et validation PDF : WeasyPrint 63+ dans un conteneur Linux dédié (sortie PDF/A-3b native), veraPDF (image officielle `verapdf/cli`, flavour 3b).
- Tests et qualité : pytest 8+, ruff, httpx.
- Conteneurisation : Docker Compose (infrastructure et conteneurs d'outils invoqués à la demande).

## Structure du dépôt

```
app/                      API FastAPI
  routers/                endpoints : auth, users, customers, company_profile,
                          invoices (+ artefacts CII et Factur-X), transmissions, audit
  templates/              template HTML du PDF lisible (Jinja2)
  middleware.py           résolution du tenant et de l'identité depuis le JWT
  db.py                   sessions sous contexte tenant obligatoire (SET LOCAL)
  models.py               modèles SQLAlchemy (tenants, users, factures, audit, PA)
  invoicing.py            calcul de TVA (Decimal, arrondi par taux) et numérotation
  cii.py                  construction et validation du XML CII EN 16931
  invoice_pdf.py          assemblage Factur-X et contrôles de structure
  pdf_render.py           pilotage du conteneur de rendu WeasyPrint
  verapdf.py              pilotage du conteneur veraPDF
  pa.py                   interface PaConnector, mock, machine à états
  audit.py                service d'écriture d'audit (fail-closed)
  audit_chain.py          chaînage HMAC et vérification d'intégrité
  security.py             argon2, émission et vérification des JWT
alembic/                  migrations : RLS, triggers d'immuabilité et de
                          transitions, tables append-only, chaînage de l'audit
docker/
  pdf/                    image WeasyPrint (rendu PDF/A-3b déterministe)
  postgres/               init des rôles migrator et app_user, default privileges
docs/                     références techniques et frontières de garantie
tests/                    103 tests (pytest), contre le vrai PostgreSQL
docker-compose.yml        PostgreSQL 16, Redis 7, conteneurs d'outils pdf et verapdf
.env.example              variables d'environnement documentées
```

## Tests

103 tests, lancés par `pytest` (infrastructure docker compose requise : la RLS ne se teste pas sur SQLite, et les tests Factur-X invoquent les conteneurs de rendu et de validation).

Familles couvertes :

- Isolation multi-tenant : lecture, écriture et suppression croisées, requête sans contexte, non-fuite du pool de connexions, isolation de chaque table applicative.
- Authentification et RBAC : claims altérés, confusion d'algorithme, expiration, anti-énumération par timing, rotation et révocation de famille de refresh, rate limiting, contrôle de rôle.
- Audit : inaltérabilité (UPDATE, DELETE, TRUNCATE refusés), fail-closed (audit en échec = mutation annulée), et tamper-evidence de la chaîne : altérations détectées à la position exacte, re-chaînage sans la clé détecté, absence de fourche sous insertions concurrentes.
- Facturation : numérotation sans trou ni doublon y compris sous émissions concurrentes, immuabilité via API et en SQL direct, TVA multi-taux et arrondis, mentions obligatoires, snapshots vendeur et acheteur figés.
- Conformité Factur-X : XSD et Schematron FR-CTC verts, catégories de TVA S, Z et E, génération déterministe, veraPDF 3b, structure Factur-X (nom, AFRelationship, MIME, XMP), round-trip octet à octet du XML embarqué.
- Cycle de vie PA : machine à états aux deux barrières, états terminaux verrouillés, distinction rejet technique et refus commercial, encaissements multiples, historique append-only.

## Documentation

Le dossier docs/ contient les références techniques et les frontières de garantie du projet, notamment le mapping vers la norme EN 16931 et les pièges de validation, la référence du connecteur PA et du cycle de vie des statuts, et la note dédiée à la portée du chaînage d'audit.

- [docs/facturx-4b-reference.md](docs/facturx-4b-reference.md) : référence Factur-X, mapping EN 16931, constats empiriques Schematron FR-CTC et PDF/A-3, stockage des artefacts.
- [docs/etape-5-reference.md](docs/etape-5-reference.md) : référence du connecteur PA, nomenclature des statuts DGFiP, règles de la machine à états, routage SIREN / SIRET.
- [docs/audit-chaine.md](docs/audit-chaine.md) : chaînage cryptographique de l'audit, sérialisation anti-fourche, ce que le dispositif détecte et ne détecte pas, tension avec la rétention RGPD.
