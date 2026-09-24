"""Ligne de commande.

dolop verifier                         chaque prérequis de la mise en route (lecture seule)
dolop simuler                          ce qui serait fait, sans rien écrire (par défaut)
dolop une-fois [--confirmer-suppressions]  un cycle réel
dolop service                          un cycle toutes les INTERVALLE_SECONDES
dolop rapport                          liens, derniers cycles, alertes en cours
dolop annuler <cycle> [--oui]          supprimer ce qu'un cycle a créé (montre d'abord)
dolop sauvegarder [fichier]            copie cohérente de la base d'état, même pendant un cycle
dolop sante                            code 0 si un cycle a réussi il y a moins de 15 min
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .adaptateurs import Adaptateur
from .alertes import TITRE, Alertes, envoi_webhook
from .config import Config, ErreurConfig
from .conversions import age
from .cycle import Contexte, Resultat, executer_cycle
from .dolibarr import Dolibarr
from .dolibarr import adaptateurs as adaptateurs_dol
from .etat import BaseIllisible, Etat
from .http import Introuvable
from .modele import NOM_COTE, Cote, autre
from .openproject import OpenProject
from .openproject import adaptateurs as adaptateurs_op
from .verification import rendre, verifier
from .verrou import DejaEnCours, verrou

log = logging.getLogger("dolop")

# Un cycle lancé à la main attend au plus ce temps que celui du service se termine.
ATTENTE_CYCLE = 300.0


def construire(config: Config) -> Contexte:
    d = Dolibarr(config)
    o = OpenProject(config)

    def avant_cycle() -> None:
        d.nouveau_cycle()
        o.nouveau_cycle()

    return Contexte(
        adaptateurs={"dol": adaptateurs_dol(d), "op": adaptateurs_op(o)},
        convertisseurs={("projet", "client"): d.convertir_client},
        commentaires_op=o,
        notes_dol=d,
        avant_cycle=avant_cycle,
        seuil_suppressions=config.seuil_suppressions,
        seuil_pourcent=config.seuil_pourcent,
        fuseau=config.fuseau,
    )


def afficher(resultat: Resultat) -> None:
    print(f"\nCycle {resultat.cycle} : {resultat.statut}")
    if resultat.message:
        print(f"  {resultat.message}")
    for cle, valeur in resultat.bilan.resume().items():
        if valeur:
            print(f"  {cle:<10} {valeur}")
    for erreur in resultat.bilan.erreurs:
        print(f"  ✗ {erreur}")


def cmd_simuler(config: Config, etat: Etat) -> int:
    print("SIMULATION — rien n'est écrit, ni dans Dolibarr, ni dans OpenProject, ni dans la base d'état.\n")
    r = executer_cycle(construire(config), etat, Alertes(etat, None), mode="simuler", echo=print)
    if r.ecritures:
        print("\nÉcritures qui seraient faites :")
        for ligne in r.ecritures:
            print(f"  {ligne}")
    elif r.statut == "réussi":
        print("\nAucune écriture : les deux outils sont d'accord.")
    afficher(r)
    return 0 if r.statut == "réussi" else 1


def _alertes(config: Config, etat: Etat) -> Alertes:
    return Alertes(etat, envoi_webhook(config.webhook) if config.webhook else None)


def cmd_une_fois(config: Config, etat: Etat, confirmer: bool) -> int:
    with verrou(config.verrou, attente=ATTENTE_CYCLE):
        r = executer_cycle(
            construire(config),
            etat,
            _alertes(config, etat),
            mode="une-fois",
            confirmer_suppressions=confirmer,
            echo=print,
        )
    afficher(r)
    return 0 if r.statut == "réussi" and not r.bilan.erreurs else 1


def cmd_service(config: Config, etat: Etat) -> int:
    arret = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: arret.set())
    try:
        etat.verifier_integrite()
    except BaseIllisible as e:
        _alerter_sans_base(config, f"Le service ne démarre pas : {e}. Restaurer la dernière sauvegarde.")
        raise
    ctx = construire(config)
    alertes = _alertes(config, etat)
    log.info("service %s démarré : un cycle toutes les %s s", _version(), config.intervalle)
    while not arret.is_set():
        # Rien ne doit arrêter la boucle : une erreur imprévue est journalisée et le cycle suivant
        # repart de zéro. Si elle se répète, « dolop sante » passe au rouge au bout de 15 minutes.
        try:
            with verrou(config.verrou, attente=config.intervalle):
                r = executer_cycle(ctx, etat, alertes, mode="service")
            resume = {k: v for k, v in r.bilan.resume().items() if v}
            log.info("cycle %s : %s %s %s", r.cycle, r.statut, json.dumps(resume, ensure_ascii=False), r.message)
            entretenir(config, etat, alertes)
        except DejaEnCours as e:
            log.warning("cycle sauté : %s", e)
        except Exception:
            log.exception("erreur hors cycle, nouvel essai au prochain tour")
        arret.wait(config.intervalle)
    log.info("service arrêté proprement")
    return 0


def _alerter_sans_base(config: Config, message: str) -> None:
    """Alerte envoyée directement : la base, qui mémorise les alertes, est justement hors d'usage."""
    log.critical(message)
    if config.webhook:
        with contextlib.suppress(Exception):
            envoi_webhook(config.webhook)(TITRE, message)


