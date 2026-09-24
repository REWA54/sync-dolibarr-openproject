# Journal des versions

Format [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), numérotation [SemVer](https://semver.org/lang/fr/).

## [Non publié]

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
