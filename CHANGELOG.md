# Journal des modifications

Toutes les évolutions notables du projet epitaxy sont consignées ici, de la
version la plus récente à la plus ancienne.

Le format suit les conventions usuelles d'un journal des modifications, et la
numérotation des versions le versionnage sémantique.

## [0.3] - 2026-09-27

Durcissement de la sécurité.

### Ajouté

- Journalisation des actions sensibles portant sur les comptes utilisateurs :
  création d'un compte, changement de rôle, activation et désactivation. Chaque
  entrée consigne l'auteur de l'action, le compte concerné et la modification
  apportée, dans la même transaction que la modification elle-même.
- Gestion du cycle de vie des comptes réservée à l'administrateur, permettant
  de changer un rôle et d'activer ou désactiver un compte.
- Révocation immédiate des accès. La désactivation d'un compte et le
  changement de rôle prennent effet sans délai, y compris sur les sessions déjà
  ouvertes, sans attendre l'expiration des jetons en cours.
- Garde-fou empêchant un client de perdre son dernier administrateur actif.
- Plafond de tentatives de connexion par adresse, en complément de la limite
  existante par couple adresse et compte. Il compte les échecs et non les
  tentatives, afin de ne pas pénaliser un usage légitime derrière une adresse
  partagée.
- Script de lancement `scripts/serve.py`, qui applique par défaut des réglages
  de serveur sûrs et rend explicite la configuration attendue derrière un proxy
  inverse.
- Note de déploiement `docs/deploiement.md`, consacrée à la configuration
  derrière un proxy inverse.
- Revalidation systématique des fichiers de l'interface web, pour qu'un simple
  rafraîchissement suffise toujours à obtenir la dernière version.

### Modifié

- Fiabilisation de la confirmation des écritures : une création ou une émission
  n'est confirmée au client qu'une fois la transaction validée. Une requête
  enchaînée voit donc toujours l'objet, y compris sous forte charge. Un échec
  de validation se traduit par une erreur explicite, jamais par une réponse de
  succès.
- L'identité de l'appelant est désormais vérifiée à chaque requête par une
  lecture d'état, en complément de la vérification cryptographique du jeton.
  L'indisponibilité de ce contrôle entraîne un refus, jamais un accès.
- Le vocabulaire du journal d'audit a été étendu aux actions de cycle de vie
  des comptes.

### Supprimé

- Mécanisme de résolution du client par en-tête, hérité des premières étapes de
  développement et rendu inutile par l'authentification par jeton. Le jeton
  vérifié est désormais la seule voie d'entrée.

### Documentation

- Rapport de test d'intrusion `docs/pentest.md`, couvrant l'isolation entre
  clients, l'authentification, le contrôle des rôles et l'inaltérabilité du
  journal d'audit, complété du suivi des améliorations apportées ensuite.
- Conventions du projet `docs/conventions.md` : objectifs, pile technique,
  invariants d'architecture et méthode de travail.

## [0.2] - 2026-09-22

Interface web.

### Ajouté

- Application web complète, servie par l'application elle-même sur une origine
  unique, ce qui évite tout partage de ressources entre origines.
- Tableau de bord de synthèse : chiffres clés de l'activité, répartition des
  factures par statut et aperçu des dernières factures, avec sélection de la
  période.
- Liste des factures avec filtres par statut, compteurs et totaux qui suivent
  le filtre actif.
- Création de facture avec calcul en direct des totaux, ventilation de la TVA
  par taux et prise en charge des cinq taux en vigueur, dont le taux
  particulier de 2,10 %.
- Création de client sans quitter le formulaire de facture.
- Téléchargement du Factur-X depuis la liste des factures.
- Identité visuelle : logo, favicone, palette de couleurs de statut et
  typographie.

## [0.1] - 2026-09-21

Socle fonctionnel.

### Ajouté

- Cœur de facturation électronique multi-client, conçu selon le modèle de la
  Solution Compatible adossée à une Plateforme Agréée, tel que défini par la
  réforme française de la facturation électronique.
- Isolation entre clients portée par la base de données, au moyen des
  politiques Row-Level Security de PostgreSQL. Chaque requête s'exécute dans un
  contexte client, et aucune ne s'exécute sans lui.
- Authentification par jetons, avec rotation des jetons de rafraîchissement et
  détection de réutilisation.
- Second facteur d'authentification TOTP.
- Contrôle d'accès par rôles : administrateur, comptable et lecture seule.
- Journal d'audit en ajout seul, avec chaînage cryptographique HMAC-SHA256 des
  entrées et clé conservée hors de la base, assorti d'une route de vérification
  de l'intégrité de la chaîne.
- Génération de factures au format Factur-X, profil EN 16931, validées par
  schéma XSD, par les règles Schematron FR-CTC et par contrôle de conformité
  PDF/A-3b.
- Suivi du cycle de vie des transmissions : dépôt, réception, approbation,
  encaissement, refus et rejet, avec historique horodaté des changements de
  statut.
- Connecteur de plateforme derrière une interface, avec une implémentation
  simulée. Aucune transmission n'est adressée à une plateforme réelle à ce
  stade.
- Suite de tests automatisés couvrant l'isolation entre clients,
  l'authentification, les rôles, le journal d'audit et la conformité du format.

[0.3]: #03---2026-09-27
[0.2]: #02---2026-09-22
[0.1]: #01---2026-09-21
