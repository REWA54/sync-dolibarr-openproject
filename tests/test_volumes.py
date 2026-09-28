"""Outils remplis : des milliers d'objets ne doivent coûter ni du temps au carré, ni une requête par
objet quand on peut l'éviter, ni toute la mémoire du conteneur."""

from __future__ import annotations

import csv
import json
import threading
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from faux import Banc, projet
from test_adaptateurs import CONFIG, Serveur, dolibarr, openproject
from test_robustesse import _env

from dolop.__main__ import afficher_plan, main
from dolop.cycle import Ecriture, Resultat
from dolop.etat import Etat
from dolop.execution import Bilan

# ---------------------------------------------------------------------- Dolibarr


def _dolibarr_rempli(taches: int, temps: int) -> Serveur:
    s = Serveur()

    def page(liste: list[dict[str, Any]]) -> Any:
        def repondre(requete: httpx.Request) -> tuple[int, Any]:
            q = parse_qs(requete.url.query.decode())
            limite, numero = int(q["limit"][0]), int(q["page"][0])
            return 200, liste[numero * limite : (numero + 1) * limite]

        return repondre

    lignes_taches = [{"id": i, "label": f"T{i}", "fk_project": 1} for i in range(1, taches + 1)]
    lignes_temps = [
        {"rowid": i, "task_id": 1 + i % taches, "project_id": 1, "fk_user": 2, "element_datehour": "2026-01-02"}
        for i in range(1, temps + 1)
    ]
    s.route("GET", "/users", (200, [{"id": 2, "login": "alice", "status": 1}]))
    s.route("GET", "/projects", (200, [{"id": 1, "status": 2}]))  # projet clos : pas de contacts à lire
    s.route("GET", "/tasks", page(lignes_taches))
    s.route("GET", "/projects/alltimespent", page(lignes_temps))
    return s


def _cpu_lecture_des_temps(taches: int, temps: int) -> float:
    meilleur = float("inf")
    for _ in range(3):
        _, a = dolibarr(_dolibarr_rempli(taches, temps))
        debut = time.process_time()
        assert len(a["temps"].lister()) == temps
        meilleur = min(meilleur, time.process_time() - debut)
    return meilleur


def test_lire_quatre_fois_plus_de_temps_coute_environ_quatre_fois_plus() -> None:
    """La liste des tâches « hors tâche » était recalculée pour chaque temps lu : un coût au carré
    (20 000 temps et 2 000 tâches : 2 s de calcul, 100 000 et 10 000 : près d'une minute)."""
    petit = _cpu_lecture_des_temps(300, 3000)
    grand = _cpu_lecture_des_temps(1200, 12000)
    assert grand < 8 * petit, f"lecture au carré : {petit:.3f} s puis {grand:.3f} s"


def test_liste_hors_tache_lue_par_une_requete_filtree() -> None:
    s = _dolibarr_rempli(taches=5, temps=5)
    _, a = dolibarr(s)
    a["temps"].lister()
    a["tache"].lister()
    filtrees = [c for _, c, _ in s.appels if "/tasks?" in c and "sqlfilters" in c]
    assert len(filtrees) == 1, "une seule requête par cycle, gardée pour les tâches et les temps"
    assert "ef.openproject_id" in filtrees[0]


def test_filtre_sur_les_attributs_refuse_on_parcourt_les_taches() -> None:
    s = Serveur()

    def taches(requete: httpx.Request) -> tuple[int, Any]:
        if "sqlfilters" in requete.url.params:
            return 400, {"error": "Error when validating parameter sqlfilters"}
        return 200, [{"id": 50, "fk_project": 7, "array_options": {"options_openproject_id": "-"}}]

    s.route("GET", "/tasks", taches)
    d, _ = dolibarr(s)
    assert d.taches_hors_tache() == {"50": "7"}


