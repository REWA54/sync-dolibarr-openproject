"""Un cycle de synchronisation, de la lecture des deux outils à l'écriture des écarts.

Déroulé :
  A. lire tout, des deux côtés ; confirmer une à une (accès direct → 404) les absences. Entre deux
     lectures complètes, le service ne lit que les tâches et les temps modifiés (lecture
     incrémentale) : le côté resté intact d'un objet relié est connu par son instantané, et les
     suppressions attendent la lecture complète suivante, seule à pouvoir les constater ;
  B. disjoncteur : trop de suppressions prévues → on s'arrête avant toute écriture ;
  C. créations et modifications, type par type, de haut en bas (utilisateurs → temps) ;
  D. suppressions, de bas en haut (temps → projets) ;
  E. commentaires OpenProject → note de la tâche Dolibarr.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .adaptateurs import Adaptateur, ErreurApi, Simulateur
from .alertes import Alertes
from .conversions import maintenant, markdown_vers_html
from .entites import MEMBRE, ORDRE, PROJET, TACHE, TEMPS, UTILISATEUR
from .etat import Etat
from .execution import Bilan, Echo, Executeur
from .modele import COTES, NOM_COTE, Cloturer, Cote, Creer, Enreg, Lien, Regles, Supprimer, autre
from .reconciliation import Entree, reconcilier
from .traduction import Convertisseur, IndexLiens, Traducteur

log = logging.getLogger("dolop.cycle")

MARQUEUR_COMMENTAIRES = "⸻ Commentaires OpenProject — copie automatique, ne pas modifier ici ⸻"


@dataclass(frozen=True)
class Commentaire:
    date: datetime
    auteur: str
    texte: str


class SourceCommentaires(Protocol):
    def commentaires(self, identifiant: str) -> list[Commentaire]: ...


class CibleNotes(Protocol):
    def ecrire_bloc(self, identifiant: str, bloc_html: str) -> None: ...


@dataclass
class Contexte:
    adaptateurs: dict[Cote, dict[str, Adaptateur]]
    convertisseurs: Mapping[tuple[str, str], Convertisseur] = field(default_factory=dict)
    commentaires_op: SourceCommentaires | None = None
    notes_dol: CibleNotes | None = None
    # Reçoit True avant une lecture complète, False avant une lecture incrémentale.
    avant_cycle: Callable[[bool], None] = lambda _complet: None
    # Reçoit la fonction « signe de vie » à appeler à chaque réponse HTTP (None en fin de cycle).
    brancher_pouls: Callable[[Callable[[], None] | None], None] = lambda _pouls: None
    seuil_suppressions: int = 5
    seuil_pourcent: int = 20
    # Seuil total de suppressions : le plus grand de seuil_suppressions et de ce % des objets reliés.
    seuil_suppressions_pourcent: int = 1
    seuil_creations: int = 50
    fuseau: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Paris"))
    # Périmètre choisi : un utilisateur n'est recopié que s'il est concerné par un projet du périmètre.
    perimetre_choisi: bool = False
    # Temps antérieurs : hors périmètre (ni lus, ni cherchés un par un, ni supprimés).
    temps_depuis: date | None = None
    # Lecture complète au plus tous les N minutes en service ; entre deux, lecture incrémentale. 0 : toujours complète.
    lecture_complete_minutes: int = 0
    # Horloge des dates « modifié depuis » (remplaçable dans les tests).
    horloge: Callable[[], datetime] = maintenant


@dataclass(frozen=True)
class Ecriture:
    """Une écriture qu'un cycle simulé aurait faite."""

    outil: str
    type: str
    action: str
    detail: str

    def __str__(self) -> str:
        return f"{self.outil:<11} {self.type:<11} {self.action} {self.detail}"


