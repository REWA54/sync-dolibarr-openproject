"""Un cycle de synchronisation, de la lecture des deux outils à l'écriture des écarts.

Déroulé :
  A. lire tout, des deux côtés ; confirmer une à une (accès direct → 404) les absences ;
  B. disjoncteur : trop de suppressions prévues → on s'arrête avant toute écriture ;
  C. créations et modifications, type par type, de haut en bas (utilisateurs → temps) ;
  D. suppressions, de bas en haut (temps → projets) ;
  E. commentaires OpenProject → note de la tâche Dolibarr.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .adaptateurs import Adaptateur, ErreurApi, Simulateur
from .alertes import Alertes
from .conversions import maintenant, markdown_vers_html
from .entites import ORDRE, PROJET, TACHE
from .etat import Etat
from .execution import Bilan, Echo, Executeur
from .modele import COTES, NOM_COTE, Cloturer, Cote, Enreg, Lien, Regles, Supprimer
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
    avant_cycle: Callable[[], None] = lambda: None
    seuil_suppressions: int = 5
    seuil_pourcent: int = 20
    fuseau: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Paris"))


@dataclass
class Resultat:
    cycle: int
    statut: str  # « réussi », « bloqué » (disjoncteur) ou « échec »
    bilan: Bilan
    message: str = ""
    ecritures: list[str] = field(default_factory=list)


class Disjoncteur(Exception):
    pass


class _NotesSimulees:
    """En simulation, la copie des commentaires est seulement notée."""

    def __init__(self) -> None:
        self.ecritures: list[str] = []

    def ecrire_bloc(self, identifiant: str, bloc_html: str) -> None:
        self.ecritures.append(f"{'Dolibarr':<11} {'commentaires':<11} copier dans la note de la tâche {identifiant}")


def executer_cycle(
    ctx: Contexte,
    etat: Etat,
    alertes: Alertes,
    *,
    mode: str,
    confirmer_suppressions: bool = False,
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

    cycle = etat.debuter_cycle(mode)
    bilan = Bilan()
    statut, message = "réussi", ""
    try:
        ctx.avant_cycle()
        _Cycle(ctx, adaptateurs, notes, etat, alertes, cycle, bilan, echo, confirmer_suppressions).derouler()
    except Disjoncteur as e:
        statut, message = "bloqué", str(e)
        alertes.lever("disjoncteur", message, permanente=True)
    except ErreurApi as e:  # panne ou refus d'un des outils : le message suffit
        statut, message = "échec", str(e)
        log.error("cycle %s en échec : %s", cycle, message)
    except Exception as e:  # défaut inattendu : on garde la trace complète
        statut, message = "échec", f"{type(e).__name__} : {e}"
        log.exception("cycle %s en échec", cycle)
    etat.terminer_cycle(cycle, statut, {**bilan.resume(), "message": message})

    if statut == "réussi":
        alertes.retablir("disjoncteur", "Le disjoncteur est refermé : la synchronisation a repris.")
        alertes.fin_de_cycle()
        if ecrire:
            etat.poser_meta("dernier_succes", maintenant().isoformat(timespec="seconds"))
    if mode == "service":
        if statut == "échec":
            n = etat.echecs_consecutifs()
            if n >= 3:
                alertes.lever("echec", f"{n} cycles en échec de suite. Dernière erreur : {message}", permanente=True)
        elif statut == "réussi":
            alertes.retablir("echec", "La synchronisation fonctionne à nouveau.")

    ecritures = [f"{NOM_COTE[s.cote]:<11} {s.type:<11} {e}" for s in simulateurs for e in s.ecritures]
    ecritures += notes_simulees.ecritures
    return Resultat(cycle, statut, bilan, message, ecritures)


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
    ):
        self.ctx = ctx
        self.notes = notes
        self.a = adaptateurs
        self.etat = etat
        self.alertes = alertes
        self.cycle = cycle
        self.bilan = bilan
        self.echo = echo
        self.confirmer = confirmer_suppressions
        self.lus: dict[str, dict[Cote, dict[str, Enreg]]] = {}
        self.absents: dict[str, dict[Cote, set[str]]] = {}
        self.geles: dict[Cote, set[str]] = {"dol": set(), "op": set()}

    def adaptateurs_de(self, type_: str) -> dict[Cote, Adaptateur]:
        return {c: self.a[c][type_] for c in COTES}

    # ------------------------------------------------------------------------- A

    def lire(self) -> None:
        for r in ORDRE:
            liens = self.etat.liens(r.type)
            lus = {c: self.a[c][r.type].lister() for c in COTES}
            actifs = [lien for lien in liens if not lien.rompu]
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
            self.absents[r.type] = self.confirmer_absences(r, lus, actifs)
            if r is PROJET:
                self.geles = self.calculer_geles(liens, lus)

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
                objet = self.a[c][r.type].lire(identifiant)
                if objet is None:
                    absents[c].add(identifiant)
                else:
                    lus[c][identifiant] = objet
        return absents

    def calculer_geles(self, liens: list[Lien], lus: dict[Cote, dict[str, Enreg]]) -> dict[Cote, set[str]]:
        geles: dict[Cote, set[str]] = {"dol": set(), "op": set()}
        for lien in liens:
            dol, op = lus["dol"].get(lien.dol_id), lus["op"].get(lien.op_id)
            if (
                lien.rompu
                or dol is None
                or op is None
                or not dol.champs.get("actif", True)
                or not op.champs.get("actif", True)
            ):
                geles["dol"].add(lien.dol_id)
                geles["op"].add(lien.op_id)
        for c in COTES:
            geles[c].update(i for i, p in lus[c].items() if not p.champs.get("actif", True))
        return geles

    # ------------------------------------------------------------------------- B

    def entree(self, r: Regles, traducteur: Traducteur) -> Entree:
        return Entree(
            regles=r,
            objets=self.lus[r.type],
            liens=self.etat.liens(r.type),
            traducteur=traducteur,
            absents=self.absents[r.type],
            en_cours=self.etat.en_cours(),
            geles=self.geles,
        )

    def verifier_disjoncteur(self, traducteur: Traducteur) -> None:
        total = 0
        details: list[str] = []
        for r in ORDRE:
            actions = reconcilier(self.entree(r, traducteur))
            n = sum(isinstance(a, Supprimer | Cloturer) for a in actions)
            if not n:
                continue
            total += n
            details.append(f"{n} × {r.libelle}")
            relies = sum(not lien.rompu for lien in self.etat.liens(r.type))
            if relies >= 10 and n * 100 > relies * self.ctx.seuil_pourcent and not self.confirmer:
                raise Disjoncteur(
                    f"{n} suppressions de « {r.libelle} » prévues sur {relies} reliés "
                    f"(plus de {self.ctx.seuil_pourcent} %). Rien n'a été écrit. "
                    "Vérifier avec « dolop simuler », puis lancer « dolop une-fois --confirmer-suppressions »."
                )
        if total > self.ctx.seuil_suppressions and not self.confirmer:
            raise Disjoncteur(
                f"{total} suppressions ou clôtures prévues ({', '.join(details)}), "
                f"au-delà du seuil de {self.ctx.seuil_suppressions}. Rien n'a été écrit. "
                "Vérifier avec « dolop simuler », puis lancer « dolop une-fois --confirmer-suppressions »."
            )

    # ------------------------------------------------------------------------- C, D, E

    def derouler(self) -> None:
        self.lire()
        index = IndexLiens()
        index.charger(self.etat.liens())
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
        return _ExecuteurCumule(
            self.bilan,
            r,
            self.adaptateurs_de(r.type),
            self.etat,
            traducteur,
            self.alertes,
            self.cycle,
            self.echo,
        )

    def synchroniser_commentaires(self) -> None:
        source, cible = self.ctx.commentaires_op, self.notes
        if source is None or cible is None:
            return
        lus = self.lus[TACHE.type]
        for lien in self.etat.liens(TACHE.type):
            dol, op = lus["dol"].get(lien.dol_id), lus["op"].get(lien.op_id)
            if lien.rompu or dol is None or op is None:
                continue
            if str(dol.champs.get("projet")) in self.geles["dol"]:
                continue
            maj = op.modifie_le.isoformat() if op.modifie_le else None
            vu, empreinte = self.etat.commentaires(lien.op_id)
            if maj is not None and maj == vu:
                continue
            try:
                commentaires = source.commentaires(lien.op_id)
                bloc = rendre_commentaires(commentaires, self.ctx.fuseau)
                nouvelle = hashlib.sha1(bloc.encode()).hexdigest() if commentaires else None
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
