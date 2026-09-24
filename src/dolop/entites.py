"""Règles de synchronisation de chaque type d'objet.

L'ordre de ``ORDRE`` est celui des créations et modifications (un projet avant ses tâches) ;
les suppressions se font dans l'ordre inverse (les temps avant leur tâche).
"""

from __future__ import annotations

from typing import Any

from .conversions import cle_nom, texte_normalise
from .modele import Champ, Regles


def _email(v: Any) -> str:
    return str(v or "").strip().casefold()


def _texte(v: Any) -> str:
    return texte_normalise(v)


UTILISATEUR = Regles(
    type="utilisateur",
    libelle="Utilisateur",
    champs=(
        Champ("email", normaliser=_email),
        Champ("prenom", normaliser=_texte),
        Champ("nom", normaliser=_texte),
        Champ("actif"),
        Champ("login", creation_seule=True),
    ),
    suppression="jamais",
    maitre="dol",
    identite=("email",),
    apparier=True,
    ignorer_inactifs=True,
)

PROJET = Regles(
    type="projet",
    libelle="Projet",
    champs=(
        Champ("titre", normaliser=_texte),
        Champ("description", normaliser=_texte),
        Champ("actif"),
        Champ("client", normaliser=cle_nom),
        Champ("code", creation_seule=True),
    ),
    suppression="cloturer",
    maitre="dol",
    identite=("titre",),
    # Un projet clos jamais synchronisé reste dans Dolibarr : l'archiver vide dans OpenProject
    # n'apporterait rien (un projet archivé n'accepte plus de tâches). Rouvert, il sera recopié.
    ignorer_inactifs=True,
)

MEMBRE = Regles(
    type="membre",
    libelle="Membre de projet",
    champs=(
        Champ("projet", ref="projet", creation_seule=True),
        Champ("utilisateur", ref="utilisateur", creation_seule=True),
        Champ("role"),
    ),
    suppression="propager",
    maitre="dol",
    identite=("projet", "utilisateur"),
    apparier=True,
    champ_projet="projet",
)

TACHE = Regles(
    type="tache",
    libelle="Tâche",
    champs=(
        Champ("projet", ref="projet"),
        Champ("parent", ref="tache"),
        Champ("titre", normaliser=_texte),
        Champ("description", normaliser=_texte),
        Champ("debut"),
        Champ("fin"),
        Champ("charge"),
        Champ("avancement"),
        Champ("assigne", ref="utilisateur"),
    ),
    suppression="propager",
    # Dolibarr ne date pas les modifications de tâches : sans horodatage des deux côtés,
    # OpenProject, outil de suivi au quotidien, l'emporte.
    maitre="op",
    identite=("projet", "titre"),
    champ_projet="projet",
)

TEMPS = Regles(
    type="temps",
    libelle="Temps passé",
    champs=(
        Champ("projet", ref="projet"),
        Champ("tache", ref="tache"),
        Champ("date"),
        Champ("duree"),
        Champ("utilisateur", ref="utilisateur"),
        Champ("note", normaliser=_texte),
    ),
    suppression="propager",
    # Le temps sert à facturer : sans horodatage des deux côtés, Dolibarr l'emporte.
    maitre="dol",
    identite=("projet", "tache", "utilisateur", "date", "duree"),
    champ_projet="projet",
)

ORDRE: tuple[Regles, ...] = (UTILISATEUR, PROJET, MEMBRE, TACHE, TEMPS)
PAR_TYPE: dict[str, Regles] = {r.type: r for r in ORDRE}
