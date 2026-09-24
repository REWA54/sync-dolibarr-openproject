# Synchronisation Dolibarr ↔ OpenProject

Saisir une seule fois, n'importe où. Le service recopie dans l'autre outil :

| Dolibarr | OpenProject | Sens |
|---|---|---|
| Utilisateurs | Utilisateurs (appariés par e-mail) | ↔ |
| Tiers du projet | Champ texte « Client » du projet | ↔ (le texte est rapproché du tiers) |
| Projets | Projets | ↔ (une suppression devient une clôture ou un archivage) |
| Contacts internes du projet | Membres | ↔ |
| Tâches | Lots de travail | ↔ |
| Temps passés | Temps | ↔ (Dolibarr fait foi sur un temps facturé) |
| Note privée de la tâche | Commentaires du lot | ← seulement |

## Principe

Toutes les deux minutes, le service lit tout, des deux côtés, puis compare chaque objet relié
à deux instantanés : ce qu'il a vu en dernier dans Dolibarr, et ce qu'il a vu en dernier dans
OpenProject. Ce qui n'a changé que d'un côté est recopié de l'autre. Ce qui a changé des deux
côtés est tranché par la date de modification, et une alerte part.

OpenProject n'émet aucun webhook quand on modifie ou supprime un temps. Lire l'état complet ne
rate rien, même après une panne.

Garde-fous :
- **suppression confirmée** : un objet absent n'est tenu pour supprimé qu'après un accès direct qui répond 404. Un refus d'accès n'est jamais pris pour une suppression ;
- **disjoncteur** : plus de 5 suppressions prévues, ou des projets qui disparaissent tous d'un coup, et le cycle s'arrête avant toute écriture, avec une alerte ;
- **pas de doublon après une coupure** : chaque création est notée « en cours » avant d'être faite, et l'identifiant du jumeau est embarqué dans l'objet (attribut supplémentaire Dolibarr `openproject_id`, champ OpenProject « ID Dolibarr ») ;
- **projets gelés** : les tâches et les temps d'un projet clos ou archivé ne bougent plus ;
- **alertes** par webhook (Home Assistant, par exemple), rappelées au plus une fois par 24 h, avec un message de rétablissement.

Détail des choix : `docs/preparation.md` (mise en route), et les commentaires en tête de
`src/dolop/reconciliation.py` et `src/dolop/cycle.py`.

## Commandes

```sh
dolop verifier                            # chaque prérequis de la mise en route (lecture seule)
dolop simuler                             # ce qui serait fait, sans rien écrire (commande par défaut)
dolop une-fois                            # un cycle réel
dolop une-fois --confirmer-suppressions   # après un disjoncteur, une fois la simulation vérifiée
dolop service                             # boucle (commande du conteneur)
dolop rapport                             # liens, derniers cycles, alertes en cours
dolop annuler <n° de cycle>               # montre ce qu'un cycle a créé ; --oui pour le supprimer
dolop sante                               # code 0 si un cycle a réussi il y a moins de 15 min
```

Dans le conteneur : `docker exec -it sync-dolibarr-openproject dolop rapport`.

## Configuration (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `DOLIBARR_URL`, `DOLIBARR_API_KEY` | — | API Dolibarr (compte technique) |
| `OPENPROJECT_URL`, `OPENPROJECT_API_KEY` | — | API OpenProject (compte technique administrateur) |
| `OPENPROJECT_HOST` | — | en-tête Host si l'URL est interne (`http://openproject-proxy`) |
| `EXCLURE_LOGINS` | `admin` | comptes jamais synchronisés, ni eux ni leurs temps |
| `ALERTE_WEBHOOK_URL` | — | webhook Home Assistant (sinon alertes dans le journal seulement) |
| `DOLIBARR_ATTRIBUT` | `openproject_id` | code de l'attribut supplémentaire (projets et tâches) |
| `OPENPROJECT_CHAMP_REF` | `ID Dolibarr` | nom du champ personnalisé (projets, lots, temps) |
| `OPENPROJECT_CHAMP_CLIENT` | `Client` | nom du champ personnalisé texte des projets |
| `OPENPROJECT_ROLE_CHEF` / `_CONTRIBUTEUR` | `Project admin` / `Member` | rôles OpenProject des chefs et contributeurs |
| `OPENPROJECT_TYPE` | `Task` | type des lots créés depuis Dolibarr |
| `OPENPROJECT_ACTIVITE` | — | identifiant d'activité imposé aux temps créés dans OpenProject |
| `TACHE_HORS_TACHE` | `Temps hors tâche` | tâche Dolibarr qui reçoit les temps OpenProject saisis sans lot |
| `INTERVALLE_SECONDES` | `120` | pause entre deux cycles |
| `SEUIL_SUPPRESSIONS` / `SEUIL_POURCENT` | `5` / `20` | disjoncteur |
| `BASE_ETAT` | `/data/etat.sqlite` | mémoire du service |

## Déploiement

Image publiée par GitHub Actions (`.github/workflows/image.yml`) sous
`ghcr.io/<compte>/sync-dolibarr-openproject:sha-xxxxxxx`, après lint, typage strict et tests.
Exemple de stack : `compose.yaml`, sur un réseau Docker partagé avec Dolibarr et OpenProject.
Aucun port n'est publié.

## Développement

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest && .venv/bin/mypy && .venv/bin/ruff check src tests
```

`tests/test_cycle.py` rejoue les situations réelles sur deux faux outils en mémoire (conflits,
coupures, temps facturés, disjoncteur…) ; `tests/test_adaptateurs.py` vérifie les requêtes HTTP
envoyées aux vraies API.
