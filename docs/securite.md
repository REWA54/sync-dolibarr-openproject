# Sécurité : modèle de menace, audit et mesures

Ce document décrit ce que le service protège, contre quoi, comment, et ce qui reste à la charge de
l'exploitant. Il se termine par le compte rendu du dernier audit.

## 1. Ce que le service manipule

| Actif | Pourquoi c'est sensible |
|---|---|
| Deux jetons d'API **administrateur** (Dolibarr, OpenProject) | qui les détient lit et modifie tout, dans les deux outils |
| Données métier : projets, tâches, temps facturables, noms et e-mails des utilisateurs | confidentialité, et exactitude de la facturation |
| Base d'état SQLite (`/data`) et ses sauvegardes | contient des instantanés de ces données, et les liens entre les deux outils |
| Adresse du webhook d'alerte | vaut un mot de passe : qui la connaît peut envoyer des notifications |

## 2. Surface d'attaque

- **Aucun port entrant.** Le service ne fait qu'appeler les deux API et le webhook.
- **Entrées non fiables** : tout ce que saisissent les utilisateurs des deux outils (titres,
  descriptions, commentaires, notes de temps, champs personnalisés), et les réponses des API.
- **Chaîne d'approvisionnement** : dépendances Python, image de base, actions GitHub, registre ghcr.io.
- **Hôte** : le dossier `/data` et les variables d'environnement du conteneur.

## 3. Mesures en place

### Dans le code
- **Contenu assaini** : tout HTML écrit dans Dolibarr (descriptions, commentaires recopiés) passe par
  une liste d'autorisation (`nh3`) : ni script, ni gestionnaire d'événement, ni lien `javascript:`.
- **Identifiants embarqués vérifiés** : l'attribut Dolibarr « ID OpenProject » est modifiable par
  tout utilisateur qui édite un projet ; un identifiant que seul Dolibarr affirme doit désigner un
  objet de même identité, sinon il est ignoré (voir `reconciliation.py`).
- **Suppression prudente** : 404 confirmé par un accès direct, jamais un 403 ; disjoncteur avant
  toute écriture si trop de suppressions sont prévues ; tâches portant du temps jamais supprimées.
- **Aucun droit propagé** : un utilisateur créé par le service n'est jamais administrateur.
- **Secrets** : jamais dans la représentation de la configuration, ni dans les journaux (l'adresse
  du webhook est masquée en cas d'échec) ; lecture possible depuis un fichier (secrets Docker).
- **Configuration validée au démarrage** : adresses `http(s)` sans identifiants, fuseau, bornes.
- **HTTP** : TLS vérifié, redirections refusées, délais bornés ; écritures jamais rejouées à
  l'aveugle (seulement si la connexion n'a jamais été établie).
- **SQL** : requêtes toutes paramétrées ; filtres Dolibarr construits à partir d'entiers.
- **Fichiers privés** : base, sauvegardes et verrou créés en `0600` (umask `077`).
- **Un seul cycle à la fois** (verrou `flock`), base vérifiée au démarrage, sauvegarde quotidienne.

### Dans l'image et le conteneur
- construction en deux étapes, image de base épinglée par empreinte, dépendances et outil de
  construction installés avec `--require-hashes` ;
- pip retiré de l'image finale : aucun outil d'installation à l'exécution ;
- utilisateur `1000:1000`, jamais root ; exemple `compose.yaml` en lecture seule, `cap_drop: ALL`,
  `no-new-privileges`, mémoire, processeur et nombre de processus bornés, journaux tournants.

### Dans la chaîne de publication
- toutes les actions GitHub épinglées par empreinte de commit, jetons en lecture seule par défaut,
  droits accordés job par job ;
- publication bloquée par : tests (3.12 et 3.13), ruff (règles de sécurité), mypy strict, bandit,
  pip-audit, gitleaks sur tout l'historique, hadolint, actionlint, zizmor, shellcheck, Trivy
  (aucune faille HIGH/CRITICAL corrigeable) et un test de fumée en conteneur durci ;
- CodeQL (Python et workflows), OpenSSF Scorecard, revue des dépendances des demandes de fusion,
  Dependabot chaque semaine ; toute la CI rejouée chaque lundi pour les failles nouvelles ;
- images signées sans clé (Sigstore, `cosign`), avec provenance SLSA et SBOM.

## 4. Risques résiduels

| Risque | Pourquoi il reste | Ce qui le borne |
|---|---|---|
| Jetons administrateur | OpenProject exige un administrateur pour inviter des utilisateurs et saisir du temps au nom de chacun | réseau interne, aucun port, fichiers de secrets, rotation (§ 5) |
| Un utilisateur OpenProject qui supprime un lot supprime la tâche Dolibarr | c'est la synchronisation voulue | disjoncteur, 404 confirmé, refus si du temps est saisi, `dolop annuler` |
| Paquets Debian de l'image de base sans correctif publié | l'éditeur n'a pas encore livré de correctif | aucun shell ni montage exposé ; CI hebdomadaire, Dependabot sur l'image |
| Trafic interne en HTTP | le réseau Docker partagé est privé à l'hôte | ne jamais publier les ports des API ; `https://` sinon |
| Deux instances sur deux machines | le verrou ne voit que sa propre base | règle d'exploitation : un seul service à la fois |
| HTML saisi directement dans Dolibarr | hors du service ; Dolibarr filtre lui-même son affichage | le service n'écrit que du HTML assaini |