def entretenir(config: Config, etat: Etat, alertes: Alertes) -> None:
    """Une fois par jour : sauvegarde de la base d'état, puis purge du vieil historique."""
    derniere = etat.meta("derniere_sauvegarde")
    if derniere and age(datetime.fromisoformat(derniere)) < timedelta(days=1):
        return
    try:
        if config.sauvegardes > 0:
            horodatage = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            chemin = etat.sauvegarder(config.dossier_sauvegardes / f"etat-{horodatage}.sqlite")
            anciennes = sorted(config.dossier_sauvegardes.glob("etat-*.sqlite"))[: -config.sauvegardes]
            for ancienne in anciennes:
                ancienne.unlink(missing_ok=True)
            log.info("base d'état sauvegardée : %s (%s gardées)", chemin, config.sauvegardes)
        purges = etat.purger(timedelta(days=config.conservation_jours))
        if purges:
            log.info("historique : %s cycles de plus de %s jours oubliés", purges, config.conservation_jours)
        etat.poser_meta("derniere_sauvegarde", datetime.now(UTC).isoformat(timespec="seconds"))
    except Exception as e:
        log.exception("sauvegarde de la base d'état impossible")
        alertes.lever("sauvegarde", f"Sauvegarde quotidienne de la base d'état impossible : {e}", permanente=True)
        return
    alertes.retablir("sauvegarde", "La sauvegarde quotidienne de la base d'état fonctionne à nouveau.")


def cmd_rapport(etat: Etat) -> int:
    print("Liens :")
    par_type: dict[str, list[int]] = {}
    for lien in etat.liens():
        compte = par_type.setdefault(lien.type, [0, 0])
        compte[1 if lien.rompu else 0] += 1
    for type_, (actifs, rompus) in par_type.items():
        print(f"  {type_:<12} {actifs} reliés" + (f", {rompus} rompus" if rompus else ""))
    en_cours = etat.en_cours()
    if en_cours:
        print(f"\nCréations interrompues à reprendre : {len(en_cours)}")
    print(f"\nDernier succès : {etat.meta('dernier_succes') or 'jamais'}")
    print("\nDerniers cycles :")
    for c in etat.derniers_cycles(10):
        resume = {k: v for k, v in json.loads(c["resume"] or "{}").items() if v}
        print(
            f"  n° {c['id']:<6} {c['debut']}  {c['mode']:<9} {c['statut']:<8} {json.dumps(resume, ensure_ascii=False)}"
        )
    alertes = etat.alertes_actives()
    print(f"\nAlertes en cours : {len(alertes) or 'aucune'}")
    for a in alertes:
        print(f"  • {a['message']}")
    return 0


def cmd_annuler(config: Config, etat: Etat, cycle: int, oui: bool) -> int:
    creations = [r for r in etat.journal(cycle) if r["action"] in ("créer", "recréer")]
    if not creations:
        print(f"Le cycle {cycle} n'a rien créé.")
        return 0
    if not oui:
        _annuler(config, etat, cycle, creations, oui=False)
        return 0
    with verrou(config.verrou, attente=ATTENTE_CYCLE):
        _annuler(config, etat, cycle, creations, oui=True)
    return 0