@dataclass
class Resultat:
    cycle: int
    statut: str  # « réussi », « bloqué » (disjoncteur) ou « échec »
    bilan: Bilan
    message: str = ""
    plan: list[Ecriture] = field(default_factory=list)
    lecture: str = "complète"  # ou « incrémentale »

    @property
    def ecritures(self) -> list[str]:
        return [str(e) for e in self.plan]


class Disjoncteur(Exception):
    pass


class _NotesSimulees:
    """En simulation, la copie des commentaires est seulement notée."""

    def __init__(self) -> None:
        self.ecritures: list[Ecriture] = []

    def ecrire_bloc(self, identifiant: str, bloc_html: str) -> None:
        self.ecritures.append(Ecriture("Dolibarr", "commentaires", "copier", f"dans la note de la tâche {identifiant}"))


class _Pouls:
    """Signe de vie d'un cycle en cours, pour que « dolop sante » distingue un long cycle qui avance
    (premier chargement d'un outil rempli) d'un service bloqué. Écrit au plus toutes les 30 s, et
    seulement depuis le fil du cycle : la base d'état ne se partage pas entre fils."""

    INTERVALLE = 30.0

    def __init__(self, etat: Etat, cycle: int) -> None:
        self.etat = etat
        self.fil = threading.get_ident()
        self.info = {"cycle": cycle, "debut": maintenant().isoformat(timespec="seconds")}
        self.dernier = float("-inf")
        self()

    def __call__(self) -> None:
        if threading.get_ident() != self.fil or time.monotonic() - self.dernier < self.INTERVALLE:
            return
        self.dernier = time.monotonic()
        info = {**self.info, "battement": maintenant().isoformat(timespec="seconds")}
        with contextlib.suppress(sqlite3.Error):  # un signe de vie manqué ne doit pas faire échouer le cycle
            self.etat.poser_meta("cycle_en_cours", json.dumps(info))


def executer_cycle(
    ctx: Contexte,
    etat: Etat,
    alertes: Alertes,
    *,
    mode: str,
    confirmer_suppressions: bool = False,
    confirmer_creations: bool = False,
    forcer_lecture_complete: bool = False,
    echo: Echo | None = None,
) -> Resultat:
    ecrire = mode != "simuler"
    adaptateurs: dict[Cote, dict[str, Adaptateur]] = ctx.adaptateurs
    simulateurs: list[Simulateur] = []
    notes: CibleNotes | None = ctx.notes_dol
    notes_simulees = _NotesSimulees()
    if not ecrire:
        notes = notes_simulees
        etat = etat.copie_en_memoire()
        alertes = Alertes(etat, envoi=None)
        adaptateurs = {}
        for cote in COTES:
            adaptateurs[cote] = {}
            for type_, reel in ctx.adaptateurs[cote].items():
                sim = Simulateur(reel, cote)
                simulateurs.append(sim)
                adaptateurs[cote][type_] = sim

    debut = ctx.horloge()
    depuis = None if forcer_lecture_complete else _depuis(ctx, etat, mode, debut)
    cycle = etat.debuter_cycle(mode)
    bilan = Bilan()
    statut, message = "réussi", ""
    pouls: Callable[[], None] = _Pouls(etat, cycle) if ecrire else (lambda: None)
    ctx.brancher_pouls(pouls)
    deroule = _Cycle(ctx, adaptateurs, notes, etat, alertes, cycle, bilan, echo, confirmer_suppressions, pouls)
    deroule.confirmer_creations = confirmer_creations
    deroule.simulation = not ecrire
    deroule.depuis = depuis
    try:
        ctx.avant_cycle(depuis is None)
        deroule.derouler()
        if deroule.blocage:  # simulation : le plan complet est montré, le cycle reste « bloqué »
            raise Disjoncteur(deroule.blocage)
    except Disjoncteur as e:
        statut, message = "bloqué", str(e)
        alertes.lever("disjoncteur", message, permanente=True)
    except ErreurApi as e:  # panne ou refus d'un des outils : le message suffit
        statut, message = "échec", str(e)
        log.error("cycle %s en échec : %s", cycle, message)
    except Exception as e:  # défaut inattendu : on garde la trace complète
        statut, message = "échec", f"{type(e).__name__} : {e}"
        log.exception("cycle %s en échec", cycle)
    ctx.brancher_pouls(None)
    etat.terminer_cycle(cycle, statut, {**bilan.resume(), "message": message})
    if ecrire:
        etat.effacer_meta("cycle_en_cours")

    if statut == "réussi":
        alertes.retablir("disjoncteur", "Le disjoncteur est refermé : la synchronisation a repris.")
        if depuis is None:
            alertes.fin_de_cycle()
        else:
            # Un cycle incrémental ne revoit pas tout : il ne peut pas dire qu'un problème a disparu.
            alertes.levees.clear()
        if ecrire:
            etat.poser_meta("dernier_succes", maintenant().isoformat(timespec="seconds"))
            etat.poser_meta("dernier_debut_reussi", debut.isoformat(timespec="seconds"))
            if depuis is None:
                etat.poser_meta("derniere_lecture_complete", debut.isoformat(timespec="seconds"))
    if mode == "service":
        if statut == "échec":
            n = etat.echecs_consecutifs()
            if n >= 3:
                alertes.lever("echec", f"{n} cycles en échec de suite. Dernière erreur : {message}", permanente=True)
        elif statut == "réussi":
            alertes.retablir("echec", "La synchronisation fonctionne à nouveau.")

    plan = [Ecriture(NOM_COTE[s.cote], s.type, action, detail) for s in simulateurs for action, detail in s.ecritures]
    plan += notes_simulees.ecritures
    if not ecrire:
        etat.fermer()  # la copie de simulation
    return Resultat(cycle, statut, bilan, message, plan, "complète" if depuis is None else "incrémentale")


