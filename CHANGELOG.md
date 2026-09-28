# Journal des versions

Format [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), numérotation [SemVer](https://semver.org/lang/fr/).

## [Non publié]

### Outils remplis — étape 1 : lire sans s'essouffler
- Temps de calcul linéaire : la liste des tâches « Temps hors tâche » était recalculée pour chaque
  tâche et chaque temps lus (20 000 temps : 2 s de calcul ; 100 000 : près d'une minute). Elle est
  lue une fois par cycle, par une requête filtrée sur l'attribut Dolibarr.
- Listes lues et converties page par page, sans garder les pages : la mémoire ne suit plus le
  volume brut (un lot OpenProject pèse une vingtaine de Kio une fois lu).
- Ce qui ne se lit qu'objet par objet est lu en parallèle (`LECTURES_PARALLELES`, 4 par défaut) :
  assignés des tâches et membres des projets Dolibarr, fiches des tiers.
- Tiers Dolibarr : seules les fiches des clients des projets sont lues, gardées d'un cycle à
  l'autre ; la liste complète ne sert plus qu'à retrouver un client tapé dans OpenProject.
- Temps anciens saisis sans heure : une requête par tâche au lieu d'une par temps.
- `dolop verifier` ne lit plus qu'une page de chaque liste pour prouver les droits.
- `dolop sante` reste vert pendant un long cycle qui avance (premier chargement d'un outil rempli) :
  le cycle laisse un signe de vie à chaque réponse des outils. Il passe au rouge si ce signe s'arrête
  ou si le cycle précédent a échoué. Délai réglable : `SANTE_MINUTES`.
- `dolop simuler` affiche un résumé par outil, type et action, puis les 40 premières écritures ;
  `--export fichier.csv` donne la liste complète.
- Nouvelles variables : `TAILLE_PAGE_DOLIBARR` (100), `TAILLE_PAGE_OPENPROJECT` (200, au lieu de 500
  en dur), `DELAI_HTTP_SECONDES` (60, au lieu de 30 en dur), `LECTURES_PARALLELES` (4),
  `SANTE_MINUTES` (15). `DOLIBARR_ATTRIBUT` est désormais refusé s'il contient autre chose que des
  lettres, chiffres et `_` (il entre dans un filtre de l'API).

### Chaîne CI/CD
- CI/CD sur une forge Forgejo auto-hébergée, sans GitHub (`.forgejo/workflows/ci.yml`,
  [`docs/forgejo.md`](docs/forgejo.md)) : mêmes vérifications, jobs sur un runner en mode hôte, outils
  de PyPI et Docker Hub figés par empreinte, image publiée dans le registre de la forge. Le workflow
  GitHub reste en place pour l'image publique.
- Tests aussi sous Python 3.14 ; la CI refuse une image dont la version de Python n'est pas
  dans la matrice de tests (une montée de version proposée par Dependabot doit d'abord y entrer).

### Corrections
- `dolop verifier` contrôle le champ « ID Dolibarr » des lots dans **chaque** projet actif (et non
  plus le premier seulement) et distingue les deux réglages en cause : champ non activé pour le type
  (configuration du formulaire du type) ou champ pas « pour tous les projets ».
- Test de fumée : faux échecs aléatoires supprimés (`docker logs | grep -q` avec `pipefail`), et la
  sauvegarde n'est plus cherchée avant d'avoir été écrite.

### Documentation
- Vérification de la signature de l'image : cosign 3 ou plus récent (la version 2 ne la trouve pas).
- Préparation : le champ « ID Dolibarr » des lots doit être activé pour chaque type, en plus de
  « pour tous les projets ».

## [0.2.0] - 2026-09-24

Audit de sécurité et de résilience : compte rendu dans [`docs/securite.md`](docs/securite.md).

### À faire lors de la mise à jour
- Aucune action obligatoire : la base d'état et les variables existantes restent valables.
- Recommandé : appliquer le durcissement du nouvel exemple `compose.yaml` (lecture seule,
  `cap_drop`, `no-new-privileges`, limites) et régler la visibilité de l'attribut Dolibarr
  « ID OpenProject » sur `5` (voir `docs/preparation.md`, B1).
- Nouveau dossier `/data/sauvegardes/` : l'inclure dans la sauvegarde de l'hôte.

### Sécurité
- HTML assaini (liste d'autorisation `nh3`) avant toute écriture dans Dolibarr : un commentaire
  ou une description OpenProject ne peut plus y glisser de script ni de lien `javascript:`.
- Un identifiant « ID OpenProject » tapé à la main dans Dolibarr ne peut plus relier un projet ou
  une tâche à un objet OpenProject sans rapport : l'identité doit aussi correspondre.
- L'adresse du webhook n'apparaît plus dans le journal quand une alerte ne part pas.
- Jetons masqués dans la représentation de la configuration ; secrets lisibles depuis un fichier
  (`*_FILE`, secrets Docker) ; configuration validée au démarrage.
- Image : construction en deux étapes, base épinglée par empreinte, outil de construction vérifié
  par empreinte, pip retiré de l'image finale (deux failles HIGH en moins).
- Base d'état, sauvegardes et verrou créés en `0600`.

### Résilience
- Verrou : un seul cycle à la fois sur une même base (service et commande manuelle).
- Le service survit à toute erreur hors cycle ; base vérifiée au démarrage, alerte si elle est abîmée.
- Sauvegarde quotidienne à chaud de la base d'état (`SAUVEGARDES`, 7 par défaut) et nouvelle
  commande `dolop sauvegarder` ; purge de l'historique au-delà de `CONSERVATION_JOURS` (180).
- `dolop sante` et `dolop rapport` ouvrent la base en lecture seule.
- Écriture rejouée seulement si la connexion n'a jamais été établie ; lectures relancées sur 429
  en respectant `Retry-After`.
- `FUSEAU` retombe sur `TZ` du conteneur ; nouvelle option `dolop --version`.

### Chaîne CI/CD
- Pipeline unique : tests Python 3.12 et 3.13, ruff (règles de sécurité), mypy strict, bandit,
  pip-audit, gitleaks, hadolint, actionlint, zizmor, shellcheck, Trivy, test de fumée en
  conteneur durci ; rien n'est publié si une étape échoue.
- Images signées (Sigstore), provenance et SBOM ; étiquettes `X.Y.Z` et `X.Y` sur les versions.
- CodeQL, OpenSSF Scorecard, revue des dépendances, Dependabot ; passage complet chaque lundi.
- Actions épinglées par empreinte, droits des jetons réduits job par job.

### Documentation
- README refait, `SECURITY.md`, `CONTRIBUTING.md`, `AGENTS.md`, `docs/exploitation.md`,
  `docs/securite.md`, modèles d'issues et de demandes de fusion, licence MIT.

## [0.1.0] - 2026-09-24

Première version : synchronisation bidirectionnelle des utilisateurs, projets, membres, tâches et
temps, commentaires OpenProject recopiés dans la note des tâches Dolibarr, simulation, disjoncteur,
alertes par webhook, commandes `verifier`, `rapport` et `annuler`.

[Non publié]: https://github.com/REWA54/sync-dolibarr-openproject/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/REWA54/sync-dolibarr-openproject/compare/96780bb...v0.2.0
[0.1.0]: https://github.com/REWA54/sync-dolibarr-openproject/commit/96780bb