def test_temps_anciens_sans_heure_une_requete_par_tache_et_non_par_temps() -> None:
    s = Serveur()
    s.route("GET", "/users", (200, [{"id": 2, "login": "alice"}]))
    s.route("GET", "/tasks", (200, []))
    lignes = [{"rowid": i, "task_id": 5 + i % 2, "project_id": 7, "fk_user": 2} for i in range(1, 7)]
    s.route("GET", "/projects/alltimespent", (200, lignes))
    for tache in (5, 6):
        ids = [i for i in range(1, 7) if 5 + i % 2 == tache]
        detail = [{"timespent_line_id": i, "timespent_line_date": 1790000000} for i in ids]
        s.route("GET", f"/tasks/{tache}/timespent", (200, detail))
    _, a = dolibarr(s)
    temps = a["temps"].lister()
    assert all(e.champs["date"] == "2026-09-21" for e in temps.values())
    assert sum("/timespent" in c for _, c, _ in s.appels) == 2


def test_contacts_des_taches_lus_en_parallele_sans_depasser_la_limite() -> None:
    """Un Dolibarr ne donne l'assigné d'une tâche que tâche par tâche : 2 000 tâches actives, c'étaient
    2 000 requêtes l'une après l'autre à chaque cycle."""
    en_vol, plus_haut = 0, 0
    verrou = threading.Lock()

    def contacts(_requete: httpx.Request) -> tuple[int, Any]:
        nonlocal en_vol, plus_haut
        with verrou:
            en_vol += 1
            plus_haut = max(plus_haut, en_vol)
        time.sleep(0.02)
        with verrou:
            en_vol -= 1
        return 200, []

    s = Serveur()
    s.route("GET", "/projects", (200, [{"id": 1, "status": 1}]))
    s.route("GET", "/tasks", (200, [{"id": i, "fk_project": 1} for i in range(1, 25)]))
    s.route("GET", r"/tasks/\d+/contacts", contacts)
    _, a = dolibarr(s)
    assert len(a["tache"].lister()) == 24
    assert 2 <= plus_haut <= CONFIG.lectures_paralleles


# ------------------------------------------------------------------- OpenProject


def _lot(i: int) -> dict[str, Any]:
    liens = {f"action{k}": {"href": f"/api/v3/work_packages/{i}/action{k}", "method": "post"} for k in range(40)}
    liens["project"] = {"href": "/api/v3/projects/1", "title": "Projet"}
    return {
        "id": i,
        "subject": f"Lot {i}",
        "description": {"raw": "Texte " * 60, "html": "<p>" + "Texte " * 60 + "</p>"},
        "_links": liens,
    }


