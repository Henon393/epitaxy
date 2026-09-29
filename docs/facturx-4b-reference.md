# Référence 4b : Génération Factur-X (EN 16931 / CII)

Document de référence pour l'étape 4b. Les résultats empiriques 4b-1
(Schematron FR-CTC réel) sont consignés en fin de document.
La structure exacte du dictionnaire attendu par la lib est dans `tests/test_generate_xml.py` de factur-x : s'y référer plutôt qu'aux numéros de BT ci-dessous, qui servent à comprendre le mapping.

## Profil et déclaration

- Profil visé : **EN 16931** (noyau complet). Suffisant pour une facture B2B domestique simple.
- Specification identifier (BT-24) : `urn:cen.eu:en16931:2017`.
- Version Factur-X : 1.09 (schemas et schematrons embarqués par factur-x 5.1).
- Le profil déclaré dans BT-24 doit être cohérent avec le XMP du PDF (`fx:ConformanceLevel`) et avec le contenu réel. Toute incohérence est un rejet.

## Stack et rôle de la lib factur-x (Akretion, 5.1, BSD)

- `generate_cii_xml(dict, profile)` : produit le XML CII depuis un dict à clés EN 16931 (BT-1, BG-25, ...).
- `xml_check_xsd(xml)` : validation syntaxique (forme).
- `xml_check_schematron(xml, check_option='fr-ctc')` : validation métier, règles EN 16931 **plus** règles françaises FR-CTC de la réforme. Dépend de saxonche.
- `generate_from_file(pdf, xml, check_schematron=True)` : embarque le XML dans un PDF pour produire le Factur-X.
- Limite : le PDF/A-3 n'est conforme que si le PDF d'entrée est déjà PDF/A (la lib copie OutputIntent et ID, elle ne convertit pas). Contrôle veraPDF en sortie recommandé.

## Mapping snapshot -> CII EN 16931

Toutes les données proviennent des colonnes de snapshot figées à l'émission, jamais de `customers` ni `company_profiles` en direct.

### En-tête
| Donnée | BT | Note |
|---|---|---|
| Numéro de facture | BT-1 | `number` |
| Date d'émission | BT-2 | **format `yyyymmdd`** (code 102), pas ISO 8601 |
| Type de facture | BT-3 | `380` (facture). `381` = avoir, pour plus tard |
| Devise | BT-5 | `EUR` |
| Date d'échéance | BT-9 | `due_date` |
| Profil | BT-24 | `urn:cen.eu:en16931:2017` |

### Vendeur (depuis seller_*)
| Donnée | BT / BG | Note |
|---|---|---|
| Groupe vendeur | BG-4 | |
| Nom / raison sociale | BT-27 | `seller_legal_name` |
| Identifiant légal (SIREN) | BT-30 | **schemeID `0002`** (SIRET = `0009`). Piège fréquent |
| Identifiant TVA | BT-31 | `seller_vat_number`, **seulement au régime réel** |
| Adresse (groupe) | BG-5 | |
| Ligne 1 / ligne 2 | BT-35 / BT-36 | `seller_address_line1/2` |
| Ville | BT-37 | `seller_city` |
| Code postal | BT-38 | `seller_postal_code` |
| Code pays | BT-40 | `seller_country_code`, **obligatoire**, ISO 3166-1 alpha-2 |

### Acheteur (depuis buyer_*)
| Donnée | BT / BG | Note |
|---|---|---|
| Groupe acheteur | BG-7 | |
| Nom | BT-44 | `buyer_name` |
| Identifiant légal (SIREN) | BT-47 | schemeID `0002`. C'est la nouvelle mention réforme |
| Identifiant TVA acheteur | BT-48 | seulement si requis (intracommunautaire, autoliquidation) |
| Adresse (groupe) | BG-8 | |
| Ligne 1 / ligne 2 | BT-50 / BT-51 | `buyer_address_line1/2` |
| Ville | BT-52 | `buyer_city` |
| Code postal | BT-53 | `buyer_postal_code` |
| Code pays | BT-55 | `buyer_country_code`, **obligatoire** |

### Lignes (BG-25, une par invoice_line)
| Donnée | BT | Note |
|---|---|---|
| Identifiant de ligne | BT-126 | |
| Désignation | BT-153 | `designation` |
| Quantité | BT-129 | `quantity` |
| Unité | BT-130 | code unité (ex `C62` = unité) |
| Prix unitaire net | BT-146 | `unit_price_ht` |
| Montant net de ligne | BT-131 | `total_ht` de la ligne |
| Code catégorie TVA ligne | BT-151 | S / Z / E (voir plus bas) |
| Taux TVA ligne | BT-152 | `vat_rate` (absent si sans taux) |