def _depuis(ctx: Contexte, etat: Etat, mode: str, debut: datetime) -> datetime | None:
    """Début du dernier cycle réussi, si ce cycle peut se contenter de lire ce qui a changé depuis.

    Lecture complète : hors service (simulation et cycle manuel voient tout), au premier cycle, et au
    plus tous les LECTURE_COMPLETE_MINUTES. Partir du *début* du dernier cycle réussi, et non de sa
    fin, couvre ce qui a été modifié pendant qu'il tournait.
    """
    if mode != "service" or ctx.lecture_complete_minutes <= 0:
        return None
    complete, dernier = etat.meta("derniere_lecture_complete"), etat.meta("dernier_debut_reussi")
    if complete is None or dernier is None:
        return None
    if debut - datetime.fromisoformat(complete) >= timedelta(minutes=ctx.lecture_complete_minutes):
        return None
    return datetime.fromisoformat(dernier)


def lire_les_deux(ctx: Contexte, etat: Etat) -> tuple[dict[str, dict[Cote, dict[str, Enreg]]], dict[Cote, set[str]]]:
    """Tout lire des deux côtés, comme un cycle complet, sans rien écrire (pour « dolop apparier »)."""
    lecture = _Cycle(ctx, ctx.adaptateurs, None, etat, Alertes(etat, envoi=None), 0, Bilan(), None, False)
    lecture.simulation = True
    ctx.avant_cycle(True)
    lecture.lire()
    return lecture.lus, lecture.geles


