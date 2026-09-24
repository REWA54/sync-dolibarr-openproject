"""« dolop verifier » face à un faux Dolibarr et un faux OpenProject : ce qu'il dit, et ce qu'il conseille."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from test_adaptateurs import CONFIG, Serveur

from dolop import verification
from dolop.dolibarr import Dolibarr
from dolop.openproject import OpenProject
from dolop.verification import rendre, verifier

TOUS_LES_DROITS = {
    "projet": {"lire": 1, "creer": 1, "supprimer": 1, "all": {"lire": 1, "creer": 1, "supprimer": 1}},
    "societe": {"lire": 1, "client": {"voir": 1}},
    "user": {"user": {"lire": 1, "creer": 1, "supprimer": 1}},
}


def _liste(*elements: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200, {"_embedded": {"elements": list(elements)}, "total": len(elements)}


def serveur_pret() -> Serveur:
    s = Serveur()
    d = "/api/index.php"
    s.route("GET", f"{d}/users/info", (200, {"login": "sync", "rights": TOUS_LES_DROITS}))
    attribut = {"openproject_id": {"label": "ID OpenProject"}}
    s.route("GET", f"{d}/setup/extrafields", (200, {"projet": attribut, "projet_task": attribut}))
    for chemin in ("projects", "tasks", "thirdparties", "projects/alltimespent"):
        s.route("GET", f"{d}/{chemin}", (200, []))
    o = "/api/v3"
    s.route("GET", f"{o}/users/me", (200, {"login": "sync", "admin": True}))
    s.route("GET", f"{o}/projects", _liste({"id": 3, "_type": "Project"}))
    champs = {"customField1": {"name": "Client"}, "customField2": {"name": "ID Dolibarr"}}
    s.route("GET", f"{o}/projects/schema", (200, champs))
    s.route("GET", f"{o}/time_entries/schema", (200, {"customField3": {"name": "ID Dolibarr"}}))
    s.route("GET", f"{o}/types", _liste({"id": 1, "name": "Task"}))
    s.route("GET", f"{o}/roles", _liste({"id": 3, "name": "Project admin"}, {"id": 4, "name": "Member"}))
    s.route("GET", f"{o}/work_packages/schemas/3-1", (200, {"customField4": {"name": "ID Dolibarr"}}))
    return s


@pytest.fixture
def brancher(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _brancher(s: Serveur) -> None:
        transport = httpx.MockTransport(s)
        monkeypatch.setattr(verification, "Dolibarr", lambda c: Dolibarr(c, transport=transport))
        monkeypatch.setattr(verification, "OpenProject", lambda c: OpenProject(c, transport=transport))

    return _brancher


CONFIG_PRETE = replace(CONFIG, exclure_logins=frozenset({"admin", "sync"}))


def test_installation_complete_tout_vert(brancher: Any) -> None:
    s = serveur_pret()
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG_PRETE)))
    assert tout_bon, texte
    assert "❌" not in texte and "⏳" not in texte
    assert all(m == "GET" for m, _, _ in s.appels), "vérifier ne fait que lire"


def test_compte_technique_non_exclu_et_droits_manquants_expliques(brancher: Any) -> None:
    s = serveur_pret()
    droits = {**TOUS_LES_DROITS, "user": {"user": {"lire": 1}}}
    s.route("GET", "/api/index.php/users/info", (200, {"login": "sync", "rights": droits}))
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG)))
    assert not tout_bon
    assert "ajouter sync à EXCLURE_LOGINS" in texte
    assert "Utilisateurs — créer/modifier" in texte


def test_champ_personnalise_absent_dit_ou_le_creer(brancher: Any) -> None:
    s = serveur_pret()
    s.route("GET", "/api/v3/projects/schema", (200, {"customField1": {"name": "Client"}}))
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG_PRETE)))
    assert not tout_bon
    assert "/admin/settings/project_custom_fields" in texte


def test_sans_projet_actif_les_controles_dependants_sont_en_attente(brancher: Any) -> None:
    s = serveur_pret()
    s.route("GET", "/api/v3/projects", _liste())
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG_PRETE)))
    assert tout_bon, texte
    assert "⏳" in texte


def test_jeton_refuse(brancher: Any) -> None:
    s = serveur_pret()
    s.route("GET", "/api/index.php/users/info", (401, {"error": "unauthorized"}))
    s.route("GET", "/api/v3/users/me", (401, {"error": "unauthorized"}))
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG_PRETE)))
    assert not tout_bon
    assert "DOLIBARR_API_KEY" in texte and "OPENPROJECT_API_KEY" in texte


def test_champ_des_lots_controle_dans_chaque_projet(brancher: Any) -> None:
    """Vécu : champ non coché « pour tous les projets », absent des projets créés par la synchronisation."""
    s = serveur_pret()
    s.route(
        "GET",
        "/api/v3/projects",
        _liste(
            {"id": 3, "_type": "Project", "name": "Ancien projet"},
            {"id": 9, "_type": "Project", "name": "Site vitrine"},
            {"id": 11, "_type": "Project", "name": "Refonte"},
        ),
    )
    s.route("GET", "/api/v3/work_packages/schemas/9-1", (200, {"subject": {"name": "Sujet"}}))
    s.route("GET", "/api/v3/work_packages/schemas/11-1", (404, {"message": "introuvable"}))
    brancher(s)
    texte, tout_bon = rendre(list(verifier(CONFIG_PRETE)))
    assert not tout_bon
    assert "« Pour tous les projets »" in texte
    assert "« Site vitrine »" in texte and "« Refonte » (type « Task » non activé" in texte
    assert "Ancien projet" not in texte, "le projet qui a le champ n'est pas cité"