### Ventilation TVA (BG-23, une par catégorie/taux)
| Donnée | BT | Note |
|---|---|---|
| Base par catégorie | BT-116 | base HT de la catégorie |
| Montant TVA par catégorie | BT-117 | issu de ton `vat_breakdown` |
| Code catégorie | BT-118 | S / Z / E |
| Taux | BT-119 | |
| Motif d'exonération (texte) | BT-120 | requis en E |
| Motif d'exonération (code) | BT-121 | code VATEX, à valider (voir franchise) |

### Totaux (BG-22)
| Donnée | BT | Note |
|---|---|---|
| Somme des montants de ligne | BT-106 | |
| Total HT | BT-109 | `total_ht` |
| Total TVA | BT-110 | `total_tva` |
| Total TTC | BT-112 | `total_ttc` |
| Net à payer | BT-115 | |

## Catégories de TVA : le prolongement de la distinction taux zéro / sans taux

| Ton cas 4a | Catégorie CII (BT-118) | Taux | Motif d'exonération |
|---|---|---|---|
| Taux 20 / 10 / 5.5 (réel) | **S** (Standard rated) | le taux | non |
| `vat_rate = Decimal("0")` (réel) | **Z** (Zero rated) | 0 | non |
| `vat_rate = None` (franchise 293 B) | **E** (Exempt) | 0 | oui : « TVA non applicable, art. 293 B du CGI » |

La franchise en base est un régime d'assujetti non redevable (dans le champ de la TVA mais exonéré), ce qui oriente vers la catégorie E, distincte de O (hors champ, non-assujetti). À confirmer contre le Schematron FR-CTC, voir ci-dessous.

## Pièges Schematron qui font échouer au premier essai

- **Date** BT-2 au format `yyyymmdd`, pas ISO 8601.
- **Séparateur décimal** point, jamais virgule. Decimal partout, déjà en place depuis 4a.
- **schemeID** sur les identifiants légaux : SIREN = `0002`, SIRET = `0009`. Souvent absent par défaut.
- **BR-CO-14** total TVA facture = somme des montants TVA par catégorie. **BR-CO-15** total TTC = total HT + total TVA. Ton arrondi par taux garantit déjà cette cohérence, c'est un atout, ne le casse pas en recalculant autrement côté XML.
- **Cohérence profil** entre BT-24 et le XMP `fx:ConformanceLevel`.
- **XSD n'est pas Schematron** : un XML peut passer la forme (XSD) et échouer le fond (Schematron). La majorité des rejets sont Schematron. Toujours passer les deux.

## Point à valider empiriquement : la franchise en base

La catégorie E, en EN 16931 de base, tend à exiger un identifiant TVA vendeur (BT-31), que l'auto-entrepreneur en franchise n'a pas. Les règles FR-CTC adaptent ce cas. Méthode : générer un XML de facture en franchise, le passer à `xml_check_schematron(check_option='fr-ctc')`, lire les failed-assert, et ajuster. Points à trancher par ce test :
- Catégorie exacte retenue par le Schematron FR-CTC pour la franchise (E attendu).
- Le motif texte (BT-120) suffit-il, ou un code VATEX (BT-121) est-il requis, et lequel.
- Comportement en l'absence de BT-31 côté vendeur.

C'est un point qui se tranche contre le Schematron réel, pas contre la documentation.

## Workflow de génération (hors transaction d'émission)

1. Lire les colonnes de snapshot de la facture émise (données figées, autoportantes).
2. Construire le dict EN 16931 et générer le XML CII (`generate_cii_xml`).
3. Valider : `xml_check_xsd` puis `xml_check_schematron(check_option='fr-ctc')`. Échec = pas de Factur-X produit, la facture n'est pas transmissible.
4. Générer le PDF lisible, en PDF/A (moteur à choisir en 4b-2).
5. Embarquer le XML validé dans le PDF via `generate_from_file`.
6. Contrôler le PDF/A-3 avec veraPDF.
7. Stocker le Factur-X produit, rattaché à la facture, immuable comme le reste.

La génération se fait hors de la transaction d'émission (qui reste courte, verrou compteur compris). Elle peut être asynchrone via le worker Redis prévu.

---

## Résultats empiriques 4b-1 (factur-x 5.1, saxonche 13, Schematron FR-CTC embarqué)

Constatés par le cycle générer → lire les failed-assert → ajuster, sur une
facture réelle multi-taux et une facture en franchise.

### Franchise en base : les trois questions tranchées
- **Catégorie retenue : E confirmée** (taux 0, BR-E-05), le Schematron
  FR-CTC ne s'y oppose pas.
