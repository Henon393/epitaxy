# Référence Étape 4b — Factur-X

Étape scindée en deux sous-étapes :
- **4b-1** : génération du XML CII conforme EN 16931, validé XSD + Schematron
  FR-CTC. Pas de PDF.
- **4b-2** : génération du PDF et packaging PDF/A-3 (Factur-X complet).

## Dépendance

`factur-x` (Akretion, BSD) — librairie de référence : génère le CII depuis un
dict EN 16931 (`generate_cii_xml`), embarque les XSD et Schematrons officiels
(dont les règles françaises FR-CTC), tire `saxonche` pour l'exécution
Schematron. Structure du dict d'entrée : se référer à
`tests/test_generate_xml.py` de la lib installée.

## Contrats 4b-1

- Le service lit **uniquement** les colonnes de snapshot d'une facture émise
  (`seller_*`, `buyer_*`, totaux, `vat_breakdown`, lignes) — jamais
  `customers` ni `company_profiles` : la facture émise est autoportante (4a).
- Profil EN 16931 : BT-24 = `urn:cen.eu:en16931:2017`.
- Validation systématique avant tout stockage : `xml_check_xsd` puis
  `xml_check_schematron(check_option='fr-ctc')`. Échec ⇒ aucun artefact,
  facture non transmissible, rapport failed-assert remonté intact.
- Totaux et ventilation du XML = ceux du snapshot 4a, **sans recalcul** côté
  XML (BR-CO-14 / BR-CO-15 cohérentes par construction).
- Génération **hors** de la transaction d'émission (le verrou compteur reste
  court).

## Catégories de TVA (mapping strict, hérité de 4a)

| Cas (test sur `is None`, jamais sur la valeur) | Catégorie | Motif |
|---|---|---|
| `vat_rate` > 0 (régime réel) | `S` | — |
| `vat_rate == Decimal("0")` (régime réel, taux zéro) | `Z` | aucun |
| `vat_rate is None` (franchise en base) | `E` | « TVA non applicable, art. 293 B du CGI » |

## Stockage des artefacts

Table `invoice_artifacts`, même régime d'inaltérabilité que `audit_log` :
RLS `ENABLE + FORCE` avec policies `FOR SELECT` / `FOR INSERT` seulement,
`REVOKE UPDATE, DELETE` pour `app_user`, unicité `(invoice_id, kind)`.
Le XML d'une facture émise ne se réécrit pas ; sha256 stocké pour
l'intégrité (transmission PA, chaînage éventuel en 3b). La présence de
l'artefact validé vaut « transmissible » — pas de flag sur `invoices`,
qui est immuable.

## Question tranchée empiriquement — franchise en base et catégorie E

Problème : en franchise, le vendeur n'a pas nécessairement d'identifiant TVA
(BT-31 absent), or la règle BR-E-10 (EN 16931) exige, en présence d'une
ventilation E, un identifiant TVA vendeur (BT-31), un enregistrement fiscal
vendeur (BT-32) ou un identifiant TVA acheteur (BT-48). Les règles FR-CTC
peuvent en outre imposer un code VATEX (BT-121) en plus ou à la place du
motif texte (BT-120).

Protocole : générer le XML d'une facture en franchise (profil sans
`vat_number`), le passer au Schematron FR-CTC, lire les failed-assert,
ajuster le mapping (BT-120 seul, BT-121 VATEX, BT-31/BT-32), re-valider.

### Résultats empiriques (constatés pendant 4b-1, factur-x 5.1, saxonche 13)

Failed-assert rencontrés sur le XML « naïf », et corrections retenues :

**Franchise en base (la question posée)**
- `BR-E-02` (schematron **de base**, pas FR-CTC) : lignes en catégorie E ⇒
  BT-31, BT-32 ou BT-63 exigé. C'est la règle ligne (BR-E-02) qui a tiré,
  pas BR-E-10 (ventilation). **Correction : BT-32 = SIREN du vendeur**
  (enregistrement fiscal, émis avec schemeID `FC` par la lib), uniquement
  quand BT-31 est absent.
- **Aucun code VATEX (BT-121) exigé** : le motif texte BT-120
  « TVA non applicable, art. 293 B du CGI » suffit au FR-CTC actuel.
  Catégorie retenue : **E + BT-120 seul, taux 0 (BR-E-05)**.

**Champs conditionnels FR-CTC, hors franchise (tous constatés)**
- `BR-FR-05/BT-22` × 3 : trois notes BG-1 obligatoires, chacune sous son
  code BT-21 — `AAB` (escompte ou son absence), `PMD` (pénalités de
  retard), `PMT` (indemnité forfaitaire de recouvrement 40 €).
  **Correction : notes légales par défaut** (TODO : paramétrables par
  profil vendeur).
- `BR-FR-08/BT-23` : liste fermée B1, S1, M1, B2, S2, M2, B4, S4, M4, S5,
  S6, B7, S7 — « A1 » rejeté. **Correction : BT-23 = `B1`** (dépôt de
  facture B2B domestique).
- `BR-FR-12/BT-49` et `BR-FR-13/BT-34` : adresses électroniques acheteur
  **et** vendeur obligatoires. **Correction : SIREN en adresse électronique,
  schéma EAS `0002`, des deux côtés.**
- BG-16 (instructions de paiement) : **non exigé** par le FR-CTC actuel —
  surveillé comme demandé, aucun failed-assert.

**Contrainte XSD (hors Schematron)**
- La lib émet toujours `ApplicableHeaderTradeDelivery` ; vide, il est rejeté
  par le XSD CII (« element is not nillable »). **Correction : BT-72
  systématique** — date de livraison saisie, sinon réputée égale à la date
  d'émission.

## Régénération d'artefact

La régénération d'un XML après correction d'un bug de mapping passe par
`migrator` (suppression contrôlée de la ligne d'artefact puis re-POST),
jamais par `app_user` — même logique que la purge de rétention d'audit_log.