## 5. Recommandations pour l'exploitant

- [ ] Comptes techniques dédiés, jetons propres au service, renouvelés chaque année et **révoqués
      dès qu'un jeton temporaire a servi** (déploiement, dépannage).
- [ ] Dolibarr : attribut « ID OpenProject » en visibilité `5` (affiché, non modifiable), voir
      [`preparation.md`](preparation.md) B1.
- [ ] OpenProject : les trois champs « ID Dolibarr » réservés aux administrateurs.
- [ ] `.env` en `0600`, ou secrets Docker (`*_FILE`) ; jamais de jeton dans un dépôt.
- [ ] Vérifier la signature de l'image avant chaque mise à jour (`cosign verify`, voir le README).
- [ ] Inclure le dossier `/data` (base d'état et `sauvegardes/`) dans la sauvegarde de l'hôte.
- [ ] Sur GitHub : protection de la branche principale (CI obligatoire, pas de force-push),
      signalement privé des failles, alertes Dependabot et analyse des secrets activés, 2FA.

## 6. Compte rendu du dernier audit

**Date :** 24 septembre 2026. **Version auditée :** 0.1.0 (commit `96780bb`) et son image publiée.
**Méthode :** lecture intégrale du code ; bandit, ruff (règles `S`), pip-audit, gitleaks sur tout
l'historique, Trivy sur l'image publiée, hadolint, actionlint, zizmor ; lecture du code source de
l'API Dolibarr 24.0.0 pour les points de comportement. Chaque faille corrigée a son test, vérifié
en échec sur l'ancien code.

| # | Gravité | Constat | Correction (0.2.0) |
|---|---|---|---|
| 1 | Élevée | HTML et liens `javascript:` saisis dans OpenProject (commentaires, descriptions) recopiés tels quels dans Dolibarr : script stocké potentiel, exécuté dans la session de qui ouvre la tâche | HTML assaini par liste d'autorisation (`nh3`) — `tests/test_securite.py` |
| 2 | Moyenne | Un identifiant tapé à la main dans l'attribut Dolibarr « ID OpenProject » reliait le projet à n'importe quel projet OpenProject non relié (archivé, par exemple), puis écrasait ce dernier et en recopiait les lots | identité exigée quand seul Dolibarr affirme le lien — test « projet étranger » |
| 3 | Moyenne | Deux cycles simultanés possibles (service + `dolop une-fois` à la main) : doublons | verrou `flock` par base d'état |
| 4 | Moyenne | Image publiée : pip embarqué, deux failles HIGH corrigeables (msgpack GHSA-6v7p-g79w-8964, setuptools CVE-2025-47273, embarqués par pip) | image en deux étapes, pip retiré : 0 faille corrigeable |
| 5 | Moyenne | Adresse du webhook (secret) écrite dans le journal quand l'envoi d'une alerte échoue | message d'erreur sans adresse |
| 6 | Faible | Jetons visibles dans la représentation de la configuration ; aucune validation (adresses, bornes, fuseau) ; `TZ` du conteneur ignoré | champs masqués, validation au démarrage, `FUSEAU` puis `TZ` |
| 7 | Faible | Aucune sauvegarde de la base d'état ; historique sans limite ; sonde de santé qui ouvrait la base en écriture ; base endommagée = trace brute | sauvegarde quotidienne, purge, lecture seule, arrêt net avec alerte |
| 8 | Faible | Une erreur hors cycle (disque plein…) arrêtait le service | la boucle survit, la sonde de santé signale |
| 9 | Faible | CI : actions non épinglées, dépendances de développement non figées, aucune analyse de sécurité, image publiée sans scan ni signature | chaîne décrite au § 3 |
| 10 | Info | Base d'état lisible par tous les comptes de l'hôte (umask par défaut) | `0600` |
| 11 | Info | Écriture jamais rejouée, même quand la requête n'était pas partie ; 429 non géré | relance sûre, `Retry-After` plafonné |
| 12 | Info | SHA-1 (empreinte, pas de sécurité) et nom de colonne SQL composé (liste fermée) signalés par bandit | intention explicite, requêtes littérales |

**Vérifié sans problème :** aucun secret dans l'historique git ; aucune faille connue dans les
dépendances Python ; TLS vérifié et redirections refusées ; requêtes SQL paramétrées ; aucune
exécution de commande ni désérialisation dangereuse ; aucun droit administrateur propagé ; conteneur
non root sans port publié.
