# Déploiement derrière un proxy inverse

Cette note traite d'un seul point, mais il conditionne la protection contre
les attaques par force brute sur la connexion.

## Le problème

La limitation des tentatives de connexion compte les échecs par couple
(adresse du client, adresse e-mail). Cette adresse doit donc être celle du
client réel, et lui seul ne doit pas pouvoir la choisir.

Or uvicorn accepte les en-têtes de transfert par défaut, et traite l'adresse
de bouclage comme une source de confiance. Lancée sans précaution,
l'application croit donc tout `X-Forwarded-For` reçu localement et réécrit
l'adresse cliente en conséquence. Un attaquant qui fait tourner cet en-tête
présente une adresse différente à chaque essai et n'est jamais bloqué.

Préciser `--forwarded-allow-ips 127.0.0.1` ne change rien : c'est déjà la
valeur par défaut. Il faut couper la prise en compte des en-têtes, pas la
restreindre à ce qu'elle est déjà.

## En local et en conteneur, sans proxy

```bash
python scripts/serve.py
```

Le script coupe la prise en compte des en-têtes de transfert. L'adresse
retenue est celle de la connexion réelle, que le client ne contrôle pas.

## Derrière un proxy inverse

```bash
python scripts/serve.py --forwarded-allow-ips 10.0.0.7
```

Deux conditions doivent être réunies, et aucune ne suffit seule.

**Déclarer l'adresse précise du proxy.** Jamais `*`, que le script refuse
d'ailleurs explicitement : faire confiance à tout le monde revient à ne faire
confiance à personne, puisque n'importe quel client choisirait alors
l'adresse sur laquelle il est compté.

L'adresse déclarée doit être celle du proxy, et le proxy doit être le seul à
pouvoir l'emprunter. Si l'application reste joignable directement depuis cette
même adresse, la protection est nulle : un client qui l'atteint sans passer
par le proxy est alors traité comme le proxy lui-même, et ses en-têtes sont
crus. En pratique, l'application n'écoute que sur l'interface qui fait face au
proxy, et rien d'autre ne l'atteint.

**Configurer le proxy pour qu'il écrase `X-Forwarded-For`, et non qu'il
l'ajoute.** C'est le point le plus facile à manquer. uvicorn retient
l'élément le plus à droite de la chaîne. Un proxy qui ajoute l'adresse réelle
en fin de chaîne neutralise donc l'usurpation, car la valeur fournie par le
client se retrouve à gauche et n'est pas retenue. Un proxy qui transmettrait
l'en-tête tel quel laisserait au contraire la faille entière.

Pour nginx, la directive `proxy_add_x_forwarded_for` a ce comportement et
convient :

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Vérifier que la protection tient

Le contrôle se fait en une mesure. Sur un compte existant, enchaîner douze
tentatives de connexion avec un mot de passe faux, en faisant varier
`X-Forwarded-For` à chaque essai. Un refus `429` doit apparaître dès le
sixième essai, exactement comme sans en-tête. Si les douze tentatives passent,
la protection est contournée.

## Ce que cette configuration ne couvre pas

La limitation reste indexée sur le couple (adresse, e-mail). Elle protège un
compte donné contre la force brute, mais n'oppose aucun plafond global à un
attaquant qui répartit ses tentatives sur de nombreux comptes. Ce point est
distinct et reste ouvert.
