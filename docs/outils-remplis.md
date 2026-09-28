# Outils déjà remplis

Brancher le service sur un Dolibarr et un OpenProject qui servent déjà depuis des années demande
trois précautions que des outils neufs n'exigent pas :

1. **choisir ce qui circule** : un Dolibarr rempli compte des centaines de projets (affaires, avant-
   ventes, projets internes) qui n'ont rien à faire dans OpenProject, et inversement ;
2. **relier ce qui existe déjà des deux côtés**, sans quoi le premier cycle le recopierait en
   double (des temps en double se facturent deux fois) ;
3. **vérifier le premier chargement avant de le lancer**, puis le laisser durer.

Sans ces réglages, le service reste protégé : au-delà de `SEUIL_CREATIONS` créations prévues
(50 par défaut), le cycle s'arrête avant toute écriture et une alerte part.

## 1. Choisir ce qui circule

| Réglage | Effet |
|---|---|
| `PERIMETRE=choisi` | seuls les projets **cochés** « à synchroniser » d'un côté ou de l'autre circulent, avec leurs tâches, temps et membres. Un utilisateur n'est recopié (invité dans OpenProject, créé dans Dolibarr) que s'il est membre, assigné ou auteur de temps dans un projet coché |
| `OPENPROJECT_TYPES=Tâche,Jalon` | seuls les lots de ces types sont lus et recopiés dans Dolibarr ; vide : tous les types |
| `TEMPS_DEPUIS=2026-01-01` | les temps antérieurs ne sont ni lus, ni recopiés, ni supprimés : l'historique, souvent déjà facturé, n'est jamais touché |

Pour `PERIMETRE=choisi`, créer les deux cases, en plus de la [préparation](preparation.md) :

- **Dolibarr** : `/projet/admin/project_extrafields.php` → *Nouvel attribut* : libellé
  `Synchroniser avec OpenProject`, **code `synchro_openproject`**, type *Case à cocher* ;
- **OpenProject** : *Administration → Champs personnalisés → Projets* → *Nouveau champ* : nom
  **`Synchroniser avec Dolibarr`**, type *Booléen*, visible par les administrateurs.

Un projet recopié par le service est coché de l'autre côté. Un projet relié dont les deux cases sont
décochées **se fige** : plus rien n'y bouge, rien n'y est supprimé ; recoché, il reprend là où il
en était. Autres noms possibles : `DOLIBARR_ATTRIBUT_SYNCHRO`, `OPENPROJECT_CHAMP_SYNCHRO`.
`dolop verifier` contrôle ces deux champs et les types de lots.

## 2. Relier ce qui existe déjà

```sh
dolop apparier                                   # propositions dans appariement.csv, à côté de la base
dolop apparier --appliquer appariement.csv       # ce qui serait relié
dolop apparier --appliquer appariement.csv --oui # relie
```

Le fichier propose, sans rien écrire, les paires que le service ne rapproche pas tout seul : un
projet ↔ le projet **de même titre** (casse, accents et espaces ignorés), une tâche ↔ la tâche de
même titre dans le projet apparié, un temps ↔ le temps identique (même tâche, même personne, même
date, même durée). Utilisateurs et membres y figurent aussi, tels que le service les rapprocherait.
Deux objets du même nom d'un côté ne sont jamais devinés : ils apparaissent en « ? » à l'écran, à
trancher à la main (ajouter la ligne voulue au fichier).

Relire le fichier (tableur, séparateur `;`), **supprimer les lignes fausses**, puis appliquer. Le
lien est enregistré et l'identifiant du jumeau est posé de chaque côté ; aucun autre champ n'est
modifié. Au cycle suivant, ce qui diffère entre les deux objets est aligné sur le plus récemment
modifié : `dolop simuler` le montre avant.

## 3. Premier chargement

```sh
dolop verifier                                     # tout vert
dolop simuler --export /data/simulation.csv        # résumé à l'écran, détail complet dans le fichier
dolop une-fois --confirmer-creations               # le chargement, après relecture de la simulation
docker compose up -d                               # puis le service
```

Le premier chargement peut durer longtemps (plusieurs requêtes par objet créé). Il s'interrompt
sans dommage : chaque création est notée « en cours » avant d'être faite, et un arrêt en plein
milieu reprend là où il en était, sans doublon. Pendant ce temps, `dolop sante` reste vert tant que
le cycle avance.

## Lecture incrémentale

Au démarrage puis au plus toutes les `LECTURE_COMPLETE_MINUTES` (60), le service lit tout. Entre
deux, il ne lit que les tâches et les temps modifiés depuis le début du cycle réussi précédent ; les
membres attendent la lecture complète (Dolibarr ne les donne que projet par projet). Côté
OpenProject, le filtre est exact (dates en UTC). Côté Dolibarr, la base compare dans son propre
fuseau, que l'API ne dit pas : le filtre recule de `MARGE_DOLIBARR_MINUTES` (180, ce qui couvre une
base en UTC ou à l'heure de Paris). Ce qu'une marge trop courte laisserait passer est rattrapé à la
lecture complète suivante. `dolop verifier` s'assure que les deux outils acceptent ces filtres.

## Volumes et réglages

| Variable | Défaut | Quand l'augmenter |
|---|---|---|
| `LECTURES_PARALLELES` | 4 | beaucoup de projets et de tâches actifs dans Dolibarr (contacts lus un par un) ; sans dépasser ce que les serveurs supportent |
| `DELAI_HTTP_SECONDES` | 60 | un OpenProject lent à servir ses pages de lots |
| `TAILLE_PAGE_OPENPROJECT` | 200 | à baisser si les pages de lots expirent ; ne pas dépasser le maximum réglé dans OpenProject (*Administration → API*) |
| `SEUIL_CREATIONS` | 50 | plus de créations légitimes par cycle (saisie de temps en masse) |
| `SEUIL_SUPPRESSIONS_POURCENT` | 1 | seuil de suppressions : le plus grand de `SEUIL_SUPPRESSIONS` et de ce pourcentage des objets reliés |
| mémoire du conteneur | 256 Mo | 512 Mo au-delà de 40 000 objets reliés environ (mesuré : un temps lu pèse 0,8 Kio, et son lien autant) |
