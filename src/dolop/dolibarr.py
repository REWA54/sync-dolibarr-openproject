"""Côté Dolibarr (API REST, version 24).

Faits vérifiés dans le code de l'API installée (24.0.0) :
- ``ref_ext`` n'est jamais enregistré pour les projets : l'identifiant OpenProject vit dans un
  attribut supplémentaire (projets et tâches) ;
- ``addtimespent`` ne renvoie pas l'identifiant créé et n'accepte pas ``ref_ext`` ;
- ``/projects/alltimespent`` liste tous les temps en une requête paginée, sans ``invoice_id`` :
  la facturation se vérifie au cas par cas avec ``getTimeSpent`` ;
- le statut d'un utilisateur se change par ``PUT /users/{id}`` avec ``status``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import time
from typing import Any

import httpx

from .adaptateurs import ErreurApi
from .config import Config
from .conversions import (
    arrondir_minute,
    cle_nom,
    date_vers_horodatage,
    date_vers_texte_gmt,
    horodatage_vers_date,
    horodatage_vers_utc,
    html_vers_markdown,
    markdown_vers_html,
    texte_local_vers_date,
    texte_simple,
)
from .cycle import remplacer_bloc
from .http import ClientHttp, Introuvable
from .modele import Enreg, Intraduisible

HORS_TACHE = "-"  # valeur de l'attribut qui marque la tâche « Temps hors tâche »
ROLES_PROJET = {"PROJECTLEADER": "chef", "PROJECTCONTRIBUTOR": "contributeur"}
CODES_ROLE = {v: k for k, v in ROLES_PROJET.items()}
EXECUTANT = "TASKEXECUTIVE"
CLOS = 2


def _int(v: Any) -> int | None:
    if v in (None, "", False):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _txt(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _id(v: Any) -> str | None:
    n = _int(v)
    return str(n) if n else None


class Dolibarr:
    """Client Dolibarr et mémoire de cycle partagée par les adaptateurs."""

    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.fuseau = config.fuseau
        self.attribut = f"options_{config.dolibarr_attribut}"
        self.http = ClientHttp(
            "Dolibarr",
            config.dolibarr_url.rstrip("/") + "/api/index.php",
            entetes={"DOLAPIKEY": config.dolibarr_cle},
            transport=transport,
        )
        self.nouveau_cycle()

    def nouveau_cycle(self) -> None:
        self._tiers: dict[str, dict[str, str]] | None = None
        self._utilisateurs: list[dict[str, Any]] | None = None
        self._projets: list[dict[str, Any]] | None = None
        self._taches: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------------ lectures

    def pages(self, chemin: str, **params: Any) -> list[dict[str, Any]]:
        resultat: list[dict[str, Any]] = []
        for page in range(10_000):
            try:
                lot = self.http.get(chemin, limit=100, page=page, **params)
            except Introuvable:
                # Certaines versions répondent 404 à une page vide. Sur la première page, ce serait
                # une adresse fausse : on laisse l'erreur remonter plutôt que de croire la liste vide.
                if page == 0:
                    raise
                return resultat
            if isinstance(lot, dict):
                lot = lot.get("data", [])
            if not isinstance(lot, list):
                raise ErreurApi(f"Dolibarr GET {chemin} : liste attendue, reçu {type(lot).__name__}")
            resultat.extend(lot)
            if len(lot) < 100:
                return resultat
        raise ErreurApi(f"Dolibarr GET {chemin} : pagination sans fin")

    def utilisateurs(self) -> list[dict[str, Any]]:
        if self._utilisateurs is None:
            self._utilisateurs = self.pages("/users", sortfield="t.rowid")
        return self._utilisateurs

    def exclus(self) -> set[str]:
        """Identifiants des comptes techniques (et de leurs temps), jamais synchronisés."""
        return {
            str(u["id"]) for u in self.utilisateurs() if _txt(u.get("login")).casefold() in self.config.exclure_logins
        }

    def projets(self) -> list[dict[str, Any]]:
        if self._projets is None:
            self._projets = self.pages("/projects", sortfield="t.rowid")
        return self._projets

    def taches(self) -> list[dict[str, Any]]:
        if self._taches is None:
            self._taches = self.pages("/tasks", sortfield="t.rowid")
        return self._taches

    def projets_clos(self) -> set[str]:
        return {str(p["id"]) for p in self.projets() if _int(p.get("status", p.get("statut"))) == CLOS}

    def taches_hors_tache(self) -> dict[str, str]:
        """Tâche « Temps hors tâche » → son projet."""
        return {
            str(t["id"]): str(_int(t.get("fk_project")))
            for t in self.taches()
            if _txt((t.get("array_options") or {}).get(self.attribut)) == HORS_TACHE
        }

    # --------------------------------------------------------------------------- tiers

    def tiers(self) -> dict[str, dict[str, str]]:
        if self._tiers is None:
            self._tiers = {
                str(t["id"]): {"nom": _txt(t.get("name") or t.get("nom")), "code": _txt(t.get("code_client"))}
                for t in self.pages("/thirdparties", sortfield="t.rowid")
            }
        return self._tiers

    def nom_tiers(self, socid: Any) -> str | None:
        ident = _id(socid)
        if ident is None:
            return None
        tiers = self.tiers().get(ident)
        return tiers["nom"] if tiers else None

    def resoudre_client(self, texte: str) -> str:
        """Texte tapé dans OpenProject → nom exact du tiers Dolibarr."""
        cle = cle_nom(texte)
        trouves = {t["nom"] for t in self.tiers().values() if cle in (cle_nom(t["nom"]), cle_nom(t["code"]))}
        if len(trouves) == 1:
            return trouves.pop()
        if not trouves:
            raise Intraduisible(f"client « {texte} » introuvable parmi les tiers Dolibarr (nom ou code client)")
        raise Intraduisible(
            f"client « {texte} » ambigu : plusieurs tiers Dolibarr correspondent ({', '.join(sorted(trouves))})"
        )

    def id_tiers(self, nom: str | None) -> int:
        if not nom:
            return 0
        ids = [i for i, t in self.tiers().items() if t["nom"] == nom]
        if len(ids) != 1:
            ids = [i for i, t in self.tiers().items() if cle_nom(t["nom"]) == cle_nom(nom)]
        if len(ids) != 1:
            raise Intraduisible(f"tiers « {nom} » introuvable ou ambigu dans Dolibarr")
        return int(ids[0])

    def convertir_client(self, valeur: Any, de: str) -> Any:
        if de == "dol" or not valeur:
            return valeur or None
        return self.resoudre_client(str(valeur))

    def ecrire_bloc(self, identifiant: str, bloc_html: str) -> None:
        """Bloc des commentaires OpenProject en fin de note privée de la tâche."""
        tache = self.http.get(f"/tasks/{identifiant}")
        note = remplacer_bloc(tache.get("note_private"), bloc_html)
        if note != (tache.get("note_private") or ""):
            self.http.put(f"/tasks/{identifiant}", {"note_private": note})


class _Base:
    type = ""
    embarque_ref = False  # l'identifiant du jumeau peut-il être rangé dans l'objet ?

    def __init__(self, d: Dolibarr):
        self.d = d

    def cloturer(self, identifiant: str) -> None:
        raise ErreurApi(f"clôture non prévue pour {self.type}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        return None

    def verrou(self, identifiant: str) -> str | None:
        return None

    def refus_suppression(self, identifiant: str) -> str | None:
        return None


def _lire(appel: Callable[[], Any]) -> Any:
    try:
        return appel()
    except Introuvable:
        return None


# ------------------------------------------------------------------------ utilisateurs


class Utilisateurs(_Base):
    type = "utilisateur"

    def _enreg(self, u: Mapping[str, Any]) -> Enreg:
        return Enreg(
            id=str(u["id"]),
            champs={
                "email": _txt(u.get("email")).casefold() or None,
                "prenom": _txt(u.get("firstname")),
                "nom": _txt(u.get("lastname")),
                "actif": _int(u.get("status", u.get("statut"))) == 1,
                "login": _txt(u.get("login")),
            },
            modifie_le=horodatage_vers_utc(u.get("datem") or u.get("date_modification")),
            libelle=_txt(u.get("login")),
        )

    def lister(self) -> dict[str, Enreg]:
        exclus = self.d.exclus()
        return {str(u["id"]): self._enreg(u) for u in self.d.utilisateurs() if str(u["id"]) not in exclus}

    def lire(self, identifiant: str) -> Enreg | None:
        u = _lire(lambda: self.d.http.get(f"/users/{identifiant}"))
        return None if u is None else self._enreg(u)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        if not champs.get("email"):
            raise Intraduisible("e-mail manquant")
        login = champs.get("login") or str(champs["email"]).split("@")[0]
        corps = {
            "login": login,
            "lastname": champs.get("nom") or login,
            "firstname": champs.get("prenom") or "",
            "email": champs["email"],
            "employee": 1,
        }
        identifiant = str(self.d.http.post("/users", corps))
        if champs.get("actif") is False:
            self.d.http.put(f"/users/{identifiant}", {"status": 0})
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        corps: dict[str, Any] = {}
        noms = {"email": "email", "prenom": "firstname", "nom": "lastname"}
        for canonique, natif in noms.items():
            if canonique in champs:
                corps[natif] = champs[canonique] or ""
        if "actif" in champs:
            corps["status"] = 1 if champs["actif"] else 0
        if corps:
            self.d.http.put(f"/users/{identifiant}", corps)
        return None

    def supprimer(self, identifiant: str) -> None:
        self.d.http.delete(f"/users/{identifiant}")


# ----------------------------------------------------------------------------- projets


class Projets(_Base):
    type = "projet"
    embarque_ref = True

    def _enreg(self, p: Mapping[str, Any]) -> Enreg:
        ref = _txt((p.get("array_options") or {}).get(self.d.attribut)) or None
        return Enreg(
            id=str(p["id"]),
            champs={
                "titre": _txt(p.get("title")),
                "description": html_vers_markdown(p.get("description")),
                "actif": _int(p.get("status", p.get("statut"))) != CLOS,
                "client": self.d.nom_tiers(p.get("socid")),
                "code": _txt(p.get("ref")),
            },
            modifie_le=horodatage_vers_utc(p.get("date_m") or p.get("date_modification")),
            ref_autre=ref,
            libelle=f"{_txt(p.get('ref'))} {_txt(p.get('title'))}".strip(),
        )

    def lister(self) -> dict[str, Enreg]:
        return {str(p["id"]): self._enreg(p) for p in self.d.projets()}

    def lire(self, identifiant: str) -> Enreg | None:
        p = _lire(lambda: self.d.http.get(f"/projects/{identifiant}"))
        return None if p is None else self._enreg(p)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        corps: dict[str, Any] = {
            "ref": "auto",
            "title": champs.get("titre") or "(sans titre)",
            "description": markdown_vers_html(champs.get("description")),
            "socid": self.d.id_tiers(champs.get("client")),
            "usage_task": 1,
        }
        if ref_autre:
            corps["array_options"] = {self.d.attribut: ref_autre}
        identifiant = str(self.d.http.post("/projects", corps))
        # Un projet naît brouillon ; on le valide pour pouvoir y saisir du temps.
        self.d.http.post(f"/projects/{identifiant}/validate", {"notrigger": 0})
        if champs.get("actif") is False:
            self.d.http.put(f"/projects/{identifiant}", {"status": CLOS})
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        corps: dict[str, Any] = {}
        if "titre" in champs:
            corps["title"] = champs["titre"] or "(sans titre)"
        if "description" in champs:
            corps["description"] = markdown_vers_html(champs["description"])
        if "client" in champs:
            corps["socid"] = self.d.id_tiers(champs["client"])
        if "actif" in champs:
            actuel = self.d.http.get(f"/projects/{identifiant}")
            clos = _int(actuel.get("status", actuel.get("statut"))) == CLOS
            if champs["actif"] is False and not clos:
                corps["status"] = CLOS
            elif champs["actif"] and clos:
                corps["status"] = 1
        if corps:
            self.d.http.put(f"/projects/{identifiant}", corps)
        return None

    def cloturer(self, identifiant: str) -> None:
        self.modifier(identifiant, {"actif": False})

    def supprimer(self, identifiant: str) -> None:
        self.d.http.delete(f"/projects/{identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        self.d.http.put(f"/projects/{identifiant}", {"array_options": {self.d.attribut: ref_autre or ""}})


# ----------------------------------------------------------------------------- membres


class Membres(_Base):
    """Contacts internes des projets. Identifiant synthétique « projet:utilisateur »."""

    type = "membre"

    def _contacts(self, projet: str) -> dict[str, str]:
        """Utilisateur → rôle canonique (chef l'emporte sur contributeur)."""
        roles: dict[str, str] = {}
        for c in self.d.http.get(f"/projects/{projet}/contacts") or []:
            role = ROLES_PROJET.get(_txt(c.get("code")))
            if c.get("source") != "internal" or role is None:
                continue
            utilisateur = str(c.get("id"))
            if roles.get(utilisateur) != "chef":
                roles[utilisateur] = role
        return roles

    def _enreg(self, projet: str, utilisateur: str, role: str) -> Enreg:
        return Enreg(
            id=f"{projet}:{utilisateur}",
            champs={"projet": projet, "utilisateur": utilisateur, "role": role},
            libelle=f"projet {projet} / utilisateur {utilisateur}",
        )

    def lister(self) -> dict[str, Enreg]:
        exclus, clos = self.d.exclus(), self.d.projets_clos()
        resultat: dict[str, Enreg] = {}
        for p in self.d.projets():
            projet = str(p["id"])
            if projet in clos:
                continue  # projet gelé : on ne lit pas ses membres
            for utilisateur, role in self._contacts(projet).items():
                if utilisateur not in exclus:
                    e = self._enreg(projet, utilisateur, role)
                    resultat[e.id] = e
        return resultat

    def lire(self, identifiant: str) -> Enreg | None:
        projet, utilisateur = identifiant.split(":")
        roles = _lire(lambda: self._contacts(projet))
        if roles is None or utilisateur not in roles:
            return None
        return self._enreg(projet, utilisateur, roles[utilisateur])

    def _ajouter(self, projet: str, utilisateur: str, role: str) -> None:
        corps = {"fk_socpeople": int(utilisateur), "type_contact": CODES_ROLE[role], "source": "internal"}
        self.d.http.post(f"/projects/{projet}/contacts", corps)

    def _retirer(self, projet: str, utilisateur: str, role: str) -> None:
        self.d.http.delete(f"/projects/{projet}/contact/{utilisateur}/{CODES_ROLE[role]}")

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        projet, utilisateur = str(champs["projet"]), str(champs["utilisateur"])
        self._ajouter(projet, utilisateur, champs.get("role") or "contributeur")
        return f"{projet}:{utilisateur}"

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        if "role" not in champs:
            return None
        projet, utilisateur = identifiant.split(":")
        nouveau = champs["role"] or "contributeur"
        for role in ROLES_PROJET.values():
            if role != nouveau and self._contacts(projet).get(utilisateur) == role:
                self._retirer(projet, utilisateur, role)
        if self._contacts(projet).get(utilisateur) != nouveau:
            self._ajouter(projet, utilisateur, nouveau)
        return None

    def supprimer(self, identifiant: str) -> None:
        projet, utilisateur = identifiant.split(":")
        for c in self.d.http.get(f"/projects/{projet}/contacts") or []:
            code = _txt(c.get("code"))
            if c.get("source") == "internal" and str(c.get("id")) == utilisateur and code in ROLES_PROJET:
                self.d.http.delete(f"/projects/{projet}/contact/{utilisateur}/{code}")


# ------------------------------------------------------------------------------ tâches


class Taches(_Base):
    type = "tache"
    embarque_ref = True

    def _assigne(self, identifiant: str) -> str | None:
        """Le dernier intervenant ajouté fait office d'assigné OpenProject."""
        executants = [
            c
            for c in self.d.http.get(f"/tasks/{identifiant}/contacts") or []
            if c.get("source") == "internal" and _txt(c.get("code")) == EXECUTANT
        ]
        if not executants:
            return None
        return str(max(executants, key=lambda c: _int(c.get("rowid")) or 0).get("id"))

    def _enreg(self, t: Mapping[str, Any], *, avec_assigne: bool = True) -> Enreg:
        projet = str(_int(t.get("fk_project")))
        parent = _id(t.get("fk_task_parent"))
        if parent in self.d.taches_hors_tache():
            parent = None
        champs: dict[str, Any] = {
            "projet": projet,
            "parent": parent,
            "titre": _txt(t.get("label")),
            "description": html_vers_markdown(t.get("description")),
            "debut": horodatage_vers_date(t.get("date_start"), self.d.fuseau),
            "fin": horodatage_vers_date(t.get("date_end"), self.d.fuseau),
            "charge": arrondir_minute(_int(t.get("planned_workload")) or 0) or None,
            "avancement": _int(t.get("progress")),
        }
        if avec_assigne:
            champs["assigne"] = self._assigne(str(t["id"]))
        return Enreg(
            id=str(t["id"]),
            champs=champs,
            ref_autre=_txt((t.get("array_options") or {}).get(self.d.attribut)) or None,
            libelle=f"{_txt(t.get('ref'))} {_txt(t.get('label'))}".strip(),
        )

    def lister(self) -> dict[str, Enreg]:
        hors, clos = self.d.taches_hors_tache(), self.d.projets_clos()
        return {
            str(t["id"]): self._enreg(t, avec_assigne=str(_int(t.get("fk_project"))) not in clos)
            for t in self.d.taches()
            if str(t["id"]) not in hors
        }

    def lire(self, identifiant: str) -> Enreg | None:
        t = _lire(lambda: self.d.http.get(f"/tasks/{identifiant}"))
        return None if t is None else self._enreg(t)

    def _corps(self, champs: Mapping[str, Any]) -> dict[str, Any]:
        corps: dict[str, Any] = {}
        if "projet" in champs:
            corps["fk_project"] = int(champs["projet"])
        if "parent" in champs:
            corps["fk_task_parent"] = int(champs["parent"]) if champs["parent"] else 0
        if "titre" in champs:
            corps["label"] = champs["titre"] or "(sans titre)"
        if "description" in champs:
            corps["description"] = markdown_vers_html(champs["description"])
        if "debut" in champs:
            corps["date_start"] = date_vers_horodatage(champs["debut"], self.d.fuseau) or ""
        if "fin" in champs:
            corps["date_end"] = date_vers_horodatage(champs["fin"], self.d.fuseau, time(23, 59)) or ""
        if "charge" in champs:
            corps["planned_workload"] = champs["charge"] or ""
        if "avancement" in champs:
            corps["progress"] = champs["avancement"] if champs["avancement"] is not None else ""
        return corps

    def _affecter(self, identifiant: str, utilisateur: str | None) -> None:
        actuel = self._assigne(identifiant)
        if actuel == utilisateur:
            return
        if actuel is not None:
            self.d.http.delete(f"/tasks/{identifiant}/contacts/{actuel}/{EXECUTANT}")
        if utilisateur is not None:
            deja = [
                c
                for c in self.d.http.get(f"/tasks/{identifiant}/contacts") or []
                if c.get("source") == "internal"
                and _txt(c.get("code")) == EXECUTANT
                and str(c.get("id")) == utilisateur
            ]
            if deja:  # déjà intervenant, mais pas le plus récent : on le remet en tête
                self.d.http.delete(f"/tasks/{identifiant}/contacts/{utilisateur}/{EXECUTANT}")
            corps = {"fk_socpeople": int(utilisateur), "type_contact": EXECUTANT, "source": "internal"}
            self.d.http.post(f"/tasks/{identifiant}/contacts", corps)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        corps = {"ref": "auto", **self._corps(champs)}
        corps.setdefault("label", "(sans titre)")
        if ref_autre:
            corps["array_options"] = {self.d.attribut: ref_autre}
        identifiant = str(self.d.http.post("/tasks", corps))
        if champs.get("assigne"):
            self._affecter(identifiant, str(champs["assigne"]))
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        corps = self._corps(champs)
        if corps:
            self.d.http.put(f"/tasks/{identifiant}", corps)
        if "assigne" in champs:
            self._affecter(identifiant, str(champs["assigne"]) if champs["assigne"] else None)
        return None

    def supprimer(self, identifiant: str) -> None:
        self.d.http.delete(f"/tasks/{identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        self.d.http.put(f"/tasks/{identifiant}", {"array_options": {self.d.attribut: ref_autre or ""}})

    def refus_suppression(self, identifiant: str) -> str | None:
        n = len(self.d.pages("/projects/alltimespent", sqlfilters=f"(t.rowid:=:{int(identifiant)})"))
        return f"{n} temps y sont saisis dans Dolibarr" if n else None

    def hors_tache(self, projet: str) -> str:
        """La tâche qui reçoit, dans Dolibarr, les temps OpenProject saisis sans lot de travail."""
        for tache, p in self.d.taches_hors_tache().items():
            if p == projet:
                return tache
        corps = {
            "ref": "auto",
            "label": self.d.config.tache_hors_tache,
            "fk_project": int(projet),
            "array_options": {self.d.attribut: HORS_TACHE},
        }
        identifiant = str(self.d.http.post("/tasks", corps))
        self.d._taches = None
        return identifiant


# ------------------------------------------------------------------------------- temps


class Temps(_Base):
    type = "temps"

    def __init__(self, d: Dolibarr, taches: Taches):
        super().__init__(d)
        self.taches = taches

    def _enreg(self, row: Mapping[str, Any]) -> Enreg:
        tache = str(_int(row.get("task_id")))
        date = texte_local_vers_date(row.get("element_datehour"))
        if date is None:
            date = self._date_sans_heure(tache, str(row["rowid"]))
        return Enreg(
            id=str(row["rowid"]),
            champs={
                "projet": str(_int(row.get("project_id"))),
                "tache": None if tache in self.d.taches_hors_tache() else tache,
                "date": date,
                "duree": arrondir_minute(_int(row.get("element_duration")) or 0),
                "utilisateur": str(_int(row.get("fk_user"))),
                "note": texte_simple(row.get("time_note")),
            },
            libelle=f"{_txt(row.get('task_label'))} {date} ({_txt(row.get('user_login'))})",
        )

    def _date_sans_heure(self, tache: str, identifiant: str) -> str | None:
        """Anciens temps saisis sans heure : la date n'est donnée que par la tâche."""
        for ligne in self.d.http.get(f"/tasks/{tache}/timespent") or []:
            if str(ligne.get("timespent_line_id")) == identifiant:
                return horodatage_vers_date(ligne.get("timespent_line_date"), self.d.fuseau)
        return None

    def _lignes(self, **params: Any) -> list[dict[str, Any]]:
        return self.d.pages("/projects/alltimespent", sortfield="et.rowid", **params)

    def lister(self) -> dict[str, Enreg]:
        exclus = self.d.exclus()
        return {str(r["rowid"]): self._enreg(r) for r in self._lignes() if str(_int(r.get("fk_user"))) not in exclus}

    def _ligne(self, identifiant: str) -> dict[str, Any] | None:
        lignes = self._lignes(sqlfilters=f"(et.rowid:=:{int(identifiant)})")
        return lignes[0] if lignes else None

    def lire(self, identifiant: str) -> Enreg | None:
        ligne = self._ligne(identifiant)
        return None if ligne is None else self._enreg(ligne)

    def _tache(self, champs: Mapping[str, Any]) -> str:
        if champs.get("tache"):
            return str(champs["tache"])
        return self.taches.hors_tache(str(champs["projet"]))

    def _ids_sur(self, tache: str) -> set[str]:
        return {str(r["rowid"]) for r in self._lignes(sqlfilters=f"(t.rowid:=:{int(tache)})")}

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        tache = self._tache(champs)
        avant = self._ids_sur(tache)
        corps = {
            "date": date_vers_texte_gmt(str(champs["date"]), self.d.fuseau),
            "duration": int(champs.get("duree") or 0),
            "user_id": int(champs["utilisateur"]),
            "note": champs.get("note") or "",
        }
        self.d.http.post(f"/tasks/{tache}/addtimespent", corps)
        nouveaux = [r for r in self._lignes(sqlfilters=f"(t.rowid:=:{int(tache)})") if str(r["rowid"]) not in avant]
        attendus = [
            r
            for r in nouveaux
            if str(_int(r.get("fk_user"))) == str(champs["utilisateur"])
            and arrondir_minute(_int(r.get("element_duration")) or 0) == int(champs.get("duree") or 0)
        ]
        choix = attendus or nouveaux
        if not choix:
            raise ErreurApi("Dolibarr : temps ajouté mais introuvable ensuite")
        return str(max(_int(r["rowid"]) or 0 for r in choix))

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        actuel = self.lire(identifiant)
        if actuel is None:
            raise Introuvable(f"Dolibarr : temps {identifiant} introuvable", 404)
        fusion = {**actuel.champs, **champs}
        if self._tache(fusion) != self._tache(actuel.champs):
            # Changer un temps de tâche n'est pas possible par l'API : on le recrée.
            nouvel = self.creer(fusion, None)
            self.supprimer(identifiant)
            return nouvel
        corps = {
            "date": date_vers_texte_gmt(str(fusion["date"]), self.d.fuseau),
            "duration": int(fusion.get("duree") or 0),
            "user_id": int(fusion["utilisateur"]),
            "note": fusion.get("note") or "",
        }
        self.d.http.put(f"/tasks/{self._tache(actuel.champs)}/timespent/{identifiant}", corps)
        return None

    def supprimer(self, identifiant: str) -> None:
        ligne = self._ligne(identifiant)
        if ligne is None:
            return
        self.d.http.delete(f"/tasks/{_int(ligne.get('task_id'))}/timespent/{identifiant}")

    def verrou(self, identifiant: str) -> str | None:
        ligne = self._ligne(identifiant)
        if ligne is None:
            return None
        detail = self.d.http.get(f"/tasks/{_int(ligne.get('task_id'))}/getTimeSpent/{identifiant}")
        facture = _int((detail or {}).get("invoice_id"))
        return f"temps déjà facturé dans Dolibarr (facture n° {facture})" if facture else None


def adaptateurs(d: Dolibarr) -> dict[str, Any]:
    taches = Taches(d)
    return {
        "utilisateur": Utilisateurs(d),
        "projet": Projets(d),
        "membre": Membres(d),
        "tache": taches,
        "temps": Temps(d, taches),
    }
