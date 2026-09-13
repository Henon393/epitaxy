# Projet : SaaS de facturation électronique (Solution Compatible), apprentissage

## Objectif
Projet personnel d'apprentissage, pas destiné à la production commerciale.
Priorités pédagogiques : multi-tenant, sécurité by design, format Factur-X normé.
Statut réglementaire cible : Solution Compatible adossée à une Plateforme Agréée (PA).
La PA est mockée pour l'instant, une vraie sandbox sera branchée en fin de parcours.

## Stack
- Python 3.12, FastAPI, SQLAlchemy 2.x, Pydantic v2
- PostgreSQL avec Row-Level Security (RLS)
- Alembic pour les migrations
- Celery ou RQ + Redis pour l'asynchrone (étapes ultérieures)
- Docker et docker compose
- Tests : pytest

## Commandes
- Démarrer l'infra (Postgres, Redis) : `docker compose up -d`
- Lancer l'API en dev : `uvicorn app.main:app --reload`
- Tous les tests : `pytest`
- Tests d'isolation tenant uniquement : `pytest tests/test_tenant_isolation.py`
- Créer une migration : `alembic revision --autogenerate -m "message"`
- Appliquer les migrations : `alembic upgrade head`
- Lint et format : `ruff check .` puis `ruff format .`

## Architecture
- Multi-tenant par RLS PostgreSQL, avec une colonne `tenant_id` sur chaque table applicative.
- Un middleware injecte le contexte tenant (SET LOCAL) au début de chaque requête. Aucune requête ne s'exécute sans contexte tenant.
- La couche d'accès aux données refuse toute requête hors contexte tenant.
- Connecteur PA derrière une interface `PaConnector`. Implémentation mock au démarrage, sandbox réelle plus tard.
- Génération du format via la librairie `factur-x`, avec validation XSD et Schematron systématique avant toute soumission.

## Règles de travail
- Discipline explore, plan, code, commit. Proposer un plan avant d'écrire du code sur les étapes structurantes, et attendre validation.
- Écrire les tests d'isolation tenant EN PRIORITÉ. Ne pas avancer tant qu'ils ne sont pas verts.
- La sécurité est le fil rouge, pas une étape finale : secrets jamais en clair, chiffrement au repos, journal d'audit inaltérable, RBAC, MFA, mesures RGPD article 32.
- Commits atomiques, messages à l'impératif.
- Chaque étape est livrée avec ses tests avant de passer à la suivante.

## Ne pas faire
- Ne jamais désactiver ni contourner la RLS pour simplifier.
- Ne pas mettre de secret (mot de passe, clé d'API PA) en clair dans le code ou les images Docker.
- Ne pas exécuter de requête SQL sans contexte tenant.
- Ne pas transmettre de facture à une vraie PA tant que le mock n'est pas remplacé volontairement.
- Ne pas ajouter de dépendance lourde sans justification.