class _Cycle:
    def __init__(
        self,
        ctx: Contexte,
        adaptateurs: dict[Cote, dict[str, Adaptateur]],
        notes: CibleNotes | None,
        etat: Etat,
        alertes: Alertes,
        cycle: int,
        bilan: Bilan,
        echo: Echo | None,
        confirmer_suppressions: bool,
        pouls: Callable[[], None] = lambda: None,
    ):
        self.ctx = ctx
        self.pouls = pouls
        self.notes = notes
        self.a = adaptateurs
        self.etat = etat
        self.alertes = alertes
        self.cycle = cycle
        self.bilan = bilan
        self.echo = echo
        self.confirmer = confirmer_suppressions
        self.confirmer_creations = False
        # En simulation, le disjoncteur n'arrête pas le déroulé : on veut voir tout ce qui serait fait.
        self.simulation = False
        self.blocage = ""
        # Lecture incrémentale : début du dernier cycle réussi (None : lecture complète).
        self.depuis: datetime | None = None
        # Objets d'un lien complétés par leur instantané, faute d'avoir été modifiés (lecture incrémentale).
        self.completes: dict[str, dict[Cote, set[str]]] = {r.type: {"dol": set(), "op": set()} for r in ORDRE}
        self.lus: dict[str, dict[Cote, dict[str, Enreg]]] = {}
        self.absents: dict[str, dict[Cote, set[str]]] = {}
        self.geles: dict[Cote, set[str]] = {"dol": set(), "op": set()}

    def adaptateurs_de(self, type_: str) -> dict[Cote, Adaptateur]:
        return {c: self.a[c][type_] for c in COTES}

    # ------------------------------------------------------------------------- A

    def lire(self) -> None:
        for r in ORDRE:
            liens = self.etat.liens(r.type)
            actifs = [lien for lien in liens if not lien.rompu]
            if self.depuis is not None and r is MEMBRE:
                # Dolibarr ne donne les membres que projet par projet : lus à la lecture complète seulement.
                self.lus[r.type], self.absents[r.type] = {"dol": {}, "op": {}}, {"dol": set(), "op": set()}
                continue
            if self.depuis is not None and r in (TACHE, TEMPS):
                modifies = self.lire_modifies(r, actifs, self.depuis)
                if modifies is not None:
                    self.lus[r.type], self.absents[r.type] = modifies, {"dol": set(), "op": set()}
                    self.pouls()
                    continue
            lus = {c: self.a[c][r.type].lister() for c in COTES}
            for c in COTES:
                # Des projets ou des utilisateurs qui disparaissent tous d'un coup : c'est presque
                # toujours un compte technique qui a perdu ses droits. OpenProject répond alors 404
                # (et non 403) sur ce qu'il ne montre plus, la confirmation seule ne suffirait pas.
                structurel = r.type in (PROJET.type, "utilisateur")
                if structurel and len(actifs) >= 2 and not lus[c] and not self.confirmer:
                    raise Disjoncteur(
                        f"{NOM_COTE[c]} ne renvoie aucun objet « {r.libelle} » alors que {len(actifs)} sont reliés. "
                        "Droits du compte technique ou panne de l'API ? Rien n'a été écrit."
                    )
            self.lus[r.type] = lus
            self.pouls()
            self.absents[r.type] = self.confirmer_absences(r, lus, actifs)
            if r is PROJET:
                self.geles = self.calculer_geles(liens, lus)

    def lire_modifies(self, r: Regles, liens: list[Lien], depuis: datetime) -> dict[Cote, dict[str, Enreg]] | None:
        """Lecture incrémentale d'un type ; None si un outil refuse le filtre (on lit alors tout)."""
        try:
            lus = {c: self.a[c][r.type].lister_depuis(depuis) for c in COTES}
        except ErreurApi as e:
            if e.statut != 400:
                raise
            log.warning("%s : filtre « modifié depuis » refusé, lecture complète de ce type (%s)", r.libelle, e)
            return None
        for lien in liens:
            for c in COTES:
                ici, la_bas = lien.id_de(c), lien.id_de(autre(c))
                if ici not in lus[c] or la_bas in lus[autre(c)]:
                    continue
                # Modifié d'un seul côté : l'autre n'a pas bougé depuis ce qu'on y a vu ou écrit en dernier.
                snap = lien.snap_de(autre(c))
                if snap is not None:
                    lus[autre(c)][la_bas] = Enreg(la_bas, dict(snap), ref_autre=ici, libelle=f"{r.libelle} {la_bas}")
                    self.completes[r.type][autre(c)].add(la_bas)
                elif (objet := self.a[autre(c)][r.type].lire(la_bas)) is not None:
                    lus[autre(c)][la_bas] = objet  # lien sans instantané (tout juste apparié) : on le lit
        # Un objet non relié qui désigne son jumeau (identifiant embarqué) : sans lire ce jumeau, resté
        # intact donc absent de la liste, l'objet serait recopié en double.
        connus = {c: {lien.id_de(c) for lien in liens} for c in COTES}
        for c in COTES:
            for objet in list(lus[c].values()):
                ref = objet.ref_autre
                if ref and objet.id not in connus[c] and ref not in lus[autre(c)] and ref not in connus[autre(c)]:
                    jumeau = self.a[autre(c)][r.type].lire(ref)
                    if jumeau is not None:
                        lus[autre(c)][jumeau.id] = jumeau
        return lus

    def confirmer_absences(
        self, r: Regles, lus: dict[Cote, dict[str, Enreg]], liens: list[Lien]
    ) -> dict[Cote, set[str]]:
        absents: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        for lien in liens:
            for c in COTES:
                identifiant = lien.id_de(c)
                if identifiant in lus[c]:
                    continue
                snap = lien.snap_de(c)
                if r.champ_projet and snap is not None and str(snap.get(r.champ_projet)) in self.geles[c]:
                    continue  # objet d'un projet gelé : on ne le cherche pas, on n'y touche pas
                if r is TEMPS and self.anterieur(snap):
                    continue  # temps d'avant TEMPS_DEPUIS : hors périmètre, on n'y touche plus
                objet = self.a[c][r.type].lire(identifiant)
                if objet is None:
                    absents[c].add(identifiant)
                else:
                    lus[c][identifiant] = objet
        return absents

    def anterieur(self, snap: Mapping[str, Any] | None) -> bool:
        depuis = self.ctx.temps_depuis
        return depuis is not None and snap is not None and str(snap.get("date") or "") < depuis.isoformat()

    def calculer_geles(self, liens: list[Lien], lus: dict[Cote, dict[str, Enreg]]) -> dict[Cote, set[str]]:
        """Projets dont les objets ne bougent plus : clos ou archivés, supprimés d'un côté, rompus, ou
        hors du périmètre choisi (ni coché d'un côté ni de l'autre)."""
        geles: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        relies: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        for lien in liens:
            relies["dol"].add(lien.dol_id)
            relies["op"].add(lien.op_id)
            dol, op = lus["dol"].get(lien.dol_id), lus["op"].get(lien.op_id)
            if (
                lien.rompu
                or dol is None
                or op is None
                or not dol.champs.get("actif", True)
                or not op.champs.get("actif", True)
                or not (dol.perimetre or op.perimetre)
            ):
                geles["dol"].add(lien.dol_id)
                geles["op"].add(lien.op_id)
        for c in COTES:
            geles[c].update(
                i
                for i, p in lus[c].items()
                if not p.champs.get("actif", True) or (not p.perimetre and i not in relies[c])
            )
        return geles

    def utilisateurs_concernes(self) -> dict[Cote, set[str]]:
        """Utilisateurs membres, assignés ou auteurs de temps dans un projet non gelé, de leur côté."""
        concernes: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        for type_, champ in (("membre", "utilisateur"), ("tache", "assigne"), ("temps", "utilisateur")):
            for c in COTES:
                for objet in self.lus.get(type_, {}).get(c, {}).values():
                    projet, qui = objet.champs.get("projet"), objet.champs.get(champ)
                    if projet is not None and qui and str(projet) not in self.geles[c]:
                        concernes[c].add(str(qui))
        return concernes

    # ------------------------------------------------------------------------- B

    def entree(self, r: Regles, traducteur: Traducteur) -> Entree:
        creables = None
        if r is UTILISATEUR and self.ctx.perimetre_choisi:
            creables = self.utilisateurs_concernes()
        return Entree(
            regles=r,
            objets=self.lus[r.type],
            liens=self.etat.liens(r.type),
            traducteur=traducteur,
            absents=self.absents[r.type],
            en_cours=self.etat.en_cours(),
            geles=self.geles,
            creables=creables,
        )

    def bloquer(self, message: str) -> None:
        if not self.simulation:
            raise Disjoncteur(message)
        self.blocage = self.blocage or message

    def verifier_disjoncteur(self, traducteur: Traducteur) -> None:
        total = creations = relies_total = 0
        details: list[str] = []
        details_creations: list[str] = []
        for r in ORDRE:
            actions = reconcilier(self.entree(r, traducteur))
            relies = self.etat.compter_liens(r.type)
            relies_total += relies
            c = sum(isinstance(a, Creer) for a in actions)
            if c:
                creations += c
                details_creations.append(f"{c} × {r.libelle}")
            n = sum(isinstance(a, Supprimer | Cloturer) for a in actions)
            if not n:
                continue
            total += n
            details.append(f"{n} × {r.libelle}")
            if relies >= 10 and n * 100 > relies * self.ctx.seuil_pourcent and not self.confirmer:
                self.bloquer(
                    f"{n} suppressions de « {r.libelle} » prévues sur {relies} reliés "
                    f"(plus de {self.ctx.seuil_pourcent} %). Rien n'a été écrit. "
                    "Vérifier avec « dolop simuler », puis lancer « dolop une-fois --confirmer-suppressions »."
                )
        # Proportionnel : 5 suppressions en un cycle, c'est beaucoup pour 30 objets reliés, peu pour 30 000.
        seuil = max(self.ctx.seuil_suppressions, math.ceil(relies_total * self.ctx.seuil_suppressions_pourcent / 100))
        if total > seuil and not self.confirmer:
            self.bloquer(
                f"{total} suppressions ou clôtures prévues ({', '.join(details)}), "
                f"au-delà du seuil de {seuil}. Rien n'a été écrit. "
                "Vérifier avec « dolop simuler », puis lancer « dolop une-fois --confirmer-suppressions »."
            )
        # Premier contact avec un outil rempli, périmètre mal réglé : tout serait recopié d'un coup.
        if creations > self.ctx.seuil_creations and not self.confirmer_creations:
            self.bloquer(
                f"{creations} créations prévues ({', '.join(details_creations)}), "
                f"au-delà du seuil de {self.ctx.seuil_creations}. Rien n'a été écrit. "
                "Vérifier avec « dolop simuler --export simulation.csv » (et « dolop apparier » si les deux "
                "outils contiennent déjà les mêmes projets), puis lancer « dolop une-fois --confirmer-creations »."
            )

    # ------------------------------------------------------------------------- C, D, E

    def derouler(self) -> None:
        self.lire()
        index = IndexLiens()
        index.charger(self.etat.paires())
        traducteur = Traducteur(index, self.ctx.convertisseurs)
        self.verifier_disjoncteur(traducteur)

        suppressions: list[tuple[Regles, list[Any]]] = []
        for r in ORDRE:
            actions = reconcilier(self.entree(r, traducteur))
            immediates = [a for a in actions if not isinstance(a, Supprimer | Cloturer)]
            suppressions.append((r, [a for a in actions if isinstance(a, Supprimer | Cloturer)]))
            self.executeur(r, traducteur).appliquer(immediates)
        for r, actions in reversed(suppressions):
            if actions:
                self.executeur(r, traducteur).appliquer(actions)

        self.synchroniser_commentaires()

    def executeur(self, r: Regles, traducteur: Traducteur) -> _ExecuteurCumule:
        executeur = _ExecuteurCumule(
            self.bilan,
            r,
            self.adaptateurs_de(r.type),
            self.etat,
            traducteur,
            self.alertes,
            self.cycle,
            self.echo,
        )
        executeur.pouls = self.pouls
        return executeur

    def synchroniser_commentaires(self) -> None:
        source, cible = self.ctx.commentaires_op, self.notes
        if source is None or cible is None:
            return
        lus = self.lus[TACHE.type]
        for lien in self.etat.liens(TACHE.type):
            dol, op = lus["dol"].get(lien.dol_id), lus["op"].get(lien.op_id)
            if lien.rompu or dol is None or op is None:
                continue
            if str(dol.champs.get("projet")) in self.geles["dol"] or lien.op_id in self.completes[TACHE.type]["op"]:
                continue  # projet gelé, ou lot resté intact (un commentaire date le lot)
            maj = op.modifie_le.isoformat() if op.modifie_le else None
            vu, empreinte = self.etat.commentaires(lien.op_id)
            if maj is not None and maj == vu:
                continue
            try:
                commentaires = source.commentaires(lien.op_id)
                bloc = rendre_commentaires(commentaires, self.ctx.fuseau)
                nouvelle = hashlib.sha1(bloc.encode(), usedforsecurity=False).hexdigest() if commentaires else None
                if nouvelle != empreinte:
                    cible.ecrire_bloc(lien.dol_id, bloc)
                    self.etat.journaliser(
                        self.cycle,
                        "commentaires",
                        "copier",
                        "dol",
                        lien.dol_id,
                        {"nombre": len(commentaires)},
                    )
                    if self.echo:
                        self.echo(
                            f"{'commentaires':<11} copier [Dolibarr {lien.dol_id}] {len(commentaires)} commentaire(s)"
                        )
                self.etat.noter_commentaires(lien.op_id, maj, nouvelle)
            except Exception as e:
                message = f"Commentaires de « {op.libelle} » non copiés : {e}"
                log.exception(message)
                self.bilan.erreurs.append(message)
                self.alertes.lever(f"erreur:commentaires:{lien.op_id}", message)


