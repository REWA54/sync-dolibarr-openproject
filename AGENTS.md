# AGENTS.md — consignes pour les agents IA

Service Python qui synchronise Dolibarr 24 et OpenProject 17 dans les deux sens, déployé comme un
conteneur Docker. Tout le projet est en français : identifiants, messages, documentation.

## Installer et vérifier (dans cet ordre)

```sh
make installer    # uv venv + dépendances figées (--require-hashes) + paquet en mode éditable
make verifier     # doit finir sans erreur avant toute proposition de modification
make securite     # bandit, pip-audit, gitleaks (Docker requis)
make image        # image amd64 + scripts/test-fumee.sh (Docker requis)
```

Sans `make` : les commandes exactes sont dans le `Makefile` et dans `.github/workflows/ci.yml`.

## Déployer pour quelqu'un

Suivre le README, section « Installation en 5 étapes ». La préparation des deux outils
(`docs/preparation.md`) se fait **dans leurs interfaces web par un humain administrateur** : un agent
ne peut que la vérifier, avec `dolop verifier` (lecture seule). Toujours `dolop simuler` avant le
premier `dolop une-fois` ou `docker compose up -d`, et montrer le résultat à l'humain.

## Carte du code

| Fichier | Rôle |
|---|---|
| `src/dolop/__main__.py` | ligne de commande, boucle du service, entretien quotidien |
| `src/dolop/cycle.py` | un cycle : lire, disjoncteur, écrire, commentaires |
| `src/dolop/reconciliation.py` | moteur pur (aucune entrée-sortie) : états → actions |
| `src/dolop/execution.py` | applique les actions, garde-fous qui relisent l'état réel |
| `src/dolop/entites.py` | règles par type d'objet (champs, sens, maître en cas de conflit) |
| `src/dolop/dolibarr.py`, `openproject.py` | adaptateurs HTTP de chaque outil |
| `src/dolop/etat.py` | base SQLite : liens, instantanés, journal, alertes, sauvegardes |
| `src/dolop/config.py` | variables d'environnement, validées au démarrage |
| `tests/faux.py` | deux faux outils en mémoire pour les scénarios de `test_cycle.py` |

## Règles à ne jamais enfreindre

1. **Aucune donnée réelle dans le dépôt** : pas de nom de client, de domaine (utiliser
   `example.org`), de chemin de serveur, d'adresse IP, de jeton. Le dépôt est public.
2. **Ne jamais exécuter `dolop une-fois`, `service` ou `annuler --oui` contre de vraies instances**
   sans accord explicite de l'humain, après lui avoir montré `dolop simuler`.
3. **Ne jamais lancer le service à deux endroits** (poste et serveur) sur les mêmes outils.
4. **Une écriture HTTP ne se rejoue jamais à l'aveugle** (voir `http.py`) ; ne pas ajouter de relance.
5. **Ne pas affaiblir un garde-fou** (404 confirmé, disjoncteur, registre « en cours », verrou,
   assainissement HTML, vérification d'identité des identifiants embarqués) sans le demander.
6. **Toute correction arrive avec un test** qui échoue sans elle ; les tests racontent une situation.
7. Dépendances : modifier `pyproject.toml` puis `make verrous` ; ne jamais éditer `requirements*.txt`.
8. Actions GitHub : toujours épinglées par empreinte de commit, avec la version en commentaire.

## Pièges connus

- OpenProject 17 répond 403, même à un administrateur, aux listes de lots, rôles et types tant
  qu'**aucun projet actif** n'existe : l'adaptateur renvoie des listes vides, `verifier` affiche ⏳.
- Dolibarr n'enregistre jamais `ref_ext` des projets par l'API : l'identifiant OpenProject vit dans
  l'attribut supplémentaire `openproject_id`.
- `addtimespent` (Dolibarr) ne renvoie pas l'identifiant créé : il est retrouvé par différence.
- Les webhooks OpenProject ne signalent ni modification ni suppression de temps : d'où la lecture
  complète à chaque cycle.