def _annuler(config: Config, etat: Etat, cycle: int, creations: list[Any], *, oui: bool) -> None:
    ctx = construire(config)
    print(("Suppression" if oui else "Serait supprimé (ajouter --oui pour le faire)") + f" — cycle {cycle} :")
    for row in reversed(creations):  # les temps avant leur tâche, les tâches avant leur projet
        cote: Cote = row["cote"]
        type_, identifiant = row["type"], row["objet_id"]
        print(f"  {NOM_COTE[cote]:<11} {type_:<11} {identifiant}")
        if not oui:
            continue
        adaptateur: Adaptateur = ctx.adaptateurs[cote][type_]
        with contextlib.suppress(Introuvable):  # déjà supprimé à la main
            adaptateur.supprimer(identifiant)
        source = json.loads(row["detail"] or "{}").get("source")
        if source:
            try:
                ctx.adaptateurs[autre(cote)][type_].poser_ref_autre(source, None)
            except Exception as e:
                print(f"    (identifiant embarqué non effacé sur {source} : {e})")
        lien = etat.lien_par_id(type_, cote, identifiant)
        if lien is not None:
            etat.supprimer_lien(lien.id)


def cmd_sauvegarder(config: Config, destination: str | None) -> int:
    horodatage = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    cible = Path(destination) if destination else config.dossier_sauvegardes / f"etat-{horodatage}.sqlite"
    with Etat.lire_seulement(config.base) as etat:
        print(f"Base d'état sauvegardée : {etat.sauvegarder(cible)}")
    return 0


def cmd_verifier(config: Config) -> int:
    print("Vérification des prérequis (lecture seule)\n")
    texte, tout_bon = rendre(list(verifier(config)))
    print(texte)
    print("\nTout est prêt." if tout_bon else "\nÀ corriger avant de synchroniser.")
    return 0 if tout_bon else 1


def cmd_sante(config: Config) -> int:
    try:
        with Etat.lire_seulement(config.base) as etat:
            dernier = etat.meta("dernier_succes")
    except (BaseIllisible, OSError, sqlite3.Error) as e:
        print(f"base d'état illisible : {e}")
        return 1
    if dernier and age(datetime.fromisoformat(dernier)) < timedelta(minutes=15):
        return 0
    print(f"aucun cycle réussi depuis 15 min (dernier : {dernier or 'jamais'})")
    return 1


def _version() -> str:
    try:
        return version("dolop")
    except PackageNotFoundError:
        return "inconnue"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dolop", description="Synchronisation Dolibarr ↔ OpenProject")
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sous = parser.add_subparsers(dest="commande")
    sous.add_parser("verifier")
    sous.add_parser("simuler")
    une = sous.add_parser("une-fois")
    une.add_argument("--confirmer-suppressions", action="store_true")
    sous.add_parser("service")
    sous.add_parser("rapport")
    ann = sous.add_parser("annuler")
    ann.add_argument("cycle", type=int)
    ann.add_argument("--oui", action="store_true")
    sauv = sous.add_parser("sauvegarder")
    sauv.add_argument("destination", nargs="?")
    sous.add_parser("sante")
    args = parser.parse_args(argv)

    # La base d'état, ses sauvegardes et le verrou contiennent des données personnelles
    # (noms, e-mails, notes) : lisibles par le seul compte du service.
    os.umask(0o077)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = Config.depuis_env()
    except ErreurConfig as e:
        print(f"Configuration : {e}", file=sys.stderr)
        return 2
    commande = args.commande or "simuler"
    try:
        return _executer(commande, args, config)
    except DejaEnCours as e:
        print(f"Impossible pour l'instant : {e}", file=sys.stderr)
        return 3
    except BaseIllisible as e:
        print(f"{e}. Restaurer la dernière sauvegarde (voir docs/exploitation.md).", file=sys.stderr)
        return 4


def _executer(commande: str, args: argparse.Namespace, config: Config) -> int:
    if commande == "verifier":
        return cmd_verifier(config)
    if commande == "sante":
        return cmd_sante(config)
    if commande == "rapport":
        with Etat.lire_seulement(config.base) as etat:
            return cmd_rapport(etat)
    if commande == "sauvegarder":
        return cmd_sauvegarder(config, args.destination)
    try:
        etat = Etat.ouvrir(config.base)
    except BaseIllisible as e:
        if commande == "service":
            _alerter_sans_base(config, f"Le service ne démarre pas : {e}. Restaurer la dernière sauvegarde.")
        raise
    with etat:
        if commande == "simuler":
            return cmd_simuler(config, etat)
        if commande == "une-fois":
            return cmd_une_fois(config, etat, args.confirmer_suppressions)
        if commande == "service":
            return cmd_service(config, etat)
        return cmd_annuler(config, etat, args.cycle, args.oui)


if __name__ == "__main__":
    sys.exit(main())
