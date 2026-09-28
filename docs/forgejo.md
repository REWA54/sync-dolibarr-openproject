# CI/CD sur une forge Forgejo auto-hébergée

Le dépôt porte deux chaînes équivalentes :

| | `.github/workflows/ci.yml` | `.forgejo/workflows/ci.yml` |
|---|---|---|
| Où | GitHub Actions (dépôt public ou miroir) | runner Forgejo auto-hébergé |
| Image | `ghcr.io/<compte>/sync-dolibarr-openproject` | registre de conteneurs de la forge |
| Signature | Sigstore sans clé + provenance SLSA | aucune : déployer par empreinte (`@sha256:…`) |
| Dépend de GitHub | oui | non : outils de PyPI et Docker Hub, figés par empreinte |

Forgejo lit `.forgejo/workflows` et ignore alors `.github/workflows`. GitHub fait l'inverse. Les deux
chaînes vérifient la même chose : tests sur trois versions de Python, lint, typage, bandit,
pip-audit, gitleaks, hadolint, actionlint, zizmor, shellcheck, Trivy et test de fumée en conteneur
durci. L'image n'est publiée que si tout passe.

## Le runner

Les jobs tournent en **mode hôte** (label `debian-13`) : directement sur une machine dédiée, sans
conteneur de job. C'est ce qui permet `docker build` et les `docker run -v "$PWD:…"` des outils sans
Docker-in-Docker. Contrepartie : un job peut tout faire sur cette machine. Elle ne doit donc servir
qu'à ça, jamais héberger d'autres services, et son pare-feu doit lui interdire le réseau local, sauf la
forge et le DNS.

Prérequis sur la machine du runner :

- Docker avec les greffons `buildx` et `compose` ;
- `git`, `curl`, `python3` ;
- `uv` dans le `PATH` du compte du runner (depuis PyPI, vérifié par empreinte) et les Python de la
  matrice : `uv python install 3.12 3.13 3.14`. Une version manquante serait téléchargée au premier
  job, depuis GitHub ;
- `forgejo-runner` (binaire vérifié par sa signature GPG), avec une capacité de 1 : les jobs passent
  un par un et ne se marchent pas dessus.

Enregistrement, limité à ce dépôt :

```sh
# sur la forge
forgejo actions generate-runner-token --scope <compte>/sync-dolibarr-openproject
# sur la machine du runner, avec le compte du runner
forgejo-runner register --no-interactive --instance https://forge.example.org \
  --token <jeton> --name runner-ci --labels debian-13:host
```

Le jeton automatique d'un job ne peut pas écrire dans le registre de Forgejo (réponse 401). La
publication utilise donc un jeton du propriétaire du dépôt, limité à **package : lecture et
écriture**, rangé dans le secret `REGISTRE_JETON` du dépôt :

1. Forgejo → avatar → *Paramètres* → *Applications* → *Générer un nouveau jeton*, avec la seule
   permission *package* en lecture et écriture ;
2. dépôt → *Paramètres* → *Actions* → *Secrets* → *Ajouter un secret* `REGISTRE_JETON`.

Sans ce secret, le job « Image » échoue au moment de publier, après toutes les vérifications. Le job
se connecte au registre dans un dossier de configuration Docker jetable : aucun identifiant ne reste
sur le runner.

## Déployer depuis le registre de la forge

Sur une forge privée, lire une image demande d'être authentifié : un jeton Forgejo limité à
`read:package` suffit, dans les registres de Portainer ou par `docker login`. Toujours déployer une
étiquette précise **et** son empreinte, affichée à la fin du job « Image » :

```
forge.example.org/<compte>/sync-dolibarr-openproject:sha-1234567@sha256:…
```

L'empreinte remplace ici la signature : une image modifiée dans le registre n'aurait plus la même.
