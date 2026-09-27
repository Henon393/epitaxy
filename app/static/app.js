/* epitaxy - interface web : comportement.
   Extrait de index.html, aucun changement de code.
   Charge en fin de <body> : le DOM au-dessus est deja analyse. */

"use strict";

/* ------------------------------------------------------------------ *
 * Constantes
 * ------------------------------------------------------------------ */

// Aucun identifiant n'est codé ici : ce fichier est public, et un
// tenant_id ou une adresse en clair y seraient une fuite. Les champs de
// connexion restent vides, l'utilisateur saisit ses identifiants.
//
// L'API exige tenant_id en plus de l'e-mail : elle n'expose volontairement
// aucune route qui retrouverait le tenant depuis l'adresse, ce qui serait
// une énumération inter-tenants.

// L'interface est servie par l'application elle-meme : meme origine,
// donc appels directs vers les routes, sans prefixe ni relais.
const API = "";
const CLE_JETONS = "epitaxy.jetons";

// Les 8 statuts réels : 2 portés par la facture, 6 par la transmission PA.
const LIBELLES = {
  brouillon: "Brouillon",
  emise:     "Émise",
  deposee:   "Déposée",
  recue:     "Reçue",
  approuvee: "Approuvée",
  encaissee: "Encaissée",
  refusee:   "Refusée",
  rejetee:   "Rejetée",
};

const euros = new Intl.NumberFormat("fr-FR", {
  style: "currency", currency: "EUR", minimumFractionDigits: 2,
});
const jour = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit", month: "2-digit", year: "numeric",
});

/* ------------------------------------------------------------------ *
 * Jetons : stockage et rafraîchissement sérialisé
 * ------------------------------------------------------------------ */

let jetons = null;

function chargerJetons() {
  try {
    const brut = sessionStorage.getItem(CLE_JETONS);
    jetons = brut ? JSON.parse(brut) : null;
  } catch { jetons = null; }
  return jetons;
}
function enregistrerJetons(valeur) {
  jetons = valeur;
  try {
    if (valeur) sessionStorage.setItem(CLE_JETONS, JSON.stringify(valeur));
    else sessionStorage.removeItem(CLE_JETONS);
  } catch { /* navigation privée : on garde les jetons en mémoire */ }
}

// UN SEUL rafraîchissement à la fois, partagé par tous les appels.
//
// Indispensable : la page tire une dizaine de requêtes en parallèle (une
// par transmission). Si le jeton d'accès expire pendant ce lot, on
// récolte autant de 401 simultanés. Sans cette sérialisation, chaque 401
// déclencherait son propre POST /auth/refresh ; le premier consomme le
// jti, les suivants présentent un jti déjà consommé, et la détection de
// réutilisation de l'API révoque TOUTE la famille de session. L'utilisateur
// serait éjecté. Ici, les dix appels attendent la même promesse.
let refreshEnCours = null;