class _ExecuteurCumule(Executeur):
    """Exécuteur dont le bilan s'ajoute à celui du cycle."""

    def __init__(self, bilan_cycle: Bilan, *args: Any):
        super().__init__(*args)
        self._bilan_cycle = bilan_cycle

    def appliquer(self, actions: Any) -> Bilan:
        bilan = super().appliquer(actions)
        self._bilan_cycle.ajouter(bilan)
        self.bilan = Bilan()
        return bilan


def rendre_commentaires(commentaires: list[Commentaire], fuseau: ZoneInfo) -> str:
    """Bloc HTML ajouté à la note privée de la tâche Dolibarr (vide s'il n'y a aucun commentaire)."""
    if not commentaires:
        return ""
    parties = [f"<p>{MARQUEUR_COMMENTAIRES}</p>"]
    for c in sorted(commentaires, key=lambda c: c.date):
        quand = c.date.astimezone(fuseau).strftime("%d/%m/%Y %H:%M")
        parties.append(f"<p><strong>{quand} · {_echapper(c.auteur)}</strong></p>")
        parties.append(markdown_vers_html(c.texte))
    return "\n".join(parties)


def _echapper(texte: str) -> str:
    return texte.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def remplacer_bloc(note: str | None, bloc: str) -> str:
    """Remplace le bloc de commentaires d'une note en gardant ce que l'utilisateur a écrit avant."""
    note = note or ""
    position = note.find(MARQUEUR_COMMENTAIRES)
    if position >= 0:
        debut_paragraphe = note.rfind("<p", 0, position)
        if debut_paragraphe >= 0 and position - debut_paragraphe <= 40:
            position = debut_paragraphe
        note = note[:position]
    note = note.rstrip()
    if not bloc:
        return note
    return f"{note}\n{bloc}" if note else bloc
