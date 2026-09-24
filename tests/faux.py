"""Faux Dolibarr et faux OpenProject en mémoire, au contrat des vrais adaptateurs."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from dolop.adaptateurs import ErreurApi
from dolop.alertes import Alertes
from dolop.conversions import cle_nom
from dolop.cycle import Commentaire, Contexte, Resultat, executer_cycle, remplacer_bloc
from dolop.entites import ORDRE
from dolop.etat import Etat
from dolop.modele import Cote, Enreg, Intraduisible


class Horloge:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)

    def avancer(self, minutes: int = 2) -> datetime:
        self.t += timedelta(minutes=minutes)
        return self.t


class FauxAdaptateur:
    _ids = itertools.count(100)

    def __init__(self, type_: str, cote: Cote, horloge: Horloge, *, ref_embarquee: bool = True):
        self.type = type_
        self.cote = cote
        self.horloge = horloge
        self.ref_embarquee = ref_embarquee
        self.objets: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, str | None] = {}
        self.maj: dict[str, datetime | None] = {}
        self.verrous: dict[str, str] = {}
        self.refus: dict[str, str] = {}
        self.caches: set[str] = set()  # absents des listes mais lisibles un par un
        self.interdits: set[str] = set()  # lecture directe refusée (403)
        self.stockage: Callable[[dict[str, Any]], dict[str, Any]] = lambda c: c
        self.horodate = True
        self.panne_apres_creation = False
        self.ecritures: list[str] = []

    # ------------------------------------------------------------ préparation des tests

    def ajouter(self, identifiant: str, ref_autre: str | None = None, **champs: Any) -> str:
        self.objets[identifiant] = dict(champs)
        self.refs[identifiant] = ref_autre
        self.maj[identifiant] = self.horloge.t if self.horodate else None
        return identifiant

    def changer(self, identifiant: str, **champs: Any) -> None:
        self.objets[identifiant].update(champs)
        self.maj[identifiant] = self.horloge.t if self.horodate else None

    def seul(self) -> tuple[str, dict[str, Any]]:
        assert len(self.objets) == 1, self.objets
        return next(iter(self.objets.items()))

    # ------------------------------------------------------------------------ contrat

    def _enreg(self, identifiant: str) -> Enreg:
        champs = self.objets[identifiant]
        libelle = str(champs.get("titre") or champs.get("email") or identifiant)
        return Enreg(identifiant, dict(champs), self.maj.get(identifiant), self.refs.get(identifiant), libelle)

    def lister(self) -> dict[str, Enreg]:
        return {i: self._enreg(i) for i in self.objets if i not in self.caches}

    def lire(self, identifiant: str) -> Enreg | None:
        if identifiant in self.interdits:
            raise ErreurApi("403 accès refusé", 403)
        return self._enreg(identifiant) if identifiant in self.objets else None

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        identifiant = f"{self.cote}{next(self._ids)}"
        self.objets[identifiant] = self.stockage(dict(champs))
        self.refs[identifiant] = ref_autre if self.ref_embarquee else None
        self.maj[identifiant] = self.horloge.t if self.horodate else None
        self.ecritures.append(f"créer {identifiant}")
        if self.panne_apres_creation:
            self.panne_apres_creation = False
            raise ErreurApi("connexion coupée juste après la création")
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        self.objets[identifiant].update(self.stockage(dict(champs)))
        self.maj[identifiant] = self.horloge.t if self.horodate else None
        self.ecritures.append(f"modifier {identifiant} {sorted(champs)}")
        return None

    def supprimer(self, identifiant: str) -> None:
        del self.objets[identifiant]
        self.ecritures.append(f"supprimer {identifiant}")

    def cloturer(self, identifiant: str) -> None:
        self.objets[identifiant]["actif"] = False
        self.ecritures.append(f"clore {identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        if self.ref_embarquee:
            self.refs[identifiant] = ref_autre

    def verrou(self, identifiant: str) -> str | None:
        return self.verrous.get(identifiant)

    def refus_suppression(self, identifiant: str) -> str | None:
        return self.refus.get(identifiant)


class FauxCommentaires:
    def __init__(self) -> None:
        self.par_lot: dict[str, list[Commentaire]] = {}
        self.appels = 0

    def commentaires(self, identifiant: str) -> list[Commentaire]:
        self.appels += 1
        return list(self.par_lot.get(identifiant, []))


class FauxNotes:
    def __init__(self) -> None:
        self.notes: dict[str, str] = {}

    def ecrire_bloc(self, identifiant: str, bloc_html: str) -> None:
        self.notes[identifiant] = remplacer_bloc(self.notes.get(identifiant), bloc_html)


class Banc:
    """Les deux faux outils, l'état et les alertes, prêts à enchaîner des cycles."""

    def __init__(self, tiers: list[str] | None = None) -> None:
        self.horloge = Horloge()
        self.dol = {r.type: FauxAdaptateur(r.type, "dol", self.horloge) for r in ORDRE}
        self.op = {r.type: FauxAdaptateur(r.type, "op", self.horloge) for r in ORDRE}
        # Comme en vrai : Dolibarr ne peut pas embarquer l'identifiant OP d'un temps,
        # ni les utilisateurs et membres (appariés par identité).
        for t in ("temps", "utilisateur", "membre"):
            self.dol[t].ref_embarquee = False
        for t in ("utilisateur", "membre"):
            self.op[t].ref_embarquee = False
        self.dol["tache"].horodate = False  # Dolibarr ne date pas les modifications de tâches
        self.tiers = tiers if tiers is not None else ["Acme", "Château Élégance"]
        self.commentaires = FauxCommentaires()
        self.notes = FauxNotes()
        self.etat = Etat.en_memoire()
        self.envoyees: list[tuple[str, str]] = []
        self.alertes = Alertes(self.etat, envoi=lambda t, m: self.envoyees.append((t, m)))

    def client(self, valeur: Any, de: Cote) -> Any:
        if de == "dol" or not valeur:
            return valeur
        for nom in self.tiers:
            if cle_nom(nom) == cle_nom(valeur):
                return nom
        raise Intraduisible(f"client « {valeur} » introuvable dans Dolibarr")

    def contexte(self, **kw: Any) -> Contexte:
        return Contexte(
            adaptateurs={"dol": dict(self.dol), "op": dict(self.op)},  # type: ignore[dict-item]
            convertisseurs={("projet", "client"): self.client},
            commentaires_op=self.commentaires,
            notes_dol=self.notes,
            **kw,
        )

    def cycle(self, mode: str = "une-fois", **kw: Any) -> Resultat:
        confirmer = kw.pop("confirmer_suppressions", False)
        self.horloge.avancer()
        return executer_cycle(self.contexte(**kw), self.etat, self.alertes, mode=mode, confirmer_suppressions=confirmer)

    def ecritures(self) -> list[str]:
        tout = [f"dol {self.dol[t].type} {e}" for t in self.dol for e in self.dol[t].ecritures]
        tout += [f"op {self.op[t].type} {e}" for t in self.op for e in self.op[t].ecritures]
        return tout

    def oublier_ecritures(self) -> None:
        for a in [*self.dol.values(), *self.op.values()]:
            a.ecritures.clear()
        self.envoyees.clear()


def projet(titre: str = "Site Acme", **kw: Any) -> dict[str, Any]:
    return {"titre": titre, "description": "", "actif": True, "client": None, "code": "PJ2501-0003", **kw}


def tache(projet_id: str, titre: str = "Maquette", **kw: Any) -> dict[str, Any]:
    base = {
        "projet": projet_id,
        "parent": None,
        "titre": titre,
        "description": "",
        "debut": None,
        "fin": None,
        "charge": None,
        "avancement": 0,
        "assigne": None,
    }
    return {**base, **kw}


def temps(projet_id: str, tache_id: str | None, utilisateur: str, **kw: Any) -> dict[str, Any]:
    base = {
        "projet": projet_id,
        "tache": tache_id,
        "date": "2026-09-24",
        "duree": 3600,
        "utilisateur": utilisateur,
        "note": "",
    }
    return {**base, **kw}


def utilisateur(email: str, **kw: Any) -> dict[str, Any]:
    return {"email": email, "prenom": "Alice", "nom": "Martin", "actif": True, "login": "alice", **kw}
