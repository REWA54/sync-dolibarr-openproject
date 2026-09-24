"""Les vrais adaptateurs face à un faux serveur HTTP : on vérifie les requêtes réellement envoyées."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from dolop.adaptateurs import ErreurApi
from dolop.config import Config
from dolop.dolibarr import Dolibarr
from dolop.dolibarr import adaptateurs as adaptateurs_dol
from dolop.http import ClientHttp
from dolop.modele import Intraduisible
from dolop.openproject import OpenProject
from dolop.openproject import adaptateurs as adaptateurs_op

Reponse = tuple[int, Any] | Callable[[httpx.Request], tuple[int, Any]]


class Serveur:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Reponse]] = []
        self.appels: list[tuple[str, str, Any]] = []

    def route(self, methode: str, motif: str, reponse: Reponse) -> None:
        self.routes.insert(0, (methode, re.compile(motif + r"$"), reponse))

    def __call__(self, requete: httpx.Request) -> httpx.Response:
        corps = json.loads(requete.content) if requete.content else None
        chemin = requete.url.path
        self.appels.append(
            (requete.method, chemin + ("?" + requete.url.query.decode() if requete.url.query else ""), corps)
        )
        for methode, motif, reponse in self.routes:
            if methode == requete.method and motif.search(chemin):
                statut, donnees = reponse(requete) if callable(reponse) else reponse
                return httpx.Response(statut, json=donnees)
        return httpx.Response(599, json={"erreur": f"route non prévue {requete.method} {chemin}"})

    def ecrits(self, methode: str) -> list[tuple[str, Any]]:
        return [(c, b) for m, c, b in self.appels if m == methode]


CONFIG = Config(
    dolibarr_url="http://dolibarr",
    dolibarr_cle="cle",
    openproject_url="http://op",
    openproject_cle="cle",
    exclure_logins=frozenset({"admin"}),
)


def dolibarr(serveur: Serveur) -> tuple[Dolibarr, dict[str, Any]]:
    d = Dolibarr(CONFIG, transport=httpx.MockTransport(serveur))
    d.http.pause = 0
    return d, adaptateurs_dol(d)


def openproject(serveur: Serveur) -> tuple[OpenProject, dict[str, Any]]:
    o = OpenProject(CONFIG, transport=httpx.MockTransport(serveur))
    o.http.pause = 0
    return o, adaptateurs_op(o)


# --------------------------------------------------------------------------- HTTP


def test_lecture_retentee_sur_panne_passagere_mais_pas_lecriture() -> None:
    etapes = iter([(503, {}), (200, {"ok": 1})])
    s = Serveur()
    s.route("GET", "/x", lambda _r: next(etapes))
    s.route("POST", "/y", (503, {}))
    client = ClientHttp("Test", "http://t", transport=httpx.MockTransport(s), pause=0)
    assert client.get("/x") == {"ok": 1}
    with pytest.raises(ErreurApi):
        client.post("/y", {})
    assert len(s.ecrits("POST")) == 1, "une écriture n'est jamais rejouée à l'aveugle"


# ----------------------------------------------------------------------- Dolibarr


def test_dolibarr_404_vaut_absent_mais_403_leve() -> None:
    s = Serveur()
    s.route("GET", "/api/index.php/projects/7", (404, {"error": "not found"}))
    s.route("GET", "/api/index.php/projects/8", (403, {"error": "forbidden"}))
    _, a = dolibarr(s)
    assert a["projet"].lire("7") is None
    with pytest.raises(ErreurApi) as e:
        a["projet"].lire("8")
    assert e.value.statut == 403


def test_dolibarr_projet_canonique() -> None:
    s = Serveur()
    s.route("GET", "/thirdparties", (200, [{"id": "29", "name": "Acme", "code_client": "CU2501-0002"}]))
    projet = {
        "id": "2",
        "ref": "PJ2501-0002",
        "title": "Site Acme",
        "description": "<p>Refonte <strong>complète</strong></p>",
        "socid": "29",
        "status": "2",
        "date_m": 1790000000,
        "array_options": {"options_openproject_id": "41"},
    }
    s.route("GET", "/projects", (200, [projet]))
    _, a = dolibarr(s)
    e = a["projet"].lister()["2"]
    assert e.champs == {
        "titre": "Site Acme",
        "description": "Refonte **complète**",
        "actif": False,
        "client": "Acme",
        "code": "PJ2501-0002",
    }
    assert e.ref_autre == "41"


def test_dolibarr_client_tape_dans_openproject() -> None:
    s = Serveur()
    tiers = [
        {"id": "1", "name": "Château Élégance", "code_client": "CU01"},
        {"id": "2", "name": "Acme", "code_client": "CU02"},
        {"id": "3", "name": "ACME", "code_client": "CU03"},
    ]
    s.route("GET", "/thirdparties", (200, tiers))
    d, _ = dolibarr(s)
    assert d.convertir_client("chateau elegance", "op") == "Château Élégance"
    assert d.convertir_client("cu01", "op") == "Château Élégance"
    with pytest.raises(Intraduisible, match="ambigu"):
        d.convertir_client("acme", "op")
    with pytest.raises(Intraduisible, match="introuvable"):
        d.convertir_client("Inconnu", "op")


def test_dolibarr_temps_cree_retrouve_son_identifiant() -> None:
    s = Serveur()
    lignes = [{"rowid": "1", "task_id": "5", "project_id": "7", "fk_user": "2", "element_duration": "3600"}]

    def lister(requete: httpx.Request) -> tuple[int, Any]:
        return 200, list(lignes)

    def ajouter(requete: httpx.Request) -> tuple[int, Any]:
        corps = json.loads(requete.content)
        lignes.append(
            {
                "rowid": "9",
                "task_id": "5",
                "project_id": "7",
                "fk_user": "2",
                "element_duration": str(corps["duration"]),
            }
        )
        return 200, {"success": {"code": 200}}

    s.route("GET", "/projects/alltimespent", lister)
    s.route("GET", "/tasks", (200, []))
    s.route("POST", "/tasks/5/addtimespent", ajouter)
    _, a = dolibarr(s)
    nouvel = a["temps"].creer(
        {"projet": "7", "tache": "5", "date": "2026-09-24", "duree": 5400, "utilisateur": "2", "note": "Réunion"}, None
    )
    assert nouvel == "9"
    (_, corps), *_ = s.ecrits("POST")
    assert corps == {"date": "2026-09-23 22:00:00", "duration": 5400, "user_id": 2, "note": "Réunion"}


def test_dolibarr_temps_facture_verrouille() -> None:
    s = Serveur()
    s.route("GET", "/projects/alltimespent", (200, [{"rowid": "9", "task_id": "5"}]))
    s.route("GET", "/tasks/5/getTimeSpent/9", (200, {"invoice_id": "12"}))
    _, a = dolibarr(s)
    assert "facture n° 12" in (a["temps"].verrou("9") or "")


def test_dolibarr_temps_exclus_et_hors_tache() -> None:
    s = Serveur()
    s.route("GET", "/users", (200, [{"id": "1", "login": "admin"}, {"id": "2", "login": "alice"}]))
    s.route("GET", "/tasks", (200, [{"id": "50", "fk_project": "7", "array_options": {"options_openproject_id": "-"}}]))
    lignes = [
        {"rowid": "1", "task_id": "50", "project_id": "7", "fk_user": "2", "element_datehour": "2026-09-24 00:00:00"},
        {"rowid": "2", "task_id": "50", "project_id": "7", "fk_user": "1", "element_datehour": "2026-09-24 00:00:00"},
    ]
    s.route("GET", "/projects/alltimespent", (200, lignes))
    _, a = dolibarr(s)
    temps = a["temps"].lister()
    assert list(temps) == ["1"], "les temps du compte admin sont ignorés"
    assert temps["1"].champs["tache"] is None, "la tâche « Temps hors tâche » redevient « sans lot »"
    assert temps["1"].champs["date"] == "2026-09-24"


# -------------------------------------------------------------------- OpenProject


def _page(elements: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"total": len(elements) if total is None else total, "_embedded": {"elements": elements}}


def test_openproject_pagination() -> None:
    s = Serveur()

    def pages(requete: httpx.Request) -> tuple[int, Any]:
        page = int(requete.url.params["offset"])
        return 200, _page([{"id": page * 10 + i} for i in range(2)] if page <= 2 else [], total=4)

    s.route("GET", "/api/v3/roles", pages)
    o, _ = openproject(s)
    assert [r["id"] for r in o.pages("/roles")] == [10, 11, 20, 21]


def test_openproject_temps_ignore_chronometre_et_lit_le_lot() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/users", (200, _page([])))
    s.route("GET", "/api/v3/time_entries/schema", (200, {"customField4": {"name": "ID Dolibarr", "type": "String"}}))
    entrees = [
        {
            "id": 3,
            "spentOn": "2026-09-24",
            "hours": "PT1H30M",
            "comment": {"raw": "Réunion"},
            "customField4": "17",
            "_links": {
                "project": {"href": "/api/v3/projects/5"},
                "entity": {"href": "/api/v3/work_packages/12"},
                "user": {"href": "/api/v3/users/6"},
            },
        },
        {"id": 4, "ongoing": True, "_links": {}},
    ]
    s.route("GET", "/api/v3/time_entries", (200, _page(entrees)))
    s.route("GET", "/api/v3/projects", (200, _page([{"id": 5, "_type": "Project"}])))
    _, a = openproject(s)
    temps = a["temps"].lister()
    assert list(temps) == ["3"]
    assert temps["3"].champs == {
        "projet": "5",
        "tache": "12",
        "date": "2026-09-24",
        "duree": 5400,
        "utilisateur": "6",
        "note": "Réunion",
    }
    assert temps["3"].ref_autre == "17"


def test_openproject_lot_reessaie_sur_conflit_de_version() -> None:
    s = Serveur()
    versions = iter([1, 2])
    s.route("GET", "/api/v3/work_packages/12", lambda _r: (200, {"id": 12, "lockVersion": next(versions)}))
    reponses = iter([(409, {"message": "conflict"}), (200, {"id": 12})])
    s.route("PATCH", "/api/v3/work_packages/12", lambda _r: next(reponses))
    _, a = openproject(s)
    a["tache"].modifier("12", {"titre": "Nouveau"})
    patchs = s.ecrits("PATCH")
    assert [b["lockVersion"] for _, b in patchs] == [1, 2]
    assert patchs[-1][1]["subject"] == "Nouveau"


def test_openproject_identifiant_de_projet_deja_pris() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/projects/schema", (200, {"customField1": {"name": "ID Dolibarr", "type": "String"}}))
    reponses = iter([(422, {"message": "Identifier has already been taken"}), (201, {"id": 33})])
    s.route("POST", "/api/v3/projects", lambda _r: next(reponses))
    _, a = openproject(s)
    assert a["projet"].creer({"titre": "Site", "code": "PJ2501-0006"}, "6") == "33"
    identifiants = [b["identifier"] for _, b in s.ecrits("POST")]
    assert identifiants == ["pj2501-0006", "pj2501-0006-2"]
    assert s.ecrits("POST")[0][1]["customField1"] == "6"


def test_openproject_champ_personnalise_manquant_explique_quoi_faire() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/projects/schema", (200, {"name": {"type": "String"}}))
    o, _ = openproject(s)
    with pytest.raises(ErreurApi, match="Administration"):
        o.valeur_champ("projet", "ID Dolibarr", "3")


def test_openproject_membre_ajoute_avant_de_pointer_pour_quelquun() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/memberships", (200, _page([])))
    s.route("GET", "/api/v3/roles", (200, _page([{"id": 4, "name": "Member"}])))
    s.route("POST", "/api/v3/memberships", (201, {"id": 90}))
    s.route("GET", "/api/v3/time_entries/schema", (200, {"customField4": {"name": "ID Dolibarr", "type": "String"}}))
    s.route("POST", "/api/v3/time_entries", (201, {"id": 77}))
    _, a = openproject(s)
    champs = {"projet": "5", "tache": None, "date": "2026-09-24", "duree": 3600, "utilisateur": "6", "note": ""}
    assert a["temps"].creer(champs, "9") == "77"
    (c1, membre), (c2, temps) = s.ecrits("POST")
    assert c1.endswith("/memberships") and membre["_meta"] == {"sendNotifications": False}
    assert c2.endswith("/time_entries") and temps["hours"] == "PT1H" and temps["customField4"] == "9"
    assert "entity" not in temps["_links"], "temps sans lot : aucun lien vers un lot"
    assert temps["_links"]["project"] == {"href": "/api/v3/projects/5"}


def test_openproject_sans_projet_actif_listes_vides_sans_403() -> None:
    """OpenProject répond 403 aux listes de lots tant qu'aucun projet actif n'existe, même à un admin."""
    s = Serveur()
    s.route("GET", "/api/v3/users", (200, _page([])))
    s.route("GET", "/api/v3/projects", (200, _page([])))
    for route in ("/api/v3/work_packages", "/api/v3/time_entries", "/api/v3/memberships"):
        s.route("GET", route, (403, {"errorIdentifier": "urn:openproject-org:api:v3:errors:MissingPermission"}))
    _, a = openproject(s)
    assert a["tache"].lister() == {} and a["temps"].lister() == {} and a["membre"].lister() == {}
    assert not any("/work_packages" in c or "/time_entries" in c for _, c, _ in s.appels)


def test_openproject_auteur_des_commentaires() -> None:
    s = Serveur()
    s.route("GET", "/api/v3/users", (200, _page([{"id": 6, "login": "amartin", "name": "Alice Martin"}])))
    activites = [
        {
            "_type": "Activity::Comment",
            "comment": {"raw": "Maquette validée"},
            "createdAt": "2026-09-24T08:30:00Z",
            "_links": {"user": {"href": "/api/v3/users/6"}},
        },
        {"_type": "Activity::Comment", "comment": {"raw": "note interne"}, "internal": True, "_links": {}},
        {"_type": "Activity", "comment": {"raw": ""}, "_links": {}},
    ]
    s.route("GET", "/api/v3/work_packages/99/activities", (200, {"_embedded": {"elements": activites}}))
    o, _ = openproject(s)
    (commentaire,) = o.commentaires("99")
    assert commentaire.auteur == "Alice Martin" and commentaire.texte == "Maquette validée"
