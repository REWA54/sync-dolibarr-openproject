"""Objets communs aux deux côtés : enregistrements canoniques, règles par type, liens et actions.

Un enregistrement porte des valeurs *canoniques* : même langage des deux côtés (Markdown pour
les textes, secondes pour les durées, dates ISO, booléens), sauf les références vers d'autres
objets, qui restent des identifiants natifs de leur propre côté et passent par la table des liens.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Cote = Literal["dol", "op"]
COTES: tuple[Cote, Cote] = ("dol", "op")


def autre(cote: Cote) -> Cote:
    return "op" if cote == "dol" else "dol"


NOM_COTE: dict[Cote, str] = {"dol": "Dolibarr", "op": "OpenProject"}


@dataclass(frozen=True)
class Enreg:
    """Un objet tel que lu d'un côté."""

    id: str
    champs: Mapping[str, Any]
    modifie_le: datetime | None = None
    # Identifiant de l'objet jumeau de l'autre côté, embarqué dans l'objet lui-même
    # (attribut supplémentaire Dolibarr, champ personnalisé OpenProject).
    ref_autre: str | None = None
    libelle: str = ""


@dataclass(frozen=True)
class Champ:
    nom: str
    # Type d'entité référencée : la valeur est un identifiant natif, traduit par les liens.
    ref: str | None = None
    # Utilisé à la création seulement (identifiant de connexion, code projet, clés d'un membre).
    creation_seule: bool = False
    # Normalisation pour comparer deux valeurs venant de côtés différents.
    normaliser: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class Regles:
    type: str
    libelle: str
    champs: tuple[Champ, ...]
    suppression: Literal["propager", "cloturer", "jamais"]
    # Quand un conflit ne peut pas être tranché à l'horodatage.
    maitre: Cote
    # Champs qui identifient un objet : appariement naturel et recherche de jumeau.
    identite: tuple[str, ...]
    # Appariement d'objets non liés par leur identité (utilisateurs par e-mail, membres par couple).
    apparier: bool = False
    # Le type dépend d'un projet : gelé si le projet l'est.
    champ_projet: str | None = None
    # Un objet inactif (projet clos, compte désactivé) jamais relié n'est pas recopié de l'autre côté.
    ignorer_inactifs: bool = False

    def champ(self, nom: str) -> Champ:
        for c in self.champs:
            if c.nom == nom:
                return c
        raise KeyError(nom)

    @property
    def synchronises(self) -> tuple[Champ, ...]:
        return tuple(c for c in self.champs if not c.creation_seule)


@dataclass
class Lien:
    id: int
    type: str
    dol_id: str
    op_id: str
    snap_dol: dict[str, Any] | None = None
    snap_op: dict[str, Any] | None = None
    # Lien rompu : les deux objets sont connus mais ne se synchronisent plus.
    rompu: bool = False

    def id_de(self, cote: Cote) -> str:
        return self.dol_id if cote == "dol" else self.op_id

    def snap_de(self, cote: Cote) -> dict[str, Any] | None:
        return self.snap_dol if cote == "dol" else self.snap_op


@dataclass(frozen=True)
class EnCours:
    """Création commencée mais dont le lien n'a pas encore été enregistré."""

    type: str
    cible: Cote
    source_id: str


class Intraduisible(Exception):
    """Une valeur ne peut pas (encore) être exprimée de l'autre côté.

    ``alerter`` distingue l'attente normale (l'objet référencé sera relié au prochain cycle)
    d'un blocage qui demande une action humaine (client introuvable, e-mail manquant).
    """

    def __init__(self, message: str, *, alerter: bool = True):
        super().__init__(message)
        self.alerter = alerter


# --------------------------------------------------------------------------- actions


@dataclass(frozen=True)
class Relier:
    dol_id: str
    op_id: str
    motif: str


@dataclass(frozen=True)
class Creer:
    cible: Cote
    source: Enreg


@dataclass(frozen=True)
class Conflit:
    champ: str
    gagnant: Cote
    valeur_dol: Any
    valeur_op: Any


@dataclass(frozen=True)
class Synchroniser:
    """Propager des champs d'un lien vers l'un ou l'autre côté, et rafraîchir les instantanés.

    ``vers_dol`` et ``vers_op`` contiennent des valeurs exprimées dans l'espace du côté *source*.
    """

    lien: Lien
    dol: Enreg
    op: Enreg
    vers_dol: Mapping[str, Any] = field(default_factory=dict)
    vers_op: Mapping[str, Any] = field(default_factory=dict)
    conflits: tuple[Conflit, ...] = ()

    @property
    def ecrit(self) -> bool:
        return bool(self.vers_dol or self.vers_op)


@dataclass(frozen=True)
class Supprimer:
    """L'objet a disparu d'un côté : le supprimer de l'autre (``cible``)."""

    lien: Lien
    cible: Cote
    objet: Enreg


@dataclass(frozen=True)
class Cloturer:
    """L'objet a disparu d'un côté : le clore ou l'archiver de l'autre, puis rompre le lien."""

    lien: Lien
    cible: Cote
    objet: Enreg


@dataclass(frozen=True)
class Rompre:
    lien: Lien
    motif: str
    alerter: bool = True


@dataclass(frozen=True)
class Oublier:
    """Les deux objets ont disparu : le lien n'a plus d'objet."""

    lien: Lien


@dataclass(frozen=True)
class Signaler:
    """Rien à écrire, mais un humain doit être prévenu."""

    cle: str
    message: str


Action = Relier | Creer | Synchroniser | Supprimer | Cloturer | Rompre | Oublier | Signaler