- **BT-120 texte seul suffit** : « TVA non applicable, art. 293 B du CGI ».
  **Aucun code VATEX (BT-121) exigé** par le FR-CTC actuel.
- **Absence de BT-31** : c'est `BR-E-02` (schematron de **base**, niveau
  ligne, pas BR-E-10) qui exige BT-31, BT-32 ou BT-63. Solution retenue :
  **BT-32 = SIREN du vendeur** (émis en SpecifiedTaxRegistration schemeID
  `FC` par la lib), uniquement quand BT-31 est absent.

### Champs conditionnels FR-CTC constatés (hors franchise)
- `BR-FR-05/BT-22` × 3 : trois notes BG-1 obligatoires sous codes BT-21
  `AAB` (escompte ou son absence), `PMD` (pénalités de retard), `PMT`
  (indemnité forfaitaire de recouvrement 40 €). Textes par défaut dans
  `app/cii.py` (TODO : paramétrables par profil vendeur).
- `BR-FR-08/BT-23` : liste fermée B1, S1, M1, B2, S2, M2, B4, S4, M4, S5,
  S6, B7, S7 (« A1 » rejeté). Retenu : **B1** (facture B2B domestique).
- `BR-FR-12/BT-49` et `BR-FR-13/BT-34` : adresse électronique acheteur
  **et** vendeur obligatoires. Retenu : SIREN, schéma EAS `0002`, des deux
  côtés.
- **BG-16 (instructions de paiement) : non exigé** par le FR-CTC actuel,
  surveillé, aucun failed-assert.

### Contrainte XSD hors Schematron
- La lib émet toujours `ApplicableHeaderTradeDelivery` ; vide, le XSD CII
  le rejette (« element is not nillable »). Retenu : **BT-72
  systématique**, `supply_date`, sinon réputé égal à la date d'émission.

## Résultats empiriques 4b-2 (WeasyPrint 66 en conteneur, veraPDF cli, pypdf)

- **Rendu containerisé** : WeasyPrint inutilisable sur l'hôte Windows (DLL
  Pango/GTK absentes), le rendu tourne dans `docker/pdf` (Linux, Pango
  trivial, fonts-dejavu), cohérent avec veraPDF déjà containerisé. Commandes
  pilotées par config (`APP_PDF_RENDER_COMMAND`, `APP_VERAPDF_COMMAND`),
  répertoire d'échange `.exchange/` bind-mounté, gitignoré.
- **AFRelationship = `Alternative`** pour EN 16931, confirmé contre la spec
  FNFE-MPE/FeRD et non contre la lib : `Data` n'est admis que pour MINIMUM
  et BASIC WL ; le défaut `data` de factur-x est **non conforme** pour notre
  profil et est surchargé explicitement.
- **`generate_from_binary` réécrit le XMP avec `pdfaid:part=3` en dur** :
  un corps A-2b saboté devient... un A-3b légitimement valide (A-3 = A-2 +
  fichiers embarqués). Le test de rejet du conteneur non conforme utilise
  donc un PDF ordinaire, sans OutputIntent : le XMP menteur (part=3) ne
  suffit pas à tromper veraPDF, qui échoue bien en flavour 3b.
- **Étage « structure Factur-X »** : assertions pypdf propres au projet
  (nom exact `factur-x.xml`, AFRelationship, MIME `text/xml`,
  `fx:ConformanceLevel` = « EN 16931 » cohérent avec le BT-24 du XML
  embarqué), angle mort de veraPDF, qui valide le conteneur PDF/A mais
  pas la spec Factur-X. Même famille d'angle mort que saxonche en 4b-1.
- **Déterminisme** : CreationDate épinglé sur la date d'émission
  (`dcterms.created`, lu par WeasyPrint), `/ID` épinglé sur le sha256 de
  l'artefact cii_xml, métadonnées `generate_from_binary` fixes. Garantie
  contractuelle **sémantique** : le XML réextrait du PDF est octet pour
  octet l'artefact cii_xml, testé, et re-vérifié dans le pipeline avant
  stockage.

## Stockage et régénération des artefacts

Table `invoice_artifacts` (migration 0005), même régime d'inaltérabilité
que `audit_log` : policies RLS `FOR SELECT`/`FOR INSERT` seulement, REVOKE
UPDATE/DELETE pour `app_user`, unicité `(invoice_id, kind)`, sha256 stocké.
La présence de l'artefact validé vaut « transmissible » (aucun flag sur
`invoices`, immuable). La régénération d'un artefact après correction d'un
bug de mapping passe par `migrator` (suppression contrôlée puis re-POST),
jamais par `app_user`, même logique que la purge de rétention d'audit_log.
