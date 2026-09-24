"""Application des actions du moteur, avec les garde-fous qui demandent de lire l'état réel."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .adaptateurs import Adaptateur, ErreurApi
from .alertes import Alertes
from .etat import Etat
from .modele import (
    NOM_COTE,
    Action,
    Cloturer,
    Cote,
    Creer,
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

log = logging.getLogger("dolop.execution")

Echo = Callable[[str], None]


@dataclass
class Bilan:
    relies: int = 0
    crees: int = 0
    modifies: int = 0
    supprimes: int = 0
    clos: int = 0
    rompus: int = 0
    restaures: int = 0
    conflits: int = 0
    differes: int = 0
    erreurs: list[str] = field(default_factory=list)

    def ajouter(self, autre_bilan: Bilan) -> None:
        for nom in (
            "relies",
            "crees",
            "modifies",
            "supprimes",
            "clos",
            "rompus",
            "restaures",
            "conflits",
            "differes",
        ):
            setattr(self, nom, getattr(self, nom) + getattr(autre_bilan, nom))
        self.erreurs.extend(autre_bilan.erreurs)

    def resume(self) -> dict[str, Any]:
        return {
            "reliés": self.relies,
            "créés": self.crees,
            "modifiés": self.modifies,
            "supprimés": self.supprimes,
            "clos": self.clos,
            "rompus": self.rompus,
            "restaurés": self.restaures,
            "conflits": self.conflits,
            "différés": self.differes,
            "erreurs": len(self.erreurs),
        }


class Executeur:
    def __init__(
        self,
        regles: Regles,
        adaptateurs: Mapping[Cote, Adaptateur],
        etat: Etat,
        traducteur: Traducteur,
        alertes: Alertes,
        cycle: int,
        echo: Echo | None = None,
    ):
        self.r = regles
        self.a = adaptateurs
        self.etat = etat
        self.trad = traducteur
        self.alertes = alertes
        self.cycle = cycle
        self.echo = echo or (lambda _m: None)
        self.bilan = Bilan()

    # ----------------------------------------------------------------------- outils

    def _nom(self, objet: Enreg) -> str:
        return f"{self.r.libelle} « {objet.libelle or objet.id} »"

    def _journal(self, action: str, cote: Cote | None, objet_id: str | None, detail: Any = None) -> None:
        self.etat.journaliser(self.cycle, self.r.type, action, cote, objet_id, detail)
        ou = f" [{NOM_COTE[cote]} {objet_id}]" if cote else ""
        self.echo(f"{self.r.type:<11} {action}{ou} {'' if detail is None else detail}")

    def _relire(self, cote: Cote, identifiant: str) -> Enreg:
        objet = self.a[cote].lire(identifiant)
        if objet is None:
            raise ErreurApi(f"{self.r.libelle} {identifiant} introuvable dans {NOM_COTE[cote]} juste après écriture")
        return objet

    def _differer(self, objet: Enreg, cote_source: Cote, erreur: Intraduisible, champ: str) -> None:
        self.bilan.differes += 1
        self.echo(f"{self.r.type:<11} différé : {self._nom(objet)} — {erreur}")
        if erreur.alerter:
            # Même clé à la création et aux cycles suivants : une seule alerte par problème.
            cle = f"valeur:{self.r.type}:{cote_source}:{objet.id}:{champ}"
            self.alertes.lever(cle, f"{self._nom(objet)} ({NOM_COTE[cote_source]}) : {erreur}")

    # ----------------------------------------------------------------------- boucle

    def appliquer(self, actions: Iterable[Action]) -> Bilan:
        for action in actions:
            try:
                self._appliquer(action)
            except Exception as e:  # un objet en échec ne bloque pas les autres
                message = f"{self.r.libelle} — {type(action).__name__} : {e}"
                log.exception(message)
                self.bilan.erreurs.append(message)
                self.alertes.lever(f"erreur:{self.r.type}:{_cle_action(action)}", message)
        return self.bilan

    def _appliquer(self, action: Action) -> None:
        match action:
            case Relier():
                self._relier(action)
            case Creer():
                self._creer(action)
            case Synchroniser():
                self._synchroniser(action)
            case Supprimer():
                self._supprimer(action)
            case Cloturer():
                self._cloturer(action)
            case Rompre():
                self.etat.rompre(action.lien.id)
                self.trad.index.retirer(self.r.type, action.lien.dol_id, action.lien.op_id)
                self.bilan.rompus += 1
                self._journal("rompre", None, None, action.motif)
                if action.alerter:
                    self.alertes.lever(f"rompu:{self.r.type}:{action.lien.id}", action.motif)
            case Oublier():
                self.etat.supprimer_lien(action.lien.id)
                self.trad.index.retirer(self.r.type, action.lien.dol_id, action.lien.op_id)
            case Signaler():
                self.alertes.lever(action.cle, action.message)

    # ---------------------------------------------------------------------- actions

    def _relier(self, action: Relier) -> None:
        self.etat.creer_lien(self.r.type, action.dol_id, action.op_id)
        self.trad.index.ajouter(self.r.type, action.dol_id, action.op_id)
        # Si l'un des deux venait d'une création interrompue, elle est désormais aboutie.
        self.etat.retirer_en_cours(self.r.type, "op", action.dol_id)
        self.etat.retirer_en_cours(self.r.type, "dol", action.op_id)
        self.bilan.relies += 1
        self._journal("relier", None, None, f"Dolibarr {action.dol_id} ↔ OpenProject {action.op_id} ({action.motif})")

    def _valeurs(self, objet: Enreg) -> dict[str, Any]:
        return {c.nom: objet.champs[c.nom] for c in self.r.champs if c.nom in objet.champs}

    def _creer(self, action: Creer) -> None:
        cible, source = action.cible, action.source
        cote_source = autre(cible)
        traduits, differes = self.trad.partiel(self.r, self._valeurs(source), cote_source)
        attente = {n: e for n, e in differes.items() if not e.alerter}
        if attente:
            # Une référence pas encore reliée (le projet d'une tâche…) : on crée au prochain cycle.
            nom, erreur = next(iter(attente.items()))
            self._differer(source, cote_source, erreur, nom)
            return
        for nom, erreur in differes.items():
            # Valeur impossible à reprendre (client introuvable…) : on crée sans elle et on prévient.
            traduits[nom] = None
            self._differer(source, cote_source, erreur, nom)
        self.etat.noter_en_cours(self.r.type, cible, source.id)
        nouvel = self.a[cible].creer(traduits, ref_autre=source.id)
        self._journal("créer", cible, nouvel, {"source": source.id, "champs": traduits})
        self.a[cote_source].poser_ref_autre(source.id, nouvel)
        relu = self._relire(cible, nouvel)
        dol_id, op_id = (source.id, nouvel) if cote_source == "dol" else (nouvel, source.id)
        # Les champs écartés restent hors de l'instantané : ils seront retentés à chaque cycle.
        snap_source = {k: v for k, v in source.champs.items() if k not in differes}
        snaps = {cote_source: snap_source, cible: dict(relu.champs)}
        self.etat.creer_lien(self.r.type, dol_id, op_id, snaps["dol"], snaps["op"])
        self.etat.retirer_en_cours(self.r.type, cible, source.id)
        self.trad.index.ajouter(self.r.type, dol_id, op_id)
        self.bilan.crees += 1

    def _lien_reel(self, lien: Lien) -> Lien:
        if lien.id:
            return lien
        reel = self.etat.lien_par_paire(self.r.type, lien.dol_id, lien.op_id)
        if reel is None:
            raise ErreurApi(f"lien {lien.dol_id} ↔ {lien.op_id} absent de la base d'état")
        return reel

    def _synchroniser(self, action: Synchroniser) -> None:
        lien = self._lien_reel(action.lien)
        objets: dict[Cote, Enreg] = {"dol": action.dol, "op": action.op}
        snaps: dict[Cote, dict[str, Any]] = {"dol": dict(action.dol.champs), "op": dict(action.op.champs)}

        sens: tuple[tuple[Cote, Mapping[str, Any]], ...] = (("op", action.vers_op), ("dol", action.vers_dol))
        for cible, valeurs in sens:
            if not valeurs:
                continue
            source: Cote = autre(cible)
            traduits, differes = self.trad.partiel(self.r, valeurs, source)
            ancien = lien.snap_de(source)
            for nom, erreur in differes.items():
                # On garde l'ancien instantané : le changement sera revu au prochain cycle.
                if ancien is not None and nom in ancien:
                    snaps[source][nom] = ancien[nom]
                else:
                    snaps[source].pop(nom, None)
                self._differer(objets[source], source, erreur, nom)
            if not traduits:
                continue

            verrou = self.a[cible].verrou(objets[cible].id)
            if verrou:
                snaps[source] = self._restaurer(lien, objets, cible, list(traduits), verrou)
                continue

            nouvel = self.a[cible].modifier(objets[cible].id, traduits)
            identifiant = objets[cible].id
            if nouvel and nouvel != identifiant:
                self.etat.changer_id(lien.id, cible, nouvel)
                self.trad.index.remplacer(self.r.type, cible, identifiant, nouvel)
                identifiant = nouvel
            snaps[cible] = dict(self._relire(cible, identifiant).champs)
            avant = {k: objets[cible].champs.get(k) for k in traduits}
            self._journal("modifier", cible, identifiant, {"avant": avant, "après": traduits})
            self.bilan.modifies += 1

        for conflit in action.conflits:
            self.bilan.conflits += 1
            garde, ecarte = (
                (conflit.valeur_dol, conflit.valeur_op)
                if conflit.gagnant == "dol"
                else (conflit.valeur_op, conflit.valeur_dol)
            )
            message = (
                f"{self._nom(action.dol)} : « {conflit.champ} » modifié des deux côtés. "
                f"Gardé ({NOM_COTE[conflit.gagnant]}) : {garde!r}. Écarté : {ecarte!r}."
            )
            self._journal("conflit", conflit.gagnant, None, message)
            self.alertes.lever(f"conflit:{self.r.type}:{lien.id}:{conflit.champ}", message)

        self.etat.maj_instantanes(lien.id, snaps["dol"], snaps["op"])

    def _restaurer(
        self, lien: Lien, objets: Mapping[Cote, Enreg], cote_verrou: Cote, champs: list[str], raison: str
    ) -> dict[str, Any]:
        """L'objet est verrouillé côté ``cote_verrou`` : on y remet ses valeurs de l'autre côté."""
        source = autre(cote_verrou)
        valeurs = {nom: objets[cote_verrou].champs[nom] for nom in champs if nom in objets[cote_verrou].champs}
        retour, _ = self.trad.partiel(self.r, valeurs, cote_verrou)
        snap = dict(objets[source].champs)
        if retour:
            self.a[source].modifier(objets[source].id, retour)
            snap = dict(self._relire(source, objets[source].id).champs)
            self._journal("restaurer", source, objets[source].id, retour)
        self.bilan.restaures += 1
        self.alertes.lever(
            f"verrou:{self.r.type}:{lien.id}",
            f"{self._nom(objets[cote_verrou])} : {raison}. "
            f"La modification faite dans {NOM_COTE[source]} a été annulée.",
        )
        return snap

    def _supprimer(self, action: Supprimer) -> None:
        lien, cible, objet = action.lien, action.cible, action.objet
        disparu: Cote = autre(cible)

        verrou = self.a[cible].verrou(objet.id)
        if verrou:
            # Temps facturé supprimé dans OpenProject : Dolibarr fait foi, on le recrée.
            traduits = self.trad.champs(self.r, self._valeurs(objet), cible)
            nouvel = self.a[disparu].creer(traduits, ref_autre=objet.id)
            # Le lien pointe tout de suite sur le nouvel objet : un arrêt brutal juste après
            # ne doit pas conduire à le recréer une seconde fois.
            self.etat.changer_id(lien.id, disparu, nouvel)
            self.trad.index.remplacer(self.r.type, disparu, lien.id_de(disparu), nouvel)
            self._journal("recréer", disparu, nouvel, {"source": objet.id, "champs": traduits})
            self.a[cible].poser_ref_autre(objet.id, nouvel)
            relu = self._relire(disparu, nouvel)
            snaps = {cible: dict(objet.champs), disparu: dict(relu.champs)}
            self.etat.maj_instantanes(lien.id, snaps["dol"], snaps["op"])
            self.bilan.restaures += 1
            self.alertes.lever(
                f"verrou:{self.r.type}:{lien.id}",
                f"{self._nom(objet)} : {verrou}. Supprimé dans {NOM_COTE[disparu]}, il y a été recréé.",
            )
            return

        refus = self.a[cible].refus_suppression(objet.id)
        if refus:
            self.etat.rompre(lien.id)
            self.trad.index.retirer(self.r.type, lien.dol_id, lien.op_id)
            self.bilan.rompus += 1
            message = (
                f"{self._nom(objet)} a été supprimé dans {NOM_COTE[disparu]} mais reste dans "
                f"{NOM_COTE[cible]} : {refus}. Les deux ne sont plus synchronisés."
            )
            self._journal("rompre", cible, objet.id, message)
            self.alertes.lever(f"rompu:{self.r.type}:{lien.id}", message)
            return

        self.a[cible].supprimer(objet.id)
        self._journal("supprimer", cible, objet.id, {"champs": dict(objet.champs)})
        self.etat.supprimer_lien(lien.id)
        self.trad.index.retirer(self.r.type, lien.dol_id, lien.op_id)
        self.bilan.supprimes += 1

    def _cloturer(self, action: Cloturer) -> None:
        lien, cible, objet = action.lien, action.cible, action.objet
        self.a[cible].cloturer(objet.id)
        self.etat.rompre(lien.id)
        self.trad.index.retirer(self.r.type, lien.dol_id, lien.op_id)
        self.bilan.clos += 1
        verbe = "clos" if cible == "dol" else "archivé"
        message = (
            f"{self._nom(objet)} a été supprimé dans {NOM_COTE[autre(cible)]} : "
            f"il est {verbe} dans {NOM_COTE[cible]}, rien n'y est effacé."
        )
        self._journal("clore", cible, objet.id, message)
        self.alertes.lever(f"clos:{self.r.type}:{lien.id}", message)


def _cle_action(action: Action) -> str:
    match action:
        case Creer():
            return f"creer:{action.source.id}"
        case Relier():
            return f"relier:{action.dol_id}:{action.op_id}"
        case Signaler():
            return action.cle
        case _:
            return f"lien:{action.lien.id}:{action.lien.dol_id}:{action.lien.op_id}"