function rafraichir() {
  if (!refreshEnCours) {
    refreshEnCours = (async () => {
      if (!jetons || !jetons.refresh_token) return false;
      const r = await fetch(`${API}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: jetons.refresh_token }),
      });
      if (!r.ok) { enregistrerJetons(null); return false; }
      const paire = await r.json();
      enregistrerJetons({
        access_token: paire.access_token,
        refresh_token: paire.refresh_token,
      });
      return true;
    })().finally(() => { refreshEnCours = null; });
  }
  return refreshEnCours;
}

class SessionExpiree extends Error {}

/**
 * Appel API authentifié. Sur 401 : un seul rafraîchissement (partagé),
 * puis une seule nouvelle tentative. Échec => SessionExpiree.
 */
async function apiFetch(chemin, options = {}, reessayer = true) {
  const entetes = Object.assign({}, options.headers);
  if (jetons && jetons.access_token) {
    entetes.Authorization = `Bearer ${jetons.access_token}`;
  }
  const reponse = await fetch(API + chemin, Object.assign({}, options, { headers: entetes }));

  if (reponse.status === 401 && reessayer) {
    const ok = await rafraichir();
    if (!ok) throw new SessionExpiree("Session expirée.");
    return apiFetch(chemin, options, false);
  }
  return reponse;
}

async function apiJson(chemin) {
  const r = await apiFetch(chemin);
  if (r.status === 401) throw new SessionExpiree("Session expirée.");
  if (!r.ok) throw new Error(`GET ${chemin} a répondu ${r.status}`);
  return r.json();
}

/* ------------------------------------------------------------------ *
 * Chargement et composition des données
 * ------------------------------------------------------------------ */

/**
 * Le statut affiché vient de deux sources :
 *  - invoice.status ne vaut que "brouillon" ou "emise" ;
 *  - les six statuts PA (deposee, recue, approuvee, encaissee, refusee,
 *    rejetee) sont le dernier événement de la transmission.
 * D'où un appel par facture émise. Au volume actuel c'est indolore ;
 * pour une vraie volumétrie il faudra exposer le statut courant
 * directement dans GET /invoices.
 */
async function chargerFactures() {
  const [factures, clients] = await Promise.all([
    apiJson("/invoices"),
    apiJson("/customers"),
  ]);

  clientsCharges = clients;
  const nomParClient = new Map(clients.map((c) => [c.id, c.name]));

  const lignes = await Promise.all(factures.map(async (f) => {
    let statut = f.status;
    let encaisse = 0;
    if (f.status === "emise") {
      const r = await apiFetch(`/invoices/${f.id}/transmission`);
      if (r.status === 401) throw new SessionExpiree("Session expirée.");
      if (r.ok) {
        const t = await r.json();
        const evenements = t.events || [];
        const dernier = evenements[evenements.length - 1];
        if (dernier) statut = dernier.status;

        // Montant reellement encaisse, somme des evenements « encaissee ».
        // Ce statut est repetable cote plateforme, pour les paiements
        // partiels successifs : sommer les evenements est donc la seule
        // lecture juste. Compter le TTC des factures encaissees
        // surevaluerait un paiement partiel.
        for (const e of evenements) {
          if (e.status === "encaissee" && e.paid_amount !== null && e.paid_amount !== undefined) {
            encaisse += Number(e.paid_amount);
          }
        }
        // Encaissee sans montant porte : la plateforme signale le paiement
        // sans le chiffrer. On retombe sur le TTC, faute de mieux.
        if (encaisse === 0 && statut === "encaissee") encaisse = Number(f.total_ttc);
      }
      // 404 = aucune transmission : la facture reste simplement « émise ».
    }
    return {
      id: f.id,
      numero: f.number,
      // Le snapshot acheteur n'est figé qu'à l'émission : pour un
      // brouillon, on retombe sur la fiche client vivante.
      client: f.buyer_name || nomParClient.get(f.customer_id) || "—",
      emission: f.issue_date,
      ttc: Number(f.total_ttc),
      statut,
      encaisse,
    };
  }));

  // Brouillons en tête (travail en cours), puis les émises de la plus
  // récente à la plus ancienne.
  lignes.sort((a, b) => {
    if ((a.numero === null) !== (b.numero === null)) return a.numero === null ? -1 : 1;
    return (b.numero || 0) - (a.numero || 0);
  });
  return lignes;
}

/* ------------------------------------------------------------------ *
 * Rendu
 * ------------------------------------------------------------------ */

const $ = (id) => document.getElementById(id);

// Factures deja chargees. Tout le filtrage des onglets se fait ici, en
// memoire : changer d'onglet ne declenche AUCUN appel reseau.
let lignesChargees = [];
let ongletActif = "toutes";

// Une meme facture appartient legitimement a plusieurs onglets : une
// encaissee est aussi une emise. « Emises » = tout ce qui porte un numero
// legal, donc tout sauf les brouillons.
const SOUS_TITRES = {
  toutes:     "toutes factures confondues",
  brouillons: "brouillons uniquement",
  emises:     "factures émises",
  encaissees: "factures encaissées",
  rejetees:   "refusées et rejetées",
};

const FILTRES = {
  toutes:     () => true,
  brouillons: (l) => l.statut === "brouillon",
  emises:     (l) => l.statut !== "brouillon",
  encaissees: (l) => l.statut === "encaissee",
  rejetees:   (l) => l.statut === "rejetee" || l.statut === "refusee",
};

/** Facture emise dont le paiement reste attendu.
 *
 * Predicat partage par l'encadre de la liste et par la carte du tableau
 * de bord. Deux definitions separees finiraient par diverger et les deux
 * ecrans afficheraient des montants contradictoires. */
function estEnAttente(l) {
  return l.statut !== "brouillon" &&
    l.statut !== "encaissee" &&
    l.statut !== "refusee" &&
    l.statut !== "rejetee";
}

/** Facture portant un numero legal : un brouillon n'est pas facture. */
function estFacturee(l) {
  return l.statut !== "brouillon";
}

function rendre(lignes) {
  lignesChargees = lignes;
  majCompteurs();
  rendreTableau();
  rendreTableauDeBord();
}

/** Etiquette de statut, identique sur la liste et sur le tableau de bord. */
function creerBadge(statut) {
  const badge = document.createElement("span");
  badge.className = `badge s-${statut}`;
  badge.textContent = LIBELLES[statut] || statut;
  return badge;
}

/* Decompte, Total TTC et En attente portent sur le SOUS-ENSEMBLE affiche
   par l'onglet actif, pas sur l'ensemble des factures. Sur un onglet ou
   « En attente » n'a pas de sens (Encaissees, ou tout est deja encaisse),
   la somme vaut naturellement 0. */
function majEnTete(lignes) {
  const n = lignes.length;
  $("compte").textContent = n === 1 ? "1 facture" : `${n} factures`;

  const total = lignes.reduce((s, l) => s + l.ttc, 0);
  $("val-total").textContent = euros.format(total);
  $("pre-total").textContent = SOUS_TITRES[ongletActif];

  const attendues = lignes.filter(estEnAttente);
  $("val-attente").textContent = euros.format(attendues.reduce((s, l) => s + l.ttc, 0));
  const pl = attendues.length > 1 ? "s" : "";
  $("pre-attente").textContent =
    `${attendues.length} facture${pl} émise${pl}, non encaissée${pl}`;
}

function majCompteurs() {
  for (const onglet of document.querySelectorAll(".onglet")) {
    const pastille = onglet.querySelector(".onglet-compte");
    if (pastille) {
      pastille.textContent = lignesChargees.filter(FILTRES[onglet.dataset.onglet]).length;
    }
  }
}

function rendreTableau() {
  const lignes = lignesChargees.filter(FILTRES[ongletActif]);
  majEnTete(lignes);

  const corps = $("corps");
  corps.replaceChildren();

  // Onglet vide : on masque le tableau entier, en-tete compris, plutot
  // que de laisser des colonnes surmonter le vide.
  const vide = lignes.length === 0;
  $("defilement").hidden = vide;
  $("vide").hidden = !vide;
  if (vide) return;

  for (const l of lignes) {
    const tr = document.createElement("tr");

    const num = document.createElement("td");
    num.className = "num";
    if (l.numero === null) { num.textContent = "—"; num.classList.add("vide"); }
    else num.textContent = String(l.numero).padStart(4, "0");

    const client = document.createElement("td");
    client.className = "client";
    client.textContent = l.client;

    const date = document.createElement("td");
    if (l.emission) {
      date.textContent = jour.format(new Date(`${l.emission}T00:00:00`));
    } else { date.textContent = "—"; date.className = "vide"; }

    const ttc = document.createElement("td");
    ttc.className = "montant";
    ttc.textContent = euros.format(l.ttc);

    const statut = document.createElement("td");
    statut.append(creerBadge(l.statut));

    // Colonne Factur-X. Les brouillons n'ont pas d'artefact : pas de
    // bouton du tout. Pour les emises, le bouton est affiche d'emblee ;
    // si l'API repond 404 (Factur-X jamais genere), il se desactive de
    // lui-meme au premier clic. Cela evite de telecharger tous les PDF
    // au chargement juste pour savoir lesquels existent.
    const pdf = document.createElement("td");
    pdf.className = "pdf";
    if (l.statut !== "brouillon") {
      const bouton = document.createElement("button");
      bouton.type = "button";
      bouton.className = "lien-pdf";
      bouton.textContent = "PDF";
      bouton.title = `Telecharger le Factur-X de la facture ${l.numero}`;
      bouton.addEventListener("click", () => telechargerPdf(l, bouton));
      pdf.append(bouton);
    }

    tr.append(num, client, date, ttc, statut, pdf);
    corps.append(tr);
  }
}

/* ------------------------------------------------------------------ *
 * Tableau de bord
 * ------------------------------------------------------------------ */

// Ordre du cycle de vie, et non ordre alphabetique : la legende et
// l'anneau suivent ainsi la progression reelle d'une facture.
const ORDRE_STATUTS = [
  "brouillon", "emise", "deposee", "recue",
  "approuvee", "encaissee", "refusee", "rejetee",
];

// Fenetres du selecteur de periode, en jours comptes depuis aujourd'hui.
const PERIODES = {
  "30":        { jours: 30,  libelle: "sur les 30 derniers jours" },
  "trimestre": { jours: 91,  libelle: "sur le dernier trimestre" },
  "annee":     { jours: 365, libelle: "sur les 12 derniers mois" },
};

let periodeActive = "annee";

/** Valeur d'une variable CSS declaree sur :root. */
function variableCss(nom) {
  return getComputedStyle(document.documentElement).getPropertyValue(nom).trim();
}

/**
 * Couleur d'un statut pour le diagramme en anneau.
 *
 * L'anneau a besoin des teintes en JavaScript, mais les recopier ici
 * creerait une seconde source de verite qui divergerait de la feuille de
 * style a la premiere retouche. On lit donc la variable declaree dans
 * :root, qui reste l'unique endroit ou la couleur est ecrite.
 *
 * C'est la teinte « anneau » et non celle du texte : sur un aplat etroit,
 * les huit teintes de texte, toutes sombres, se confondraient.
 */
function couleurStatut(statut) {
  return variableCss(`--st-${statut}-anneau`) || variableCss("--texte-secondaire");
}

/**
 * Factures retenues pour la periode choisie.
 *
 * Le filtre porte sur la date d'emission. Un brouillon n'en a pas : il
 * n'est pas un evenement date mais un travail en cours, donc il reste
 * visible quelle que soit la periode. L'exclure ferait disparaitre du
 * tableau de bord un travail en cours simplement parce qu'il n'est pas
 * encore emis.
 */
function lignesDeLaPeriode() {
  const jours = PERIODES[periodeActive].jours;
  const limite = new Date();
  limite.setHours(0, 0, 0, 0);
  limite.setDate(limite.getDate() - jours);

  return lignesChargees.filter((l) => {
    if (!l.emission) return true;
    return new Date(`${l.emission}T00:00:00`) >= limite;
  });
}

function rendreTableauDeBord() {
  majDateDuJour();
  const lignes = lignesDeLaPeriode();
  majChiffresCles(lignes);
  rendreAnneau(lignes);
  rendreDernieres(lignes);
}

/** « Nous sommes le 27 septembre 2026 », depuis l'horloge du poste. */
function majDateDuJour() {
  const complet = new Intl.DateTimeFormat("fr-FR", {
    day: "numeric", month: "long", year: "numeric",
  });
  $("tb-date").textContent = `Nous sommes le ${complet.format(new Date())}.`;
}

function majChiffresCles(lignes) {
  const suffixe = PERIODES[periodeActive].libelle;
  const pluriel = (n) => (n > 1 ? "s" : "");

  // Total facture : seules les factures portant un numero legal. Un
  // brouillon n'est pas facture, le compter gonflerait un chiffre
  // d'affaires qui n'existe pas encore.
  const facturees = lignes.filter(estFacturee);
  $("tb-facture").textContent = euros.format(facturees.reduce((s, l) => s + l.ttc, 0));
  $("tb-facture-pre").textContent =
    `${facturees.length} facture${pluriel(facturees.length)} émise${pluriel(facturees.length)} ${suffixe}`;

  // Meme predicat que l'encadre de la liste, par construction.
  const attente = lignes.filter(estEnAttente);
  $("tb-attente").textContent = euros.format(attente.reduce((s, l) => s + l.ttc, 0));
  $("tb-attente-pre").textContent = attente.length === 0
    ? "aucun paiement en attente"
    : `${attente.length} facture${pluriel(attente.length)} non encaissée${pluriel(attente.length)}`;

  const brouillons = lignes.filter((l) => l.statut === "brouillon").length;
  $("tb-nombre").textContent = String(lignes.length);
  $("tb-nombre-pre").textContent = brouillons === 0
    ? `aucun brouillon en cours`
    : `dont ${brouillons} brouillon${pluriel(brouillons)} en cours`;

  const encaisse = lignes.reduce((s, l) => s + l.encaisse, 0);
  const reglees = lignes.filter((l) => l.encaisse > 0).length;
  $("tb-encaisse").textContent = euros.format(encaisse);
  $("tb-encaisse-pre").textContent = reglees === 0
    ? "aucun encaissement enregistré"
    : `sur ${reglees} facture${pluriel(reglees)}, paiements partiels compris`;
}

/**
 * Pourcentages entiers dont la somme fait exactement 100.
 *
 * Arrondir chaque part separement donnerait un total de 99 ou 101 selon
 * les cas : avec 3 statuts a un tiers chacun, trois arrondis a 33
 * totalisent 99. La methode du plus fort reste corrige ce defaut. On
 * attribue d'abord la partie entiere de chaque part, puis les unites
 * restantes aux statuts dont la decimale perdue est la plus grande.
 *
 * Retourne une Map, cle par statut.
 */
function repartirPourcentages(nombreParStatut, total) {
  const parts = new Map();
  if (total === 0) return parts;

  const restes = [];
  let attribue = 0;
  for (const [statut, nombre] of nombreParStatut) {
    const exact = (nombre / total) * 100;
    const entier = Math.floor(exact);
    parts.set(statut, entier);
    attribue += entier;
    restes.push({ statut, reste: exact - entier });
  }

  // Les unites non attribuees vont aux plus grandes decimales perdues.
  restes.sort((a, b) => b.reste - a.reste);
  for (let i = 0; i < 100 - attribue; i += 1) {
    const cible = restes[i % restes.length].statut;
    parts.set(cible, parts.get(cible) + 1);
  }
  return parts;
}

/**
 * Anneau de repartition, en SVG pur.
 *
 * Chaque segment est un cercle dont le trait ne couvre qu'une fraction du
 * perimetre, decale par stroke-dashoffset. Aucune bibliotheque n'est
 * chargee : le projet n'a pas d'etape de construction cote navigateur, et
 * une dependance externe serait un tiers a auditer pour un dessin.
 */
function rendreAnneau(lignes) {
  const svg = $("tb-anneau");
  const legende = $("tb-legende");
  svg.replaceChildren();
  legende.replaceChildren();

  const parStatut = new Map();
  for (const l of lignes) {
    const actuel = parStatut.get(l.statut) || { nombre: 0, ttc: 0 };
    actuel.nombre += 1;
    actuel.ttc += l.ttc;
    parStatut.set(l.statut, actuel);
  }
  const presents = ORDRE_STATUTS.filter((s) => parStatut.has(s));
  const total = lignes.length;

  const NS = "http://www.w3.org/2000/svg";
  const RAYON = 38;
  const PERIMETRE = 2 * Math.PI * RAYON;

  const cercle = (couleur, longueur, decalage) => {
    const c = document.createElementNS(NS, "circle");
    c.setAttribute("cx", "50");
    c.setAttribute("cy", "50");
    c.setAttribute("r", String(RAYON));
    c.setAttribute("fill", "none");
    c.setAttribute("stroke", couleur);
    c.setAttribute("stroke-width", "13");
    c.setAttribute("stroke-dasharray", `${longueur} ${PERIMETRE - longueur}`);
    c.setAttribute("stroke-dashoffset", String(decalage));
    // Depart a midi plutot qu'a 3 heures, sens horaire.
    c.setAttribute("transform", "rotate(-90 50 50)");
    return c;
  };

  if (total === 0) {
    svg.append(cercle(variableCss("--anneau-vide"), PERIMETRE, 0));
  } else {
    let parcouru = 0;
    for (const statut of presents) {
      const part = parStatut.get(statut).nombre / total;
      const longueur = part * PERIMETRE;
      // Un segment plein ferait disparaitre la separation : on garde un
      // liseré d'un demi-point quand plusieurs statuts coexistent.
      const visible = presents.length > 1 ? Math.max(longueur - 0.6, 0.6) : longueur;
      svg.append(cercle(couleurStatut(statut), visible, -parcouru));
      parcouru += longueur;
    }
  }

  // Total au centre de l'anneau, pose en HTML par-dessus le dessin.
  $("tb-anneau-total").textContent = String(total);
  $("tb-anneau-libelle").textContent = total === 1 ? "FACTURE" : "FACTURES";

  if (total === 0) {
    const li = document.createElement("li");
    li.className = "legende-nom";
    li.textContent = "Aucune facture sur la période";
    legende.append(li);
    return;
  }

  const parts = repartirPourcentages(
    new Map(presents.map((s) => [s, parStatut.get(s).nombre])),
    total,
  );

  for (const statut of presents) {
    const { nombre, ttc } = parStatut.get(statut);
    const li = document.createElement("li");

    const puce = document.createElement("span");
    puce.className = "legende-puce";
    puce.style.background = couleurStatut(statut);

    const nom = document.createElement("span");
    nom.className = "legende-nom";
    nom.textContent = LIBELLES[statut] || statut;

    const compte = document.createElement("span");
    compte.className = "legende-nombre";
    compte.textContent = String(nombre);

    const part = document.createElement("span");
    part.className = "legende-part";
    part.textContent = `${parts.get(statut)} %`;

    const montant = document.createElement("span");
    montant.className = "legende-montant";
    montant.textContent = euros.format(ttc);

    li.append(puce, nom, compte, part, montant);
    legende.append(li);
  }
}

/** Les 5 factures les plus recentes. lignesChargees est deja triee. */
function rendreDernieres(lignes) {
  const corps = $("tb-dernieres");
  corps.replaceChildren();

  const extrait = lignes.slice(0, 5);
  const vide = extrait.length === 0;
  corps.closest(".defilement").hidden = vide;
  $("tb-dernieres-vide").hidden = !vide;
  if (vide) return;

  for (const l of extrait) {
    const tr = document.createElement("tr");

    const num = document.createElement("td");
    num.className = "num";
    if (l.numero === null) { num.textContent = "—"; num.classList.add("vide"); }
    else num.textContent = String(l.numero).padStart(4, "0");

    const client = document.createElement("td");
    client.className = "client";
    client.textContent = l.client;

    const ttc = document.createElement("td");
    ttc.className = "montant";
    ttc.textContent = euros.format(l.ttc);

    const statut = document.createElement("td");
    statut.append(creerBadge(l.statut));

    tr.append(num, client, ttc, statut);
    corps.append(tr);
  }
}

/**
 * Telecharge le Factur-X d'une facture.
 *
 * Passe par apiFetch, donc par le relais et avec le jeton : on herite
 * gratuitement du rafraichissement serialise en cas de 401. Un <a href>
 * simple ne conviendrait pas, il ne porterait aucun en-tete.
 */
async function telechargerPdf(l, bouton) {
  const libelle = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = "…";
  let url = null;
  try {
    const reponse = await apiFetch(`/invoices/${l.id}/facturx`);

    if (reponse.status === 404) {
      // Facture emise dont le Factur-X n'a jamais ete genere : on le dit
      // et on neutralise le bouton, il ne reussira pas davantage ensuite.
      bouton.textContent = "—";
      bouton.title = "Aucun Factur-X n'a ete genere pour cette facture.";
      signaler(`Aucun Factur-X pour la facture n° ${l.numero}.`);
      return;
    }
    if (reponse.status === 401) throw new SessionExpiree("Session expirée.");
    if (!reponse.ok) throw new Error(`HTTP ${reponse.status}`);

    const blob = await reponse.blob();
    url = URL.createObjectURL(blob);
    const lien = document.createElement("a");
    lien.href = url;
    lien.download = `facture-${String(l.numero).padStart(4, "0")}.pdf`;
    document.body.append(lien);
    lien.click();
    lien.remove();

    bouton.textContent = libelle;
    bouton.disabled = false;
  } catch (err) {
    bouton.textContent = libelle;
    bouton.disabled = false;
    if (err instanceof SessionExpiree) {
      enregistrerJetons(null);
      afficherConnexion("Session expirée, reconnectez-vous.");
      return;
    }
    console.error(err);
    signaler(`Telechargement impossible : ${err.message}`);
  } finally {
    // Laisse au navigateur le temps d'enregistrer le fichier avant de
    // liberer l'URL objet.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

/* ------------------------------------------------------------------ *
 * Formulaire : arithmétique décimale exacte
 *
 * Le serveur (app/invoicing.py) calcule en Decimal avec ROUND_HALF_UP :
 * arrondi du montant PAR LIGNE, regroupement des bases PAR TAUX, puis
 * arrondi de la TVA PAR TAUX (jamais par ligne). Reproduire cela en
 * flottants donnerait des écarts d'un centime : la page afficherait un
 * total différent de celui que l'API renverra. On travaille donc en
 * centimes entiers avec BigInt.
 * ------------------------------------------------------------------ */

/** "12,5" -> {v: 125n, d: 1} ; null si la saisie n'est pas un décimal positif. */
function decoder(txt) {
  const t = String(txt ?? "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(t)) return null;
  const [entier, decimales = ""] = t.split(".");
  return { v: BigInt(entier + decimales), d: decimales.length };
}

/** Division entière arrondie au plus proche, moitié vers le haut. */
function arrondiHalfUp(numerateur, denominateur) {
  const q = numerateur / denominateur;
  const r = numerateur % denominateur;
  return 2n * r >= denominateur ? q + 1n : q;
}

/** Montant HT d'une ligne, en centimes. null si saisie invalide. */
function montantLigneCentimes(quantite, prix) {
  const q = decoder(quantite);
  const p = decoder(prix);
  if (!q || !p) return null;
  return arrondiHalfUp(q.v * p.v * 100n, 10n ** BigInt(q.d + p.d));
}

/** TVA d'une base (en centimes) à un taux donné ("20.00"), en centimes. */
function tvaCentimes(baseCentimes, taux) {
  const t = decoder(taux);
  return arrondiHalfUp(baseCentimes * t.v, 100n * 10n ** BigInt(t.d));
}

function formaterCentimes(centimes) {
  return euros.format(Number(centimes) / 100);
}

/* ------------------------------------------------------------------ *
 * Formulaire : état et rendu
 * ------------------------------------------------------------------ */

const TAUX_TVA = ["20.00", "10.00", "5.50", "2.10", "0.00"];  // taux francais en vigueur
let clientsCharges = [];

function ligneVide() {
  const tr = document.createElement("tr");
  const cellule = (enfant) => {
    const td = document.createElement("td");
    td.append(enfant);
    return td;
  };

  const designation = document.createElement("input");
  designation.type = "text";
  designation.placeholder = "Prestation, article…";
  designation.maxLength = 500;
  designation.dataset.champ = "designation";

  const quantite = document.createElement("input");
  quantite.type = "text";
  quantite.inputMode = "decimal";
  quantite.className = "nombre";
  quantite.value = "1";
  quantite.dataset.champ = "quantite";

  const prix = document.createElement("input");
  prix.type = "text";
  prix.inputMode = "decimal";
  prix.className = "nombre";
  prix.placeholder = "0.00";
  prix.dataset.champ = "prix";

  const tva = document.createElement("select");
  tva.dataset.champ = "tva";
  for (const taux of TAUX_TVA) {
    const opt = document.createElement("option");
    opt.value = taux;
    opt.textContent = taux.replace(".", ",").replace(",00", "") + " %";
    tva.append(opt);
  }

  const montant = document.createElement("td");
  montant.className = "montant";
  montant.dataset.champ = "montant";
  montant.textContent = euros.format(0);

  const supprimer = document.createElement("button");
  supprimer.type = "button";
  supprimer.className = "supprimer";
  supprimer.textContent = "×";
  supprimer.title = "Supprimer cette ligne";
  supprimer.addEventListener("click", () => {
    tr.remove();
    // Toujours au moins une ligne à l'écran, sinon le tableau disparaît.
    if (!$("lignes-corps").children.length) $("lignes-corps").append(ligneVide());
    recalculer();
  });

  for (const champ of [designation, quantite, prix, tva]) {
    champ.addEventListener("input", recalculer);
    champ.addEventListener("change", recalculer);
  }

  tr.append(
    cellule(designation), cellule(quantite), cellule(prix),
    cellule(tva), montant, cellule(supprimer),
  );
  return tr;
}

/** Lit les lignes saisies. Chaque entrée porte ses éléments, pour le marquage. */
function lireLignes() {
  return [...$("lignes-corps").children].map((tr) => {
    const champ = (nom) => tr.querySelector('[data-champ="' + nom + '"]');
    return {
      tr,
      elDesignation: champ("designation"),
      elQuantite: champ("quantite"),
      elPrix: champ("prix"),
      designation: champ("designation").value.trim(),
      quantite: champ("quantite").value,
      prix: champ("prix").value,
      taux: champ("tva").value,
      cellMontant: champ("montant"),
    };
  });
}

/** Recalcul intégral, en mémoire, sans aucun appel réseau. */
function recalculer() {
  let totalHt = 0n;
  const bases = new Map();

  for (const l of lireLignes()) {
    const centimes = montantLigneCentimes(l.quantite, l.prix);
    l.cellMontant.textContent = centimes === null ? "—" : formaterCentimes(centimes);
    if (centimes === null) continue;
    totalHt += centimes;
    bases.set(l.taux, (bases.get(l.taux) ?? 0n) + centimes);
  }

  // TVA arrondie PAR TAUX, après regroupement : c'est l'ordre du serveur.
  let totalTva = 0n;
  const parts = [];
  for (const taux of [...bases.keys()].sort((a, b) => Number(a) - Number(b))) {
    const tva = tvaCentimes(bases.get(taux), taux);
    totalTva += tva;
    parts.push(taux.replace(".", ",") + " % : " + formaterCentimes(tva));
  }

  $("f-ht").textContent = formaterCentimes(totalHt);
  $("f-tva").textContent = formaterCentimes(totalTva);
  $("f-ttc").textContent = formaterCentimes(totalHt + totalTva);
  $("f-ventilation").innerHTML = parts.length ? parts.join(" · ") : "&nbsp;";
}

/* ------------------------------------------------------------------ *
 * Formulaire : validation
 * ------------------------------------------------------------------ */

function nettoyerErreurs() {
  $("form-erreurs").hidden = true;
  for (const p of document.querySelectorAll("#ecran-formulaire .err")) p.hidden = true;
  for (const e of document.querySelectorAll("#ecran-formulaire .invalide")) {
    e.classList.remove("invalide");
  }
}

function marquer(element, idMessage, texte) {
  if (element) element.classList.add("invalide");
  if (idMessage) {
    const p = $(idMessage);
    p.textContent = texte;
    p.hidden = false;
  }
}

/**
 * Valide la saisie. Retourne {ok, messages, premier} ; les erreurs sont
 * aussi marquées directement sur les champs concernés.
 */
function validerFormulaire() {
  nettoyerErreurs();
  const messages = [];
  let premier = null;
  const retenir = (el) => { if (!premier) premier = el; };

  const clientId = $("fa-client").value;
  if (!clientId) {
    marquer($("fa-client"), "err-client", "Choisissez un client.");
    messages.push("Le client est obligatoire.");
    retenir($("fa-client"));
  }

  // Une ligne entièrement vierge est ignorée plutôt que signalée : c'est
  // la ligne que le formulaire ajoute d'office.
  const lignes = lireLignes();
  const utiles = lignes.filter(
    (l) => l.designation !== "" || l.prix.trim() !== "",
  );
  if (utiles.length === 0) {
    marquer(null, "err-lignes", "Ajoutez au moins une ligne complète.");
    messages.push("La facture doit comporter au moins une ligne.");
  }

  utiles.forEach((l, index) => {
    const n = index + 1;
    if (l.designation === "") {
      marquer(l.elDesignation, null, "");
      messages.push("Ligne " + n + " : la désignation est obligatoire.");
      retenir(l.elDesignation);
    }
    const q = decoder(l.quantite);
    if (!q || q.v === 0n) {
      marquer(l.elQuantite, null, "");
      messages.push("Ligne " + n + " : la quantité doit être un nombre strictement positif.");
      retenir(l.elQuantite);
    }
    const p = decoder(l.prix);
    if (!p) {
      marquer(l.elPrix, null, "");
      messages.push("Ligne " + n + " : le prix unitaire doit être un nombre positif ou nul.");
      retenir(l.elPrix);
    }
  });

  // Dates. L'échéance ne peut pas précéder la date de facture : le
  // Schematron FR-CTC (BR-FR-CO-07/BT-9) rejetterait le Factur-X à
  // l'émission. Autant le dire ici plutôt que trois étapes plus loin.
  const aujourdhui = dateLocaleIso(new Date());
  const livraison = $("fa-livraison").value;
  const echeance = $("fa-echeance").value;
  if (echeance && echeance < aujourdhui) {
    marquer($("fa-echeance"), "err-echeance",
      "L'échéance ne peut pas précéder la date de facture (règle FR-CTC BR-FR-CO-07).");
    messages.push("L'échéance est antérieure à aujourd'hui.");
    retenir($("fa-echeance"));
  } else if (livraison && echeance && echeance < livraison) {
    marquer($("fa-echeance"), "err-echeance", "L'échéance ne peut pas précéder la livraison.");
    messages.push("L'échéance précède la date de livraison.");
    retenir($("fa-echeance"));
  }

  if (messages.length) {
    const boite = $("form-erreurs");
    boite.replaceChildren();
    const titre = document.createElement("strong");
    titre.textContent = messages.length === 1
      ? "Un problème empêche la création :"
      : messages.length + " problèmes empêchent la création :";
    const liste = document.createElement("ul");
    liste.style.margin = "6px 0 0";
    liste.style.paddingLeft = "20px";
    for (const m of messages) {
      const li = document.createElement("li");
      li.textContent = m;
      liste.append(li);
    }
    boite.append(titre, liste);
    boite.hidden = false;
  }

  return { ok: messages.length === 0, lignes: utiles, clientId, livraison, echeance, premier };
}

/* ------------------------------------------------------------------ *
 * Formulaire : ouverture, fermeture, envoi
 * ------------------------------------------------------------------ */

/** Date du jour en AAAA-MM-JJ, heure LOCALE (toISOString décalerait en UTC). */
function dateLocaleIso(d) {
  const deux = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + deux(d.getMonth() + 1) + "-" + deux(d.getDate());
}

/* Les trois ecrans vivent dans la meme page et se relaient par affichage,
   sans routeur ni rechargement : les factures deja chargees restent en
   memoire d'un ecran a l'autre. */
const ECRANS = {
  tableau:    "ecran-tableau",
  liste:      "ecran-liste",
  formulaire: "ecran-formulaire",
};

// Ecran d'ou le formulaire a ete ouvert, pour y revenir a l'annulation.
let vueAvantFormulaire = "tableau";

function montrerVue(nom) {
  for (const [cle, id] of Object.entries(ECRANS)) {
    $(id).hidden = cle !== nom;
  }
  // Le bouton de creation n'a pas de sens pendant la saisie.
  $("btn-nouvelle").hidden = nom === "formulaire";

  // Le formulaire n'a pas d'entree de navigation : aucun lien n'est courant
  // pendant la saisie, plutot qu'un lien actif menant ailleurs.
  for (const lien of document.querySelectorAll(".nav-lien")) {
    if (lien.dataset.vue === nom) lien.setAttribute("aria-current", "page");
    else lien.removeAttribute("aria-current");
  }
}

function montrerListe() { montrerVue("liste"); }

function ouvrirFormulaire() {
  erreurApp(null);
  nettoyerErreurs();

  remplirClients();

  const aujourdhui = new Date();
  const dans30 = new Date(aujourdhui.getTime() + 30 * 86400000);
  $("fa-livraison").value = dateLocaleIso(aujourdhui);
  $("fa-echeance").value = dateLocaleIso(dans30);
  $("fa-categorie").value = "prestation_de_services";

  $("lignes-corps").replaceChildren(ligneVide());
  recalculer();

  vueAvantFormulaire = $("ecran-tableau").hidden ? "liste" : "tableau";
  montrerVue("formulaire");
  $("fa-client").focus();
}

/** Déplie le détail d'erreur renvoyé par l'API, y compris un 422 Pydantic. */
function detailApi(charge, statut) {
  const d = charge && charge.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const champ = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(" → ") : "";
      return champ ? champ + " : " + e.msg : e.msg;
    }).join(" ; ");
  }
  return "L'API a refusé la création (HTTP " + statut + ").";
}

function erreurFormulaire(texte) {
  const boite = $("form-erreurs");
  boite.replaceChildren(document.createTextNode(texte));
  boite.hidden = false;
  boite.scrollIntoView({ block: "nearest" });
}

async function envoyerFormulaire(evt) {
  evt.preventDefault();
  const verdict = validerFormulaire();
  if (!verdict.ok) {
    if (verdict.premier) verdict.premier.focus();
    $("form-erreurs").scrollIntoView({ block: "nearest" });
    return;
  }

  const bouton = $("btn-creer");
  bouton.disabled = true;
  bouton.textContent = "Création…";

  const charge = {
    customer_id: verdict.clientId,
    operation_category: $("fa-categorie").value,
    lines: verdict.lignes.map((l) => ({
      designation: l.designation,
      quantity: String(l.quantite).trim().replace(",", "."),
      unit_price_ht: String(l.prix).trim().replace(",", "."),
      vat_rate: l.taux,
    })),
  };
  if (verdict.livraison) charge.supply_date = verdict.livraison;
  if (verdict.echeance) charge.due_date = verdict.echeance;

  try {
    const reponse = await apiFetch("/invoices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(charge),
    });

    if (reponse.status === 401) throw new SessionExpiree("Session expirée.");

    if (!reponse.ok) {
      const corps = await reponse.json().catch(() => null);
      erreurFormulaire(detailApi(corps, reponse.status));
      return;
    }

    const creee = await reponse.json();
    // Retour à la liste, rechargée pour que la nouvelle facture y figure.
    rendre(await chargerFactures());
    montrerListe();
    confirmer("Facture créée : " + euros.format(Number(creee.total_ttc)) + " TTC, en brouillon.");
  } catch (err) {
    if (err instanceof SessionExpiree) {
      enregistrerJetons(null);
      afficherConnexion("Session expirée, reconnectez-vous.");
      return;
    }
    console.error(err);
    erreurFormulaire("Création impossible : " + err.message);
  } finally {
    bouton.disabled = false;
    bouton.textContent = "Créer la facture";
  }
}

/* ------------------------------------------------------------------ *
 * Modale « Nouveau client »
 *
 * Le formulaire de facture reste monté dans le DOM sous le voile : rien
 * n'est démonté ni re-rendu, donc la facture en cours de saisie ne peut
 * pas être perdue pendant la création d'un client.
 * ------------------------------------------------------------------ */

/** Remplit le menu déroulant des clients, en sélectionnant éventuellement l'un d'eux. */
function remplirClients(idASelectionner) {
  const select = $("fa-client");
  select.replaceChildren();

  const vide = document.createElement("option");
  vide.value = "";
  vide.textContent = clientsCharges.length
    ? "— Choisir un client —"
    : "Aucun client enregistré";
  select.append(vide);

  for (const c of clientsCharges) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name;
    select.append(opt);
  }
  if (idASelectionner) select.value = idASelectionner;
}

const CHAMPS_CLIENT = [
  ["cl-nom", "err-cl-nom"],
  ["cl-email", "err-cl-email"],
  ["cl-siren", "err-cl-siren"],
  ["cl-adresse1", "err-cl-adresse1"],
  ["cl-cp", "err-cl-cp"],
  ["cl-ville", "err-cl-ville"],
  ["cl-pays", "err-cl-pays"],
];

function nettoyerErreursClient() {
  $("cl-erreurs").hidden = true;
  for (const [champ, err] of CHAMPS_CLIENT) {
    $(champ).classList.remove("invalide");
    $(err).hidden = true;
  }
}

/**
 * Valide la saisie du client.
 *
 * L'API n'exige que `name` et `email` ; SIREN, adresse, code postal,
 * ville et pays y sont facultatifs. On les impose quand même : un client
 * incomplet produit une facture dont le XML CII échouera plus tard à la
 * validation Schematron. Mieux vaut bloquer ici.
 */
function validerClient() {
  nettoyerErreursClient();
  const messages = [];
  let premier = null;

  const faute = (idChamp, idErr, texte) => {
    $(idChamp).classList.add("invalide");
    $(idErr).textContent = texte;
    $(idErr).hidden = false;
    messages.push(texte);
    if (!premier) premier = $(idChamp);
  };

  const lire = (id) => $(id).value.trim();

  const nom = lire("cl-nom");
  if (nom === "") faute("cl-nom", "err-cl-nom", "Le nom est obligatoire.");

  const email = lire("cl-email");
  if (email === "") {
    faute("cl-email", "err-cl-email", "L'adresse e-mail est obligatoire.");
  } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) {
    faute("cl-email", "err-cl-email", "L'adresse e-mail n'est pas valide.");
  }

  const siren = lire("cl-siren");
  if (siren === "") {
    faute("cl-siren", "err-cl-siren", "Le SIREN est obligatoire.");
  } else if (!/^\d{9}$/.test(siren)) {
    faute("cl-siren", "err-cl-siren", "Le SIREN doit comporter exactement 9 chiffres.");
  }

  if (lire("cl-adresse1") === "") {
    faute("cl-adresse1", "err-cl-adresse1", "L'adresse est obligatoire.");
  }
  if (lire("cl-cp") === "") {
    faute("cl-cp", "err-cl-cp", "Le code postal est obligatoire.");
  }
  if (lire("cl-ville") === "") {
    faute("cl-ville", "err-cl-ville", "La ville est obligatoire.");
  }

  const pays = lire("cl-pays").toUpperCase();
  if (pays === "") {
    faute("cl-pays", "err-cl-pays", "Le code pays est obligatoire.");
  } else if (!/^[A-Z]{2}$/.test(pays)) {
    faute("cl-pays", "err-cl-pays", "Le code pays doit être deux lettres majuscules, par exemple FR.");
  }

  if (messages.length) {
    const boite = $("cl-erreurs");
    boite.replaceChildren();
    const titre = document.createElement("strong");
    titre.textContent = messages.length === 1
      ? "Un problème empêche la création :"
      : messages.length + " problèmes empêchent la création :";
    const liste = document.createElement("ul");
    liste.style.margin = "6px 0 0";
    liste.style.paddingLeft = "20px";
    for (const m of messages) {
      const li = document.createElement("li");
      li.textContent = m;
      liste.append(li);
    }
    boite.append(titre, liste);
    boite.hidden = false;
  }

  return { ok: messages.length === 0, premier };
}

function ouvrirModaleClient() {
  nettoyerErreursClient();
  for (const id of ["cl-nom", "cl-email", "cl-siren", "cl-adresse1", "cl-adresse2", "cl-cp", "cl-ville"]) {
    $(id).value = "";
  }
  $("cl-pays").value = "FR";
  $("voile-client").hidden = false;
  $("cl-nom").focus();
}

function fermerModaleClient() {
  $("voile-client").hidden = true;
  // Le focus revient là d'où l'on vient, pas au début de la page.
  $("fa-client").focus();
}

async function envoyerClient(evt) {
  evt.preventDefault();
  const verdict = validerClient();
  if (!verdict.ok) {
    if (verdict.premier) verdict.premier.focus();
    return;
  }

  const bouton = $("cl-creer");
  bouton.disabled = true;
  bouton.textContent = "Création…";

  const lire = (id) => $(id).value.trim();
  const charge = {
    name: lire("cl-nom"),
    email: lire("cl-email"),
    siren: lire("cl-siren"),
    address_line1: lire("cl-adresse1"),
    postal_code: lire("cl-cp"),
    city: lire("cl-ville"),
    country_code: lire("cl-pays").toUpperCase(),
  };
  const complement = lire("cl-adresse2");
  if (complement !== "") charge.address_line2 = complement;

  try {
    const reponse = await apiFetch("/customers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(charge),
    });

    if (reponse.status === 401) throw new SessionExpiree("Session expirée.");

    if (!reponse.ok) {
      // La modale reste ouverte et la saisie intacte : l'utilisateur
      // corrige sans avoir à tout retaper.
      const corps = await reponse.json().catch(() => null);
      const boite = $("cl-erreurs");
      boite.replaceChildren(document.createTextNode(detailApi(corps, reponse.status)));
      boite.hidden = false;
      return;
    }

    const cree = await reponse.json();

    // On relit la liste côté serveur plutôt que d'ajouter l'option à la
    // main : le menu reflète alors l'état réel du tenant.
    const rafraichis = await apiFetch("/customers");
    if (rafraichis.ok) clientsCharges = await rafraichis.json();
    remplirClients(cree.id);

    fermerModaleClient();
  } catch (err) {
    if (err instanceof SessionExpiree) {
      enregistrerJetons(null);
      afficherConnexion("Session expirée, reconnectez-vous.");
      return;
    }
    console.error(err);
    const boite = $("cl-erreurs");
    boite.replaceChildren(document.createTextNode("Création impossible : " + err.message));
    boite.hidden = false;
  } finally {
    bouton.disabled = false;
    bouton.textContent = "Créer le client";
  }
}

/* ------------------------------------------------------------------ *
 * Vues et cycle de vie
 * ------------------------------------------------------------------ */

function afficherConnexion(message) {
  $("vue-app").hidden = true;
  $("vue-connexion").hidden = false;
  const boite = $("erreur-connexion");
  if (message) { boite.textContent = message; boite.hidden = false; }
  else boite.hidden = true;
  // Les champs conservent ce que l'utilisateur a saisi, rien n'est
  // pré-rempli. Le focus va au premier champ vide.
  const premierVide = ["f-tenant", "f-email", "f-motdepasse"].find((id) => !$(id).value);
  $(premierVide || "f-motdepasse").focus();
}

function afficherApp() {
  $("vue-connexion").hidden = true;
  $("vue-app").hidden = false;
  // Le tableau de bord est la page d'accueil.
  montrerVue("tableau");
}

/* Message discret et transitoire : reutilise le bandeau d'alerte
   existant, mais s'efface tout seul pour ne pas rester en travers. */
let minuteurSucces = null;
function confirmer(texte) {
  const boite = $("succes-app");
  boite.textContent = texte;
  boite.hidden = false;
  clearTimeout(minuteurSucces);
  minuteurSucces = setTimeout(() => { boite.hidden = true; }, 8000);
}

let minuteurMessage = null;
function signaler(texte) {
  erreurApp(texte);
  clearTimeout(minuteurMessage);
  minuteurMessage = setTimeout(() => erreurApp(null), 6000);
}

function erreurApp(message) {
  const boite = $("erreur-app");
  if (message) { boite.textContent = message; boite.hidden = false; }
  else boite.hidden = true;
}

function erreurTableau(message) {
  const boite = $("erreur-tableau");
  if (message) { boite.textContent = message; boite.hidden = false; }
  else boite.hidden = true;
}

async function demarrer() {
  afficherApp();
  erreurApp(null);
  erreurTableau(null);
  try {
    rendre(await chargerFactures());
  } catch (err) {
    if (err instanceof SessionExpiree) {
      enregistrerJetons(null);
      afficherConnexion("Session expirée, reconnectez-vous.");
      return;
    }
    console.error(err);
    $("compte").textContent = "—";
    $("corps").replaceChildren();
    // Le message est pose sur les deux ecrans : l'accueil est le tableau
    // de bord, et un bandeau confine a la liste passerait inapercu.
    erreurApp(`Chargement impossible : ${err.message}`);
    erreurTableau(`Chargement impossible : ${err.message}`);
  }
}

$("form-connexion").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const bouton = $("btn-connexion");
  bouton.disabled = true;
  bouton.textContent = "Connexion…";
  try {
    const reponse = await fetch(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tenant_id: $("f-tenant").value.trim(),
        email: $("f-email").value.trim(),
        password: $("f-motdepasse").value,
      }),
    });
    if (!reponse.ok) {
      const detail = await reponse.json().catch(() => ({}));
      afficherConnexion(detail.detail || `Connexion refusée (HTTP ${reponse.status}).`);
      return;
    }
    const data = await reponse.json();
    if (data.mfa_required) {
      afficherConnexion("Ce compte exige un second facteur, non géré par cette page.");
      return;
    }
    enregistrerJetons({
      access_token: data.access_token,
      refresh_token: data.refresh_token,
    });
    $("f-motdepasse").value = "";
    await demarrer();
  } catch (err) {
    afficherConnexion(`API injoignable : ${err.message}`);
  } finally {
    bouton.disabled = false;
    bouton.textContent = "Se connecter";
  }
});

$("btn-deconnexion").addEventListener("click", () => {
  enregistrerJetons(null);
  afficherConnexion(null);
});

// Onglets : filtrage reel sur les factures deja chargees, donc aucun
// appel reseau et aucun rechargement de page.
for (const onglet of document.querySelectorAll(".onglet")) {
  onglet.addEventListener("click", () => {
    ongletActif = onglet.dataset.onglet;
    for (const autre of document.querySelectorAll(".onglet")) {
      autre.setAttribute("aria-selected", String(autre === onglet));
    }
    rendreTableau();
  });
}

// Navigation : simple bascule d'affichage, aucun appel reseau.
for (const lien of document.querySelectorAll(".nav-lien")) {
  lien.addEventListener("click", () => montrerVue(lien.dataset.vue));
}

// Selecteur de periode : recalcul sur les factures deja en memoire.
for (const bouton of document.querySelectorAll(".periode")) {
  bouton.addEventListener("click", () => {
    periodeActive = bouton.dataset.periode;
    for (const autre of document.querySelectorAll(".periode")) {
      autre.setAttribute("aria-pressed", String(autre === bouton));
    }
    rendreTableauDeBord();
  });
}

$("btn-voir-toutes").addEventListener("click", () => montrerVue("liste"));

$("btn-nouvelle").addEventListener("click", ouvrirFormulaire);
// Retour a l'ecran d'ou la saisie a ete ouverte, et non toujours a la liste.
$("btn-annuler").addEventListener("click", () => montrerVue(vueAvantFormulaire));
$("btn-ajouter-ligne").addEventListener("click", () => {
  const tr = ligneVide();
  $("lignes-corps").append(tr);
  tr.querySelector('[data-champ="designation"]').focus();
  recalculer();
});
$("form-facture").addEventListener("submit", envoyerFormulaire);

// Modale « Nouveau client ».
$("btn-nouveau-client").addEventListener("click", ouvrirModaleClient);
$("cl-annuler").addEventListener("click", fermerModaleClient);
$("form-client").addEventListener("submit", envoyerClient);
// Clic sur le voile, mais pas dans le panneau.
$("voile-client").addEventListener("click", (e) => {
  if (e.target === $("voile-client")) fermerModaleClient();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("voile-client").hidden) fermerModaleClient();
});

// Au chargement : on reprend la session si elle existe.
if (chargerJetons()) demarrer();
else afficherConnexion(null);
