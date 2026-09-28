"""Contrat commun des adaptateurs (un par côté et par type), et le simulateur qui n'écrit rien."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from .modele import Cote, Enreg


class ErreurApi(Exception):
    """Réponse inattendue d'un des deux outils. Le message dit laquelle et pourquoi."""

    def __init__(self, message: str, statut: int | None = None):
        super().__init__(message)
        self.statut = statut


class Adaptateur(Protocol):
    type: str

    def lister(self) -> dict[str, Enreg]:
        """Tous les objets de ce type visibles de ce côté."""
        ...

    def lister_depuis(self, depuis: datetime) -> dict[str, Enreg]:
        """Ceux modifiés depuis ``depuis`` (lecture incrémentale), quitte à en rendre de trop."""
        ...

    def lire(self, identifiant: str) -> Enreg | None:
        """``None`` seulement si l'outil confirme que l'objet n'existe pas (404).

        Toute autre erreur lève ErreurApi : on ne prend jamais un refus d'accès pour une suppression.
        """
        ...

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str: ...

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        """Renvoie un nouvel identifiant si l'objet a dû être recréé (temps déplacé de tâche)."""
        ...

    def supprimer(self, identifiant: str) -> None: ...

    def cloturer(self, identifiant: str) -> None: ...

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        """Embarque l'identifiant du jumeau dans l'objet (sans effet si le type ne le permet pas)."""
        ...

    def verrou(self, identifiant: str) -> str | None:
        """Raison pour laquelle l'objet ne doit pas être modifié depuis l'autre côté (temps facturé)."""
        ...

    def refus_suppression(self, identifiant: str) -> str | None:
        """Raison pour laquelle l'objet ne peut pas être supprimé (tâche qui porte encore du temps)."""
        ...


class Simulateur:
    """Enveloppe un adaptateur : les lectures passent, les écritures sont seulement notées."""

    _compteur = itertools.count(1)

    def __init__(self, reel: Adaptateur, cote: Cote):
        self.reel = reel
        self.type = reel.type
        self.cote = cote
        self.crees: dict[str, dict[str, Any]] = {}
        # (action, détail) de chaque écriture qui aurait été faite.
        self.ecritures: list[tuple[str, str]] = []

    def lister(self) -> dict[str, Enreg]:
        return self.reel.lister()

    def lister_depuis(self, depuis: datetime) -> dict[str, Enreg]:
        return self.reel.lister_depuis(depuis)

    def lire(self, identifiant: str) -> Enreg | None:
        if identifiant in self.crees:
            return Enreg(identifiant, dict(self.crees[identifiant]), libelle="(à créer)")
        return self.reel.lire(identifiant)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        identifiant = f"~{next(self._compteur)}"
        self.crees[identifiant] = dict(champs)
        self.ecritures.append(("créer", f"{identifiant} {dict(champs)}"))
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        if identifiant in self.crees:
            self.crees[identifiant].update(champs)
        self.ecritures.append(("modifier", f"{identifiant} {dict(champs)}"))
        return None

    def supprimer(self, identifiant: str) -> None:
        self.ecritures.append(("supprimer", identifiant))

    def cloturer(self, identifiant: str) -> None:
        self.ecritures.append(("clore", identifiant))

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        if getattr(self.reel, "embarque_ref", True):
            self.ecritures.append(("embarquer", f"{ref_autre} dans {identifiant}"))

    def verrou(self, identifiant: str) -> str | None:
        return None if identifiant in self.crees else self.reel.verrou(identifiant)

    def refus_suppression(self, identifiant: str) -> str | None:
        return None if identifiant in self.crees else self.reel.refus_suppression(identifiant)
