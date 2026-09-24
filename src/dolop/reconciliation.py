"""Moteur de réconciliation à trois états. Aucune entrée-sortie : des états en entrée, des actions en sortie.

Pour chaque objet relié, on compare chaque côté à *son propre* instantané (ce qu'on y a vu ou
écrit en dernier). Un champ changé d'un seul côté est propagé ; changé des deux côtés, c'est un
conflit tranché à l'horodatage. Comparer chaque côté à lui-même rend les conversions imparfaites
(HTML ↔ Markdown…) inoffensives : ce qu'on vient d'écrire ne revient jamais en écho.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .modele import (
    COTES,
    Action,
    Cloturer,
    Conflit,
    Cote,
    Creer,
    EnCours,
    Enreg,
    Intraduisible,
    Lien,
    Oublier,
    Regles,
    Relier,
    Rompre,
    Signaler,
    Supprimer,
    Synchroniser,
    autre,
)
from .traduction import Traducteur

_ABSENT = object()


@dataclass
class Entree:
    regles: Regles
    objets: dict[Cote, dict[str, Enreg]]
    liens: list[Lien]
    traducteur: Traducteur
    # Identifiants reliés dont l'absence a été confirmée (404) : supprimés pour de bon.
    absents: dict[Cote, set[str]] = field(default_factory=lambda: {"dol": set(), "op": set()})
    en_cours: list[EnCours] = field(default_factory=list)
    # Projets gelés (clos, archivés ou supprimés d'un côté) : leurs objets ne bougent plus.
    geles: dict[Cote, set[str]] = field(default_factory=lambda: {"dol": set(), "op": set()})


def reconcilier(e: Entree) -> list[Action]:
    return _Moteur(e).executer()


class _Moteur:
    def __init__(self, e: Entree):
        self.e = e
        self.r = e.regles
        self.actions: list[Action] = []
        connus: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        for lien in e.liens:
            connus["dol"].add(lien.dol_id)
            connus["op"].add(lien.op_id)
        self.libres: dict[Cote, dict[str, Enreg]] = {
            c: {i: o for i, o in e.objets[c].items() if i not in connus[c] and not self.gele(c, o)} for c in COTES
        }
        self.nouveaux: list[Lien] = []

    # ------------------------------------------------------------------------ outils

    def gele(self, cote: Cote, objet: Enreg | Mapping[str, Any] | None) -> bool:
        if self.r.champ_projet is None or objet is None:
            return False
        champs = objet.champs if isinstance(objet, Enreg) else objet
        projet = champs.get(self.r.champ_projet)
        return projet is not None and str(projet) in self.e.geles[cote]

    def meme_identite(self, source: Enreg, cote_source: Cote, candidat: Enreg) -> bool:
        for nom in self.r.identite:
            champ = self.r.champ(nom)
            v_src = source.champs.get(nom)
            try:
                traduite = self.e.traducteur.valeur(self.r, champ, v_src, cote_source)
            except Intraduisible:
                return False
            norm = champ.normaliser or (lambda v: v)
            if norm(traduite) != norm(candidat.champs.get(nom)):
                return False
        return True

    def cle_naturelle(self, objet: Enreg, cote: Cote) -> tuple[Any, ...] | None:
        """Identité exprimée côté Dolibarr, pour comparer des objets des deux côtés."""
        valeurs = []
        for nom in self.r.identite:
            champ = self.r.champ(nom)
            v = objet.champs.get(nom)
            if cote == "op":
                try:
                    v = self.e.traducteur.valeur(self.r, champ, v, "op")
                except Intraduisible:
                    return None
            if champ.normaliser:
                v = champ.normaliser(v)
            if v in (None, ""):
                return None
            valeurs.append(v)
        return tuple(valeurs)

    def apparier(self, dol: Enreg, op: Enreg, motif: str) -> None:
        self.actions.append(Relier(dol.id, op.id, motif))
        self.nouveaux.append(Lien(0, self.r.type, dol.id, op.id))
        del self.libres["dol"][dol.id]
        del self.libres["op"][op.id]

    # ------------------------------------------------------------------------ étapes

    def executer(self) -> list[Action]:
        self.apparier_par_reference()
        self.apparier_creations_interrompues()
        if self.r.apparier:
            self.apparier_par_identite()
        for lien in [*self.e.liens, *self.nouveaux]:
            self.traiter_lien(lien)
        self.planifier_creations()
        return self.actions

    def apparier_par_reference(self) -> None:
        for cote in COTES:
            for objet in list(self.libres[cote].values()):
                if objet.id not in self.libres[cote] or not objet.ref_autre:
                    continue
                jumeau = self.libres[autre(cote)].get(objet.ref_autre)
                # Un objet cloné dans Dolibarr garde l'attribut de l'original : on n'apparie que si
                # le jumeau ne désigne personne d'autre. Sinon, c'est un nouvel objet.
                if jumeau is None or jumeau.ref_autre not in (None, objet.id):
                    continue
                dol, op = (objet, jumeau) if cote == "dol" else (jumeau, objet)
                self.apparier(dol, op, "identifiant embarqué")

    def apparier_creations_interrompues(self) -> None:
        for ec in self.e.en_cours:
            if ec.type != self.r.type:
                continue
            cote_source = autre(ec.cible)
            source = self.libres[cote_source].get(ec.source_id)
            if source is None:
                continue
            candidats = [o for o in self.libres[ec.cible].values() if o.ref_autre == source.id]
            candidats += [
                o
                for o in self.libres[ec.cible].values()
                if o not in candidats and not o.ref_autre and self.meme_identite(source, cote_source, o)
            ]
            if candidats:
                jumeau = candidats[0]
                dol, op = (source, jumeau) if cote_source == "dol" else (jumeau, source)
                self.apparier(dol, op, "reprise d'une création interrompue")

    def apparier_par_identite(self) -> None:
        index: dict[tuple[Any, ...], list[Enreg]] = {}
        for objet in self.libres["op"].values():
            cle = self.cle_naturelle(objet, "op")
            if cle is not None:
                index.setdefault(cle, []).append(objet)
        par_cle_dol: dict[tuple[Any, ...], list[Enreg]] = {}
        for objet in self.libres["dol"].values():
            cle = self.cle_naturelle(objet, "dol")
            if cle is not None:
                par_cle_dol.setdefault(cle, []).append(objet)
        for cle, dols in par_cle_dol.items():
            ops = index.get(cle, [])
            if not ops:
                continue
            if len(dols) == 1 and len(ops) == 1:
                self.apparier(dols[0], ops[0], "même identité")
                continue
            # Deux comptes avec le même e-mail d'un côté : on ne devine pas, on prévient,
            # et on retire ces objets des créations pour ne pas fabriquer un troisième doublon.
            noms = ", ".join(o.libelle or o.id for o in [*dols, *ops])
            self.actions.append(
                Signaler(
                    f"ambigu:{self.r.type}:{cle}",
                    f"{self.r.libelle} : plusieurs correspondances possibles ({noms}). "
                    "Rien n'est relié tant que le doublon existe.",
                )
            )
            for o in dols:
                self.libres["dol"].pop(o.id, None)
            for o in ops:
                self.libres["op"].pop(o.id, None)

    def traiter_lien(self, lien: Lien) -> None:
        if lien.rompu:
            return
        dol = self.e.objets["dol"].get(lien.dol_id)
        op = self.e.objets["op"].get(lien.op_id)
        dol_absent = lien.dol_id in self.e.absents["dol"]
        op_absent = lien.op_id in self.e.absents["op"]
        # Ni lu ni confirmé absent : hors périmètre de ce cycle (projet gelé). On n'y touche pas.
        if (dol is None and not dol_absent) or (op is None and not op_absent):
            return
        if dol is None and op is None:
            self.actions.append(Oublier(lien))
        elif dol is None:
            assert op is not None
            self.disparu(lien, survivant="op", objet=op)
        elif op is None:
            self.disparu(lien, survivant="dol", objet=dol)
        elif not (self.gele("dol", dol) or self.gele("op", op)):
            self.comparer(lien, dol, op)

    def disparu(self, lien: Lien, survivant: Cote, objet: Enreg) -> None:
        if self.gele(survivant, objet):
            return
        if self.r.suppression == "propager":
            self.actions.append(Supprimer(lien, survivant, objet))
        elif self.r.suppression == "cloturer":
            self.actions.append(Cloturer(lien, survivant, objet))
        else:
            motif = f"{self.r.libelle} « {objet.libelle or objet.id} » supprimé côté {_nom(autre(survivant))}"
            self.actions.append(Rompre(lien, motif))

    def comparer(self, lien: Lien, dol: Enreg, op: Enreg) -> None:
        snaps = {"dol": lien.snap_dol, "op": lien.snap_op}
        objets = {"dol": dol, "op": op}
        vers_dol: dict[str, Any] = {}
        vers_op: dict[str, Any] = {}
        conflits: list[Conflit] = []
        signaler_conflits = lien.snap_dol is not None and lien.snap_op is not None

        for champ in self.r.synchronises:
            v = {c: objets[c].champs.get(champ.nom, _ABSENT) for c in COTES}
            if v["dol"] is _ABSENT or v["op"] is _ABSENT:
                continue  # champ non lu d'un côté pendant ce cycle
            change = {c: _change(snaps[c], champ.nom, v[c]) for c in COTES}
            if not (change["dol"] or change["op"]):
                continue
            if self.e.traducteur.egaux(self.r, champ, v["dol"], v["op"]):
                continue  # déjà d'accord (ou modifiés de la même façon des deux côtés)
            if change["dol"] and change["op"]:
                gagnant = self.trancher(dol, op)
                if signaler_conflits:
                    conflits.append(Conflit(champ.nom, gagnant, v["dol"], v["op"]))
                if gagnant == "dol":
                    vers_op[champ.nom] = v["dol"]
                else:
                    vers_dol[champ.nom] = v["op"]
            elif change["dol"]:
                vers_op[champ.nom] = v["dol"]
            else:
                vers_dol[champ.nom] = v["op"]

        a_jour = lien.snap_dol == dict(dol.champs) and lien.snap_op == dict(op.champs)
        if vers_dol or vers_op or not a_jour:
            self.actions.append(Synchroniser(lien, dol, op, vers_dol, vers_op, tuple(conflits)))

    def inactif_a_ignorer(self, objet: Enreg) -> bool:
        return self.r.ignorer_inactifs and _inactif(objet)

    def trancher(self, dol: Enreg, op: Enreg) -> Cote:
        if dol.modifie_le and op.modifie_le and dol.modifie_le != op.modifie_le:
            return "dol" if dol.modifie_le > op.modifie_le else "op"
        return self.r.maitre

    def planifier_creations(self) -> None:
        refs_internes = [c.nom for c in self.r.champs if c.ref == self.r.type]
        for cote in COTES:
            a_creer = [o for o in self.libres[cote].values() if not self.inactif_a_ignorer(o)]
            # Parents avant enfants : une sous-tâche a besoin que sa tâche parente existe déjà.
            ids = {o.id for o in a_creer}
            ordonnes: list[Enreg] = []
            places: set[str] = set()
            restants = a_creer
            while restants:
                prets = [
                    o
                    for o in restants
                    if all(
                        o.champs.get(n) is None or str(o.champs.get(n)) not in ids or str(o.champs.get(n)) in places
                        for n in refs_internes
                    )
                ]
                if not prets:  # cycle de parenté : on crée quand même, la traduction différera
                    prets = restants
                ordonnes.extend(prets)
                places.update(o.id for o in prets)
                restants = [o for o in restants if o.id not in places]
            for objet in ordonnes:
                self.actions.append(Creer(autre(cote), objet))


def _inactif(objet: Enreg) -> bool:
    return objet.champs.get("actif") is False


def _change(snap: Mapping[str, Any] | None, nom: str, valeur: Any) -> bool:
    if snap is None or nom not in snap:
        return True
    return bool(snap[nom] != valeur)


def _nom(cote: Cote) -> str:
    return "Dolibarr" if cote == "dol" else "OpenProject"
