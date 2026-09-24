# Politique de sécurité

## Signaler une faille

**Ne pas ouvrir d'issue publique.** Utiliser le signalement privé de GitHub :
onglet [Security → Report a vulnerability](https://github.com/REWA54/sync-dolibarr-openproject/security/advisories/new).

Merci d'indiquer la version (`dolop --version` ou l'étiquette de l'image), les étapes pour reproduire
et l'impact supposé. Ne jamais joindre de jeton d'API, de base d'état ni de donnée réelle.

| Étape | Délai visé |
|---|---|
| Accusé de réception | 3 jours ouvrés |
| Premier diagnostic | 10 jours ouvrés |
| Correctif publié (gravité élevée ou critique) | 30 jours |

La faille est rendue publique (avis GitHub, entrée du CHANGELOG) une fois le correctif disponible,
avec remerciements si vous le souhaitez.

## Versions suivies

Seule la dernière version publiée reçoit des correctifs de sécurité. Mettre à jour en changeant
l'étiquette de l'image : voir [`docs/exploitation.md`](docs/exploitation.md).

## Dans le périmètre

- le code du service (`src/dolop`), son image Docker et l'exemple `compose.yaml` ;
- la chaîne de construction et de publication (`.github/workflows`).

Hors périmètre : les failles de Dolibarr ou d'OpenProject eux-mêmes (à signaler à leurs éditeurs),
et une installation qui s'écarte de la documentation (port publié, conteneur en root…).

## Ce qui est déjà en place

Le modèle de menace, le dernier audit et la liste des mesures sont dans
[`docs/securite.md`](docs/securite.md). Chaque publication d'image est bloquée par :
tests, CodeQL, bandit, pip-audit, gitleaks (tout l'historique), hadolint, actionlint, zizmor
et Trivy (aucune faille HIGH ou CRITICAL corrigeable tolérée). Les images publiées sont signées
(Sigstore) et accompagnées de leur provenance et de leur SBOM.
