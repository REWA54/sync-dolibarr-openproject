"""Lecture incrémentale : entre deux lectures complètes, ne lire que ce qui a changé, sans rien rater
ni rien doubler. Les suppressions attendent la lecture complète, seule à pouvoir les constater."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from faux import Banc, projet, tache, temps
from test_adaptateurs import Serveur, _page, dolibarr, openproject
from test_cycle import banc_relie

from dolop.modele import COTES

HORAIRE = {"lecture_complete_minutes": 60}


def service(b: Banc, **kw: Any) -> Any:
    r = b.cycle(mode="service", **HORAIRE, **kw)
    assert r.statut == "réussi", r.message
    return r


def _banc_avec_temps(n: int = 30) -> tuple[Banc, dict[str, str]]:
    b, ids = banc_relie()
    for i in range(n):
        b.op["temps"].ajouter(f"oe{i}", **temps(ids["op"], ids["ot"], ids["ou"], duree=60 * (i + 1)))
    b.cycle()
    b.oublier_ecritures()
    return b, ids


def _listes_completes(b: Banc, type_: str) -> int:
    return b.dol[type_].listes_completes + b.op[type_].listes_completes


def test_premier_cycle_du_service_complet_puis_incremental() -> None:
    b, _ = _banc_avec_temps()
    assert service(b, forcer_lecture_complete=True).lecture == "complète", "au démarrage du service"
    avant = _listes_completes(b, "temps")
    assert service(b).lecture == "incrémentale"
    assert _listes_completes(b, "temps") == avant, "les temps ne sont plus relus en entier"


def test_modification_dun_seul_cote_propagee_sans_relire_lautre() -> None:
    b, ids = _banc_avec_temps()
    service(b)
    b.oublier_ecritures()
    b.op["tache"].changer(ids["ot"], titre="Maquette v2")
    # Le jumeau Dolibarr, resté intact, n'est pas relu : son instantané suffit (le relire échouerait).
    b.dol["tache"].interdits.add(ids["dt"])
    r = service(b)
    assert r.lecture == "incrémentale"
    assert b.dol["tache"].objets[ids["dt"]]["titre"] == "Maquette v2"
    assert b.ecritures() == [f"dol tache modifier {ids['dt']} ['titre']"]


def test_creation_arrive_au_cycle_incremental_suivant() -> None:
    b, ids = _banc_avec_temps()
    service(b)
    b.op["temps"].ajouter("oe99", **temps(ids["op"], ids["ot"], ids["ou"], duree=7200))
    service(b)
    assert any(o["duree"] == 7200 for o in b.dol["temps"].objets.values())


def test_suppression_attend_la_lecture_complete() -> None:
    b, _ = _banc_avec_temps()
    service(b)
    del b.op["temps"].objets["oe3"]
    service(b)
    assert len(b.dol["temps"].objets) == 30, "absent d'une liste de modifiés ne veut pas dire supprimé"
    b.horloge.avancer(61)
    assert service(b).lecture == "complète"
    assert len(b.dol["temps"].objets) == 29


def test_jumeau_designe_par_son_identifiant_embarque_lu_plutot_que_recopie() -> None:
    """Un lot créé par un cycle interrompu avant d'enregistrer le lien : sa tâche d'origine, intacte
    depuis, n'est pas dans la liste des modifiés. Sans la lire, elle serait recopiée en double."""
    b, ids = _banc_avec_temps()
    service(b)
    b.horloge.avancer(10)
    b.dol["tache"].ajouter("dt7", **tache(ids["dp"], "Recette"))
    b.dol["tache"].touches["dt7"] = datetime(2026, 1, 1, tzinfo=UTC)  # ancienne, jamais modifiée depuis
    b.op["tache"].ajouter("ot7", ref_autre="dt7", **tache(ids["op"], "Recette"))
    service(b)
    assert sum(o["titre"] == "Recette" for o in b.dol["tache"].objets.values()) == 1
    assert b.etat.lien_par_paire("tache", "dt7", "ot7") is not None


def test_filtre_refuse_par_un_outil_on_relit_tout() -> None:
    b, _ = _banc_avec_temps()
    service(b)
    for cote in COTES:
        (b.dol if cote == "dol" else b.op)["temps"].filtre_refuse = True
    del b.op["temps"].objets["oe3"]
    service(b)
    assert len(b.dol["temps"].objets) == 29, "lu en entier, le type retrouve aussi ses suppressions"


def test_commentaires_pas_relus_pour_un_lot_reste_intact() -> None:
    b, ids = _banc_avec_temps()
    service(b)
    appels = b.commentaires.appels
    b.dol["tache"].changer(ids["dt"], avancement=50)
    service(b)
    assert b.op["tache"].objets[ids["ot"]]["avancement"] == 50
    assert b.commentaires.appels == appels


def test_alerte_dun_objet_non_relu_ni_eteinte_ni_renvoyee() -> None:
    b = Banc(tiers=[])
    b.op["projet"].ajouter("op1", **projet(client="Inconnu"))
    b.cycle(mode="service", **HORAIRE)
    for _ in range(3):
        b.cycle(mode="service", **HORAIRE)
    b.horloge.avancer(61)
    b.cycle(mode="service", **HORAIRE)
    assert sum("Inconnu" in m for _, m in b.envoyees) == 1


def test_supprime_dun_cote_modifie_de_lautre_ni_panne_ni_alerte_puis_suppression() -> None:
    b, ids = _banc_avec_temps(n=0)
    service(b)
    del b.op["tache"].objets[ids["ot"]]
    b.dol["tache"].changer(ids["dt"], titre="Maquette v2")
    r = service(b)
    assert r.lecture == "incrémentale" and not r.bilan.erreurs
    assert b.envoyees == [], "pas de fausse alerte : la lecture complète s'en chargera"
    b.horloge.avancer(61)
    service(b)
    assert ids["dt"] not in b.dol["tache"].objets, "la suppression est recopiée à la lecture complète"


# ---------------------------------------------------------------------- adaptateurs


DEPUIS = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def test_dolibarr_modifies_depuis_avec_marge_de_fuseau() -> None:
    s = Serveur()
    s.route("GET", "/tasks", (200, []))
    s.route("GET", "/projects", (200, []))
    s.route("GET", "/users", (200, []))
    s.route("GET", "/projects/alltimespent", (200, []))
    _, a = dolibarr(s)
    a["tache"].lister_depuis(DEPUIS)
    a["temps"].lister_depuis(DEPUIS)
    requetes = " ".join(httpx_query(c) for _, c, _ in s.appels)
    assert "(t.tms:>=:'2026-09-28 07:00:00')" in requetes, "3 h de marge : base en UTC ou à Paris"
    assert "(et.tms:>=:'2026-09-28 07:00:00')" in requetes


def test_openproject_modifies_depuis_en_utc() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/projects", (200, _page([{"id": 5, "active": True}])))
    s.route("GET", "/api/v3/users", (200, _page([])))
    s.route("GET", "/api/v3/work_packages", (200, _page([])))
    s.route("GET", "/api/v3/time_entries", (200, _page([])))
    _, a = openproject(s)
    a["tache"].lister_depuis(DEPUIS)
    a["temps"].lister_depuis(DEPUIS)
    requetes = " ".join(httpx_query(c) for _, c, _ in s.appels)
    assert requetes.count('"updatedAt": {"operator": "<>d", "values": ["2026-09-28T09:55:00Z", ""]}') == 2


def httpx_query(chemin: str) -> str:
    q = parse_qs(urlsplit(chemin).query)
    return " ".join(v for valeurs in q.values() for v in valeurs)
