# Contribuer

Merci ! Ce projet synchronise des données de facturation : la prudence passe avant la vitesse.

## Mettre en place

Prérequis : [uv](https://docs.astral.sh/uv/), et Docker pour l'image.

```sh
make installer   # .venv avec les dépendances figées et vérifiées par empreinte
make verifier    # ruff, mypy strict, pytest avec couverture : la CI exige le même résultat
make securite    # bandit, pip-audit, gitleaks
make image       # image locale + test de fumée en conteneur durci
```

## Conventions

- **Français partout** : code (identifiants), commentaires, messages, documentation. Les termes
  techniques sans équivalent courant restent en anglais.
- **Un test raconte une situation réelle** (`test_temps_facture_supprime_dans_openproject_est_recree`).
  Toute correction de bogue ou de faille arrive avec le test qui échouait avant elle.
- **Aucune donnée réelle** dans le dépôt : ni nom de client, ni domaine (utiliser `example.org`),
  ni chemin de serveur, ni jeton. Les tests utilisent des données fictives.
- **Le moteur n'a pas d'entrée-sortie** (`reconciliation.py`) : des états en entrée, des actions
  en sortie. Les appels HTTP restent dans `dolibarr.py` et `openproject.py`.
- **Une écriture ne se rejoue jamais à l'aveugle** : c'est le registre « en cours » et la recherche
  de jumeau qui reprennent une création interrompue.
- Lignes de 120 caractères, `ruff format`, typage strict (`mypy --strict`).

## Proposer une modification

1. Une branche par sujet, depuis `master`.
2. `make verifier && make securite` avant de pousser.
3. Une demande de fusion qui dit **quoi**, **pourquoi**, et **comment c'est vérifié** (modèle fourni).
4. Toute la CI doit être verte ; les nouvelles dépendances passent par la revue des dépendances.

Dépendances : modifier `pyproject.toml`, puis `make verrous` (les versions en place sont gardées,
seules les nouvelles sont résolues). Ne jamais éditer `requirements*.txt` à la main.

## Publier une version

1. Déplacer la rubrique « Non publié » du [CHANGELOG](CHANGELOG.md) sous `## [X.Y.Z] - AAAA-MM-JJ`.
2. Mettre `version = "X.Y.Z"` dans `pyproject.toml`.
3. Commit, puis étiquette signée : `git tag -s vX.Y.Z -m vX.Y.Z && git push origin master vX.Y.Z`.

La CI construit, scanne, teste, publie `ghcr.io/rewa54/sync-dolibarr-openproject:X.Y.Z` (et `X.Y`,
`sha-…`), signe l'image, attache provenance et SBOM, puis crée la page de version avec les notes
du CHANGELOG. Numérotation [SemVer](https://semver.org/lang/fr/) : une version qui demande une
action lors de la mise à jour le dit dans le CHANGELOG.

## Failles de sécurité

Jamais dans une issue publique : voir [SECURITY.md](SECURITY.md).