def test_lots_convertis_page_par_page_sans_tout_garder_en_memoire() -> None:
    """Un lot pèse une vingtaine de Kio une fois lu : garder toutes les pages avant de les convertir
    dépassait les 256 Mo du conteneur vers 10 000 lots."""
    total, taille = 3000, CONFIG.taille_page_openproject
    corps = {
        n: json.dumps({"total": total, "_embedded": {"elements": [_lot(n * taille + i) for i in range(taille)]}})
        for n in range(total // taille)
    }

    def lots(requete: httpx.Request) -> httpx.Response:
        page = int(requete.url.params["offset"]) - 1
        return httpx.Response(200, content=corps.get(page, json.dumps({"total": total, "_embedded": {"elements": []}})))

    def transport(requete: httpx.Request) -> httpx.Response:
        chemin = requete.url.path
        if chemin.endswith("/work_packages"):
            return lots(requete)
        if chemin.endswith("/projects"):
            return httpx.Response(200, json={"total": 1, "_embedded": {"elements": [{"id": 1}]}})
        if chemin.endswith("/types"):
            return httpx.Response(200, json={"total": 1, "_embedded": {"elements": [{"id": 1, "name": "Task"}]}})
        return httpx.Response(200, json={})  # schéma sans champ personnalisé

    from dolop.openproject import OpenProject
    from dolop.openproject import adaptateurs as adaptateurs_op

    o = OpenProject(CONFIG, transport=httpx.MockTransport(transport))
    a = adaptateurs_op(o)
    tracemalloc.start()
    lus = a["tache"].lister()
    _, pic = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(lus) == total
    # Toutes les pages gardées, une fois décodées, pèsent environ 5 fois le JSON reçu.
    recu = sum(len(c) for c in corps.values())
    assert pic < 2.5 * recu, f"pic de {pic / 2**20:.1f} Mio pour {recu / 2**20:.1f} Mio reçus"


def test_taille_des_pages_openproject_reglable() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/roles", (200, {"total": 0, "_embedded": {"elements": []}}))
    o, _ = openproject(s)
    o.pages("/roles")
    assert f"pageSize={CONFIG.taille_page_openproject}" in s.appels[0][1]


# ------------------------------------------------------------------- simulation


def test_simulation_dun_outil_rempli_resumee_et_exportee(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = [Ecriture("OpenProject", "projet", "créer", f"~{i} {{'titre': 'P{i}'}}") for i in range(500)]
    plan += [Ecriture("Dolibarr", "temps", "créer", f"~{i}") for i in range(1000)]
    export = tmp_path / "simulation.csv"
    afficher_plan(Resultat(1, "réussi", Bilan(), plan=plan), str(export))
    sortie = capsys.readouterr().out
    assert "OpenProject projet       créer          500" in sortie
    assert "Dolibarr    temps        créer         1000" in sortie
    assert sortie.count("{'titre'") <= 40, "le détail est dans l'export, pas à l'écran"
    assert "… et 1460 autres" in sortie
    with export.open(encoding="utf-8") as f:
        lignes = list(csv.reader(f, delimiter=";"))
    assert lignes[0] == ["outil", "type", "action", "detail"] and len(lignes) == 1501


# -------------------------------------------------------------------------- santé


def _poser(etat: Etat, **meta: str) -> None:
    for cle, valeur in meta.items():
        etat.poser_meta(cle, valeur)


def _il_y_a(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_sante_verte_pendant_un_long_premier_chargement_qui_avance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    etat = Etat.ouvrir(tmp_path / "etat.sqlite")
    etat.debuter_cycle("une-fois")  # commencé il y a 2 heures, jamais terminé
    en_cours = {"cycle": 1, "debut": _il_y_a(120), "battement": _il_y_a(1)}
    _poser(etat, cycle_en_cours=json.dumps(en_cours))
    assert main(["sante"]) == 0
    _poser(etat, cycle_en_cours=json.dumps({**en_cours, "battement": _il_y_a(20)}))
    assert main(["sante"]) == 1, "plus aucun signe de vie : bloqué"
    etat.fermer()


def test_sante_rouge_si_les_cycles_echouent_meme_pendant_un_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    etat = Etat.ouvrir(tmp_path / "etat.sqlite")
    etat.terminer_cycle(etat.debuter_cycle("service"), "échec", {})
    en_cours = {"cycle": 2, "debut": _il_y_a(0), "battement": _il_y_a(0)}
    _poser(etat, dernier_succes=_il_y_a(40), cycle_en_cours=json.dumps(en_cours))
    assert main(["sante"]) == 1
    etat.fermer()


def test_signe_de_vie_pendant_le_cycle_efface_a_la_fin() -> None:
    b = Banc()
    b.dol["projet"].ajouter("dp1", **projet())
    vu: list[str | None] = []
    lister = b.dol["tache"].lister

    def lister_en_notant() -> Any:
        vu.append(b.etat.meta("cycle_en_cours"))
        return lister()

    b.dol["tache"].lister = lister_en_notant  # type: ignore[method-assign]
    assert b.cycle().statut == "réussi"
    assert vu and vu[0] is not None and json.loads(vu[0])["cycle"] == 1
    assert b.etat.meta("cycle_en_cours") is None
