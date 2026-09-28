"""« dolop apparier » : relier des objets qui existent déjà des deux côtés, avant le premier cycle.

Le service ne rapproche automatiquement que les utilisateurs (e-mail) et les membres (projet et
utilisateur). Deux outils déjà remplis contiennent souvent aussi les mêmes projets, tâches et temps :
sans appariement, le premier cycle les recopierait en double (et des temps en double se facturent
deux fois). Deux projets peuvent porter le même titre : le service ne devine donc pas, il propose.

1. ``dolop apparier`` écrit les propositions dans un fichier CSV (un projet ↔ un projet de même
   titre, une tâche ↔ une tâche de même titre dans le projet apparié, un temps ↔ un temps identique) ;
2. un humain le relit et retire les lignes fausses ;
3. ``dolop apparier --appliquer fichier`` montre ce qui sera relié, ``--oui`` le fait : lien dans la
   base d'état et identifiant du jumeau embarqué de chaque côté. Aucun champ n'est modifié : le cycle
   suivant aligne les deux objets, celui modifié en dernier l'emporte (voir « dolop simuler »).
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .adaptateurs import Adaptateur
from .entites import ORDRE
from .etat import Etat
from .modele import Cote, Enreg, Lien, Relier, Signaler
from .reconciliation import Entree, reconcilier
from .traduction import Convertisseur, IndexLiens, Traducteur

# Dans l'ordre des cycles : une tâche suit son projet apparié, un temps son utilisateur et sa tâche.
# Utilisateurs et membres sont proposés comme le service les rapprocherait (e-mail ; projet et personne).
TYPES = ORDRE
ENTETE = ["type", "dolibarr", "libelle_dolibarr", "openproject", "libelle_openproject"]


@dataclass(frozen=True)
class Proposition:
    type: str
    dol_id: str
    dol_libelle: str
    op_id: str
    op_libelle: str


def proposer(
    lus: Mapping[str, dict[Cote, dict[str, Enreg]]],
    liens: list[Lien],
    geles: dict[Cote, set[str]],
    convertisseurs: Mapping[tuple[str, str], Convertisseur] | None = None,
) -> tuple[list[Proposition], list[str]]:
    """Propositions (objets non reliés, de même identité, sans ambiguïté) et doutes à trancher à la main."""
    index = IndexLiens()
    index.charger(liens)
    traducteur = Traducteur(index, convertisseurs)
    propositions: list[Proposition] = []
    doutes: list[str] = []
    for r in TYPES:
        # Le moteur du service, avec l'appariement par identité qu'il n'applique d'habitude qu'aux
        # utilisateurs et aux membres.
        entree = Entree(
            regles=replace(r, apparier=True),
            objets=lus[r.type],
            liens=[lien for lien in liens if lien.type == r.type],
            traducteur=traducteur,
            geles=geles,
        )
        for action in reconcilier(entree):
            if isinstance(action, Relier) and action.motif == "même identité":
                dol, op = lus[r.type]["dol"][action.dol_id], lus[r.type]["op"][action.op_id]
                propositions.append(Proposition(r.type, dol.id, dol.libelle, op.id, op.libelle))
                index.ajouter(r.type, dol.id, op.id)  # provisoire : les tâches suivent leur projet proposé
            elif isinstance(action, Signaler) and action.cle.startswith("ambigu:"):
                doutes.append(action.message)
    return propositions, doutes


def ecrire(propositions: Iterable[Proposition], chemin: Path) -> None:
    with chemin.open("w", encoding="utf-8", newline="") as f:
        ecrivain = csv.writer(f, delimiter=";")
        ecrivain.writerow(ENTETE)
        ecrivain.writerows([p.type, p.dol_id, p.dol_libelle, p.op_id, p.op_libelle] for p in propositions)


def lire(chemin: Path) -> list[Proposition]:
    with chemin.open(encoding="utf-8", newline="") as f:
        lignes = list(csv.reader(f, delimiter=";"))
    if not lignes or [c.strip() for c in lignes[0]] != ENTETE:
        raise ValueError(f"{chemin} : en-tête attendu « {';'.join(ENTETE)} »")
    types = {r.type for r in TYPES}
    resultat = []
    for numero, ligne in enumerate(lignes[1:], start=2):
        if not ligne or not "".join(ligne).strip():
            continue
        if len(ligne) != len(ENTETE) or ligne[0] not in types or not ligne[1].strip() or not ligne[3].strip():
            raise ValueError(f"{chemin}, ligne {numero} : ligne invalide {ligne!r}")
        resultat.append(Proposition(ligne[0], ligne[1].strip(), ligne[2], ligne[3].strip(), ligne[4]))
    ordre = {r.type: i for i, r in enumerate(TYPES)}
    return sorted(resultat, key=lambda p: ordre[p.type])  # projets d'abord, même si le fichier a été trié


def appliquer(
    propositions: list[Proposition],
    adaptateurs: Mapping[Cote, Mapping[str, Adaptateur]],
    etat: Etat,
    *,
    oui: bool,
    echo: Callable[[str], None] = print,
) -> int:
    """Relie chaque paire encore libre des deux côtés et qui existe toujours. Renvoie le nombre relié."""
    cycle = etat.debuter_cycle("apparier") if oui else 0
    relies = 0
    deja: dict[tuple[str, Cote], set[str]] = {}
    for p in propositions:
        vus = (deja.setdefault((p.type, "dol"), set()), deja.setdefault((p.type, "op"), set()))
        if p.dol_id in vus[0] or p.op_id in vus[1]:
            echo(f"  ✗ {p.type} {p.dol_id} ↔ {p.op_id} : l'un des deux figure déjà plus haut dans le fichier")
            continue
        if etat.lien_par_id(p.type, "dol", p.dol_id) or etat.lien_par_id(p.type, "op", p.op_id):
            echo(f"  ✗ {p.type} {p.dol_id} ↔ {p.op_id} : l'un des deux est déjà relié")
            continue
        dol = adaptateurs["dol"][p.type].lire(p.dol_id)
        op = adaptateurs["op"][p.type].lire(p.op_id)
        if dol is None or op is None:
            outil = "Dolibarr" if dol is None else "OpenProject"
            echo(f"  ✗ {p.type} {p.dol_id} ↔ {p.op_id} : introuvable dans {outil}")
            continue
        vus[0].add(p.dol_id)
        vus[1].add(p.op_id)
        echo(f"  {p.type:<7} Dolibarr {p.dol_id} « {dol.libelle} » ↔ OpenProject {p.op_id} « {op.libelle} »")
        relies += 1
        if not oui:
            continue
        # Sans instantanés : au cycle suivant, chaque champ qui diffère est tranché à l'horodatage.
        etat.creer_lien(p.type, p.dol_id, p.op_id)
        adaptateurs["dol"][p.type].poser_ref_autre(p.dol_id, p.op_id)
        adaptateurs["op"][p.type].poser_ref_autre(p.op_id, p.dol_id)
        etat.journaliser(cycle, p.type, "relier", None, None, f"Dolibarr {p.dol_id} ↔ OpenProject {p.op_id} (apparier)")
    if oui:
        etat.terminer_cycle(cycle, "réussi", {"reliés": relies})
    return relies
