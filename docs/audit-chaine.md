# Chaînage cryptographique du journal d'audit (étape 3b)

## Principe

Chaque entrée d'`audit_log` porte `entry_hash = HMAC-SHA256(clé, canonique ‖
prev_hash)`, où `prev_hash` est le hash de l'entrée précédente de la chaîne
du tenant (genesis lié au tenant pour la première : `HMAC(clé,
"genesis:tenant_id")`, une chaîne ne se transplante pas d'un tenant à
l'autre). La clé (`APP_AUDIT_HMAC_KEY`) est tenue hors base, même régime que
les autres secrets.

Le chaînage **complète** le contrôle d'accès existant (REVOKE UPDATE/DELETE
+ RLS) : le contrôle d'accès **empêche**, le chaînage **détecte**, y
compris une altération par un rôle à pleins droits, par recalcul
(`GET /audit/verify`, admin, point de rupture exact).

## Sérialisation anti-fourche

Une chaîne est un ordre total : la tête (`audit_chain_heads`, un curseur,
jamais une source de vérité) est **verrouillée par ligne** dans `record()`,
façon compteur de factures 4a. Deux écritures concurrentes du même tenant se
sérialisent avant tout calcul : aucune fourche possible. Filet : UNIQUE
`(tenant_id, position)`. Coût assumé : les écritures d'audit d'un tenant se
sérialisent du `record()` au commit. Ordre des verrous uniforme (compteur de
factures puis tête d'audit) : pas de deadlock.

## Contenu canonique et version de schéma

Format v1 : champs en ordre fixe joints par `\n` (version, id, position,
tenant, acteur, action, cible, métadonnées en JSON trié compact, created_at
normalisé UTC ISO-8601, posé côté application pour être hachable).

`hash_schema_version` est stockée en clair sur chaque entrée **et** incluse
dans le canonique : le format pourra évoluer (v2, …) sans invalider la
vérifiabilité des entrées antérieures, la vérification appliquant à chaque
entrée le format de sa version. La v1 est gelée, y compris en copie dans la
migration 0007.

## Ce que le dispositif détecte

- Altération de n'importe quel champ d'une entrée chaînée, horodatage et
  métadonnées compris (entry_hash invalide).
- Suppression ou insertion au milieu de la chaîne (positions non contiguës,
  prev_hash rompu).
- **Re-chaînage sans la clé** : une chaîne recalculée en SHA-256 nu ou avec
  une autre clé produit des HMAC invalides.
- Transplantation d'une chaîne entre tenants (genesis lié au tenant).

## Ce qu'il ne détecte pas, dit sans détour

- **Un attaquant détenant la clé HMAC** et un accès en écriture à pleins
  droits peut re-chaîner une histoire falsifiée indistinguable.
- **La troncature de fin de chaîne** avec remise en arrière de la tête est
  invisible entre deux vérifications (la tête n'est qu'un curseur).
- **La ligne de base** : le re-chaînage de la migration 0007 établit la
  confiance à l'instant de la migration ; il ne certifie pas
  rétroactivement que l'historique antérieur n'avait pas déjà été altéré.
- **La rotation de la clé HMAC** imposerait un re-chaînage complet par
  migrator : la chaîne existante devient invérifiable sous une nouvelle clé
  (les HMAC anciens ne se recalculent qu'avec l'ancienne).

Ces quatre limites se traitent par **ancrage externe** périodique du hash de
tête (horodatage qualifié, publication, journal tiers), extension notée,
non implémentée.

## Collision chaînage / rétention RGPD

Le TODO de rétention de l'étape 3 (purge des données personnelles
d'audit_log par migrator) entre en tension frontale avec le chaînage :
purger des entrées casse la contiguïté des positions et le `prev_hash` du
successeur. Voie retenue au niveau conception, non implémentée :

- **Purge par segments ré-ancrés** : coupe à une position donnée, insertion
  d'une **entrée d'ancre** portant le hash de tête du segment purgé (et ses
  bornes), puis re-chaînage du reste sur cette ancre ; la vérification
  devient segmentée (chaque segment vérifiable, l'ancre attestant le
  segment disparu).
- L'**anonymisation en place** (effacer l'IP/email dans metadata sans tuer
  la ligne) est exclue comme alternative simple : le hash couvre tous les
  champs par conception, toute anonymisation serait détectée comme une
  altération, ce qui est le comportement voulu du dispositif, pas un bug.
