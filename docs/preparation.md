# Mise en route

Tout se fait dans les interfaces de Dolibarr et d'OpenProject. Chaque étape dit comment revenir
en arrière. Dans les adresses, remplacer `dolibarr.example.org` et `openproject.example.org` par
les vôtres.

À la fin, `dolop verifier` doit être entièrement vert. Il est en lecture seule et dit précisément
ce qui manque.

---

## A. OpenProject

**A1. Nettoyer.** Supprimer ou archiver les projets de démonstration : un projet archivé est
ignoré par la synchronisation. Vérifier qu'un même e-mail n'est pas porté par deux comptes : c'est
par l'e-mail qu'un utilisateur Dolibarr et un utilisateur OpenProject sont reconnus comme la même
personne.

**A2. Compte technique.** *Administration → Utilisateurs → + Utilisateur* : identifiant `sync`,
**Administrateur** coché. Lui donner un mot de passe, s'y connecter dans une fenêtre privée, puis
*Paramètres du compte → Jetons d'accès → API → + Jeton API*. Copier le jeton, qui ne sera plus
jamais affiché.
Administrateur, car il doit voir tous les projets, inviter des utilisateurs et saisir du temps au
nom de chacun.
↩︎ Supprimer le jeton, ou verrouiller le compte, coupe tout accès du service.

**A3. Champs personnalisés**, tous de type **Texte**, non obligatoires, **« pour tous les projets »** :

| Adresse | Nom | Réglages |
|---|---|---|
| `https://openproject.example.org/admin/settings/project_custom_fields` | `Client` | visible de tous |
| même page | `ID Dolibarr` | *Administrateurs uniquement* |
| `https://openproject.example.org/custom_fields?tab=WorkPackageCustomField` | `ID Dolibarr` | *Administrateurs uniquement*, **cocher tous les types** |
| `https://openproject.example.org/custom_fields?tab=TimeEntryCustomField` | `ID Dolibarr` | *Administrateurs uniquement* |

`Client` se remplit à la main en créant un projet dans OpenProject. On y tape le nom ou le code du
tiers Dolibarr ; accents et majuscules n'importent pas. Un nom inconnu n'empêche pas la création :
le projet arrive dans Dolibarr sans client, et une alerte part.
Les trois `ID Dolibarr` servent au service à reconnaître chaque objet. Ne jamais les modifier à la main.
↩︎ Chaque champ se supprime depuis la même page.

## B. Dolibarr

**B1. Attribut « ID OpenProject »**, sur les projets et sur les tâches :
- `https://dolibarr.example.org/projet/admin/project_extrafields.php`
- `https://dolibarr.example.org/projet/admin/project_task_extrafields.php`

*Nouvel attribut* : libellé `ID OpenProject`, **code `openproject_id`**, type *Chaîne*, non obligatoire.
Dolibarr n'enregistre pas la référence externe d'un projet reçue par son API : l'identifiant
OpenProject doit donc vivre dans cet attribut.
↩︎ Supprimable depuis la même page.

**B2. Compte technique.** *Utilisateurs & Groupes → Nouvel utilisateur* : identifiant `sync`,
**Administrateur : oui**. Dans *Jeton pour API*, générer un jeton et le copier avant d'enregistrer.
Puis, onglet **Permissions** : dans Dolibarr, être administrateur **ne donne pas** les droits des
modules. Accorder :
- Projets : lire, créer/modifier, supprimer, y compris sur **tous** les projets ;
- Tiers : lire, voir tous les tiers ;
- Utilisateurs : lire, créer/modifier, supprimer ou désactiver.

↩︎ Désactiver l'utilisateur coupe tout accès du service.

## C. Configuration et vérification

Copier `.env.exemple` en `.env`, y coller les deux jetons, et régler :
- `EXCLURE_LOGINS` : les comptes administrateurs et le compte technique de chaque outil ;
- `OPENPROJECT_TYPE` : le nom exact du type de lot à utiliser (« Task » en anglais, « Tache » dans
  une instance installée en français).

Puis :

```sh
dolop verifier   # tout doit être vert (⏳ tant qu'aucun projet actif n'existe dans OpenProject)
dolop simuler    # ce qui serait fait, sans rien écrire
dolop une-fois   # premier cycle réel ; « dolop annuler <n°> --oui » le défait
```

Bon à savoir :
- les projets **clos** jamais synchronisés restent dans Dolibarr. Un projet archivé n'accepte plus
  de tâches dans OpenProject ; s'il est rouvert, il arrive avec ses tâches ;
- ce qui est saisi par un compte exclu (par exemple `admin`) n'est jamais synchronisé, ses temps
  compris. Travailler dans OpenProject avec son compte personnel.
