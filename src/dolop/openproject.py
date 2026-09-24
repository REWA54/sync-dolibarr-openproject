"""Côté OpenProject (API v3, version 17).

Faits vérifiés dans le code installé (17.8.0) :
- un temps pointe sur son lot par ``_links.entity`` (``workPackage`` est obsolète) ;
- les chronomètres en cours sont des temps ``ongoing`` : ignorés jusqu'à l'arrêt ;
- les champs personnalisés s'appellent ``customFieldN`` ; leur nom se lit dans le schéma
  propre à chaque type (projets, lots, temps) ;
- un lot se modifie avec son ``lockVersion`` ; un assigné ou un auteur de temps doit être
  membre du projet.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from .adaptateurs import ErreurApi
from .config import Config
from .conversions import (
    cle_nom,
    duree_iso_vers_secondes,
    identifiant_op,
    iso_vers_utc,
    secondes_vers_duree_iso,
    texte_simple,
)
from .cycle import Commentaire
from .http import ClientHttp, Introuvable
from .modele import Enreg, Intraduisible

API = "/api/v3"


def _id_lien(lien: Mapping[str, Any] | None, segment: str) -> str | None:
    """« /api/v3/work_packages/12 » → « 12 » si le lien désigne bien ce genre d'objet."""
    href = (lien or {}).get("href")
    if not href or f"/{segment}/" not in href:
        return None
    return str(href.rstrip("/").rsplit("/", 1)[-1])


def _lien(segment: str, identifiant: str | None) -> dict[str, str | None]:
    return {"href": f"{API}/{segment}/{identifiant}" if identifiant else None}


def _brut(valeur: Any) -> str:
    if isinstance(valeur, dict):
        valeur = valeur.get("raw")
    return "" if valeur is None else str(valeur)


class OpenProject:
    """Client OpenProject et mémoire de cycle partagée par les adaptateurs."""

    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        entetes: dict[str, str] = {}
        if config.openproject_hote:
            entetes = {"Host": config.openproject_hote, "X-Forwarded-Proto": "https"}
        self.http = ClientHttp(
            "OpenProject",
            config.openproject_url.rstrip("/") + API,
            entetes=entetes,
            auth=("apikey", config.openproject_cle),
            transport=transport,
        )
        self._champs: dict[tuple[str, str], tuple[str, str]] = {}
        self._schemas_vus: set[tuple[str, str, str | None]] = set()
        self.nouveau_cycle()

    def nouveau_cycle(self) -> None:
        self._schemas_vus = set()  # un champ créé entre deux cycles est retrouvé au suivant
        self._utilisateurs: list[dict[str, Any]] | None = None
        self._membres: set[tuple[str, str]] | None = None
        self._roles: dict[str, str] | None = None
        self._type: str | None = None
        self._projets: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------------ lectures

    def pages(self, chemin: str, filtres: list[Any] | None = None, **params: Any) -> list[dict[str, Any]]:
        if filtres is not None:
            params["filters"] = json.dumps(filtres)
        resultat: list[dict[str, Any]] = []
        for page in range(1, 10_000):
            reponse = self.http.get(chemin, pageSize=500, offset=page, **params)
            elements = (reponse.get("_embedded") or {}).get("elements") or []
            resultat.extend(elements)
            total = reponse.get("total")
            if not elements or (total is not None and len(resultat) >= int(total)):
                return resultat
        raise ErreurApi(f"OpenProject GET {chemin} : pagination sans fin")

    def projets(self, actifs: bool) -> list[dict[str, Any]]:
        cle = "t" if actifs else "f"
        if cle not in self._projets:
            self._projets[cle] = [
                p
                for p in self.pages("/projects", [{"active": {"operator": "=", "values": [cle]}}])
                if p.get("_type", "Project") == "Project"
            ]
        return self._projets[cle]

    def aucun_projet_actif(self) -> bool:
        """OpenProject refuse (403) les listes de lots, rôles, types… tant qu'aucun projet actif
        n'existe, même à un administrateur. Or sans projet actif, ces listes sont vides par nature."""
        return not self.projets(actifs=True)

    def utilisateurs(self) -> list[dict[str, Any]]:
        if self._utilisateurs is None:
            self._utilisateurs = self.pages("/users")
        return self._utilisateurs

    def nom_utilisateur(self, identifiant: str | None) -> str:
        """Le lien « user » d'une activité n'a pas de titre : le nom se lit dans la liste des utilisateurs."""
        for u in self.utilisateurs():
            if str(u["id"]) == identifiant:
                return str(u.get("name") or u.get("login") or identifiant)
        return "?"

    def exclus(self) -> set[str]:
        return {
            str(u["id"])
            for u in self.utilisateurs()
            if str(u.get("login", "")).casefold() in self.config.exclure_logins
        }

    # --------------------------------------------------------- champs personnalisés

    def _schema(self, genre: str, projet: str | None = None) -> dict[str, Any]:
        if genre == "projet":
            return dict(self.http.get("/projects/schema"))
        if genre == "temps":
            return dict(self.http.get("/time_entries/schema"))
        if projet is None:
            return {}
        return dict(self.http.get(f"/work_packages/schemas/{projet}-{self.type_par_defaut()}"))

    def champ(self, genre: str, nom: str, projet: str | None = None) -> tuple[str, str] | None:
        """(« customField7 », type) du champ ``nom`` pour ce genre d'objet, s'il existe."""
        cle = (genre, nom)
        # L'identifiant customFieldN est global : trouvé une fois, il vaut partout. Un échec, lui,
        # ne vaut que pour le schéma consulté (le champ peut manquer dans un seul projet).
        if cle not in self._champs and (genre, nom, projet) not in self._schemas_vus:
            self._schemas_vus.add((genre, nom, projet))
            for propriete, desc in self._schema(genre, projet).items():
                personnalise = propriete.startswith("customField") and isinstance(desc, dict)
                if personnalise and cle_nom(desc.get("name")) == cle_nom(nom):
                    self._champs[cle] = (propriete, str(desc.get("type", "String")))
                    break
        return self._champs.get(cle)

    def champ_obligatoire(self, genre: str, nom: str, projet: str | None = None) -> tuple[str, str]:
        trouve = self.champ(genre, nom, projet)
        if trouve is None:
            quoi = {"projet": "les projets", "lot": "les lots de travail", "temps": "les temps"}[genre]
            raise ErreurApi(
                f"OpenProject : champ personnalisé « {nom} » introuvable pour {quoi}. "
                "Le créer (Administration → Champs personnalisés), coché « pour tous les projets » et tous les types."
            )
        return trouve

    def lire_champ(self, objet: Mapping[str, Any], genre: str, nom: str, projet: str | None = None) -> str | None:
        trouve = self.champ(genre, nom, projet)
        if trouve is None:
            return None
        return _brut(objet.get(trouve[0])).strip() or None

    def valeur_champ(self, genre: str, nom: str, valeur: str | None, projet: str | None = None) -> dict[str, Any]:
        propriete, type_ = self.champ_obligatoire(genre, nom, projet)
        if type_.casefold() == "formattable":
            return {propriete: {"raw": valeur or ""}}
        return {propriete: valeur or None}

    # --------------------------------------------------------------- référentiels

    def roles(self) -> dict[str, str]:
        if self._roles is None:
            self._roles = {cle_nom(r.get("name")): str(r["id"]) for r in self.pages("/roles")}
        return self._roles

    def role(self, canonique: str) -> str:
        nom = self.config.op_role_chef if canonique == "chef" else self.config.op_role_contributeur
        ident = self.roles().get(cle_nom(nom))
        if ident is None:
            raise ErreurApi(f"OpenProject : rôle « {nom} » introuvable (connus : {', '.join(sorted(self.roles()))})")
        return ident

    def type_par_defaut(self) -> str:
        if self._type is None:
            types = {cle_nom(t.get("name")): str(t["id"]) for t in self.pages("/types")}
            ident = types.get(cle_nom(self.config.op_type_tache))
            if ident is None:
                raise ErreurApi(f"OpenProject : type « {self.config.op_type_tache} » introuvable ({', '.join(types)})")
            self._type = ident
        return self._type

    def membres(self) -> set[tuple[str, str]]:
        if self._membres is None:
            self._membres = set()
            for m in self.pages("/memberships"):
                projet = _id_lien(m["_links"].get("project"), "projects")
                utilisateur = _id_lien(m["_links"].get("principal"), "users")
                if projet and utilisateur:
                    self._membres.add((projet, utilisateur))
        return self._membres

    def creer_membre(self, projet: str, utilisateur: str, role: str) -> str:
        corps = {
            "_links": {
                "project": _lien("projects", projet),
                "principal": _lien("users", utilisateur),
                "roles": [_lien("roles", self.role(role))],
            },
            "_meta": {"sendNotifications": False},
        }
        reponse = self.http.post("/memberships", corps)
        self.membres().add((projet, utilisateur))
        return str(reponse["id"])

    def assurer_membre(self, projet: str | None, utilisateur: str | None) -> None:
        """OpenProject refuse d'assigner ou de pointer pour quelqu'un qui n'est pas membre du projet."""
        if projet and utilisateur and (projet, utilisateur) not in self.membres():
            self.creer_membre(projet, utilisateur, "contributeur")

    def commentaires(self, identifiant: str) -> list[Commentaire]:
        reponse = self.http.get(f"/work_packages/{identifiant}/activities")
        resultat = []
        for a in (reponse.get("_embedded") or {}).get("elements") or []:
            texte = _brut(a.get("comment")).strip()
            if a.get("_type") != "Activity::Comment" or not texte or a.get("internal"):
                continue
            date = iso_vers_utc(a.get("createdAt")) or datetime.min.replace(tzinfo=UTC)
            auteur = self.nom_utilisateur(_id_lien((a.get("_links") or {}).get("user"), "users"))
            resultat.append(Commentaire(date, auteur, texte))
        return resultat


class _Base:
    type = ""
    embarque_ref = False  # l'identifiant du jumeau peut-il être rangé dans l'objet ?

    def __init__(self, o: OpenProject):
        self.o = o

    def _lire(self, chemin: str) -> dict[str, Any] | None:
        try:
            return dict(self.o.http.get(chemin))
        except Introuvable:
            return None

    def cloturer(self, identifiant: str) -> None:
        raise ErreurApi(f"clôture non prévue pour {self.type}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        return None

    def verrou(self, identifiant: str) -> str | None:
        return None

    def refus_suppression(self, identifiant: str) -> str | None:
        return None


# ------------------------------------------------------------------------ utilisateurs


class Utilisateurs(_Base):
    type = "utilisateur"

    def _enreg(self, u: Mapping[str, Any]) -> Enreg:
        return Enreg(
            id=str(u["id"]),
            champs={
                "email": str(u.get("email") or "").strip().casefold() or None,
                "prenom": str(u.get("firstName") or "").strip(),
                "nom": str(u.get("lastName") or "").strip(),
                "actif": u.get("status") != "locked",
                "login": str(u.get("login") or ""),
            },
            modifie_le=iso_vers_utc(u.get("updatedAt")),
            libelle=str(u.get("login") or u.get("name") or u["id"]),
        )

    def lister(self) -> dict[str, Enreg]:
        exclus = self.o.exclus()
        return {str(u["id"]): self._enreg(u) for u in self.o.utilisateurs() if str(u["id"]) not in exclus}

    def lire(self, identifiant: str) -> Enreg | None:
        u = self._lire(f"/users/{identifiant}")
        return None if u is None else self._enreg(u)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        if not champs.get("email"):
            raise Intraduisible("e-mail manquant : impossible d'inviter cette personne dans OpenProject")
        corps = {
            "login": champs.get("login") or champs["email"],
            "email": champs["email"],
            "firstName": champs.get("prenom") or "-",
            "lastName": champs.get("nom") or "-",
            "status": "invited",  # OpenProject envoie lui-même l'e-mail d'invitation
        }
        identifiant = str(self.o.http.post("/users", corps)["id"])
        if champs.get("actif") is False:
            self.o.http.post(f"/users/{identifiant}/lock")
        return identifiant

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        corps = {
            natif: champs[canonique] or "-"
            for canonique, natif in (("email", "email"), ("prenom", "firstName"), ("nom", "lastName"))
            if canonique in champs
        }
        if corps:
            self.o.http.patch(f"/users/{identifiant}", corps)
        if "actif" in champs:
            actuel = self.o.http.get(f"/users/{identifiant}")
            if champs["actif"] is False and actuel.get("status") != "locked":
                self.o.http.post(f"/users/{identifiant}/lock")
            elif champs["actif"] and actuel.get("status") == "locked":
                self.o.http.delete(f"/users/{identifiant}/lock")
        return None

    def supprimer(self, identifiant: str) -> None:
        self.o.http.delete(f"/users/{identifiant}")


# ----------------------------------------------------------------------------- projets


class Projets(_Base):
    type = "projet"
    embarque_ref = True

    def _enreg(self, p: Mapping[str, Any]) -> Enreg:
        cfg = self.o.config
        return Enreg(
            id=str(p["id"]),
            champs={
                "titre": str(p.get("name") or "").strip(),
                "description": _brut(p.get("description")).strip(),
                "actif": bool(p.get("active", True)),
                "client": self.o.lire_champ(p, "projet", cfg.op_champ_client),
                "code": str(p.get("identifier") or ""),
            },
            modifie_le=iso_vers_utc(p.get("updatedAt")),
            ref_autre=self.o.lire_champ(p, "projet", cfg.op_champ_ref),
            libelle=str(p.get("name") or p["id"]),
        )

    def lister(self) -> dict[str, Enreg]:
        projets = [*self.o.projets(actifs=True), *self.o.projets(actifs=False)]
        return {str(p["id"]): self._enreg(p) for p in projets}

    def lire(self, identifiant: str) -> Enreg | None:
        p = self._lire(f"/projects/{identifiant}")
        return None if p is None else self._enreg(p)

    def _corps(self, champs: Mapping[str, Any]) -> dict[str, Any]:
        corps: dict[str, Any] = {}
        if "titre" in champs:
            corps["name"] = champs["titre"] or "(sans titre)"
        if "description" in champs:
            corps["description"] = {"raw": champs["description"] or ""}
        if "actif" in champs:
            corps["active"] = bool(champs["actif"])
        if "client" in champs:
            corps.update(self.o.valeur_champ("projet", self.o.config.op_champ_client, champs["client"]))
        return corps

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        corps = self._corps(champs)
        corps.setdefault("name", "(sans titre)")
        if ref_autre:
            corps.update(self.o.valeur_champ("projet", self.o.config.op_champ_ref, ref_autre))
        base = identifiant_op(champs.get("code"), str(corps["name"]))
        for n in range(1, 10):
            corps["identifier"] = base if n == 1 else f"{base[:96]}-{n}"
            try:
                identifiant = str(self.o.http.post("/projects", corps)["id"])
                self.o._projets.clear()  # la suite du cycle doit voir ce nouveau projet actif
                return identifiant
            except ErreurApi as e:
                if e.statut != 422 or "identifier" not in str(e).casefold():
                    raise
        raise ErreurApi(f"OpenProject : aucun identifiant libre pour « {base} »")

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        corps = self._corps(champs)
        if corps:
            self.o.http.patch(f"/projects/{identifiant}", corps)
        return None

    def cloturer(self, identifiant: str) -> None:
        self.o.http.patch(f"/projects/{identifiant}", {"active": False})

    def supprimer(self, identifiant: str) -> None:
        self.o.http.delete(f"/projects/{identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        self.o.http.patch(
            f"/projects/{identifiant}", self.o.valeur_champ("projet", self.o.config.op_champ_ref, ref_autre)
        )


# ----------------------------------------------------------------------------- membres


class Membres(_Base):
    type = "membre"

    def _enreg(self, m: Mapping[str, Any]) -> Enreg | None:
        liens = m.get("_links") or {}
        projet = _id_lien(liens.get("project"), "projects")
        utilisateur = _id_lien(liens.get("principal"), "users")
        if not projet or not utilisateur:
            return None  # groupe ou utilisateur fictif : pas d'équivalent Dolibarr
        chef = cle_nom(self.o.config.op_role_chef)
        roles = {cle_nom(r.get("title")) for r in liens.get("roles") or []}
        return Enreg(
            id=str(m["id"]),
            champs={
                "projet": projet,
                "utilisateur": utilisateur,
                "role": "chef" if chef in roles else "contributeur",
            },
            libelle=f"{(liens.get('principal') or {}).get('title')} / {(liens.get('project') or {}).get('title')}",
        )

    def lister(self) -> dict[str, Enreg]:
        if self.o.aucun_projet_actif():
            return {}
        exclus = self.o.exclus()
        resultat: dict[str, Enreg] = {}
        for m in self.o.pages("/memberships"):
            e = self._enreg(m)
            if e is not None and e.champs["utilisateur"] not in exclus:
                resultat[e.id] = e
        return resultat

    def lire(self, identifiant: str) -> Enreg | None:
        m = self._lire(f"/memberships/{identifiant}")
        return None if m is None else self._enreg(m)

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        return self.o.creer_membre(
            str(champs["projet"]), str(champs["utilisateur"]), champs.get("role") or "contributeur"
        )

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        if "role" in champs:
            corps = {"_links": {"roles": [_lien("roles", self.o.role(champs["role"] or "contributeur"))]}}
            self.o.http.patch(f"/memberships/{identifiant}", corps)
        return None

    def supprimer(self, identifiant: str) -> None:
        self.o.http.delete(f"/memberships/{identifiant}")


# ------------------------------------------------------------------ lots de travail


class Lots(_Base):
    type = "tache"
    embarque_ref = True

    def _enreg(self, w: Mapping[str, Any]) -> Enreg:
        liens = w.get("_links") or {}
        projet = _id_lien(liens.get("project"), "projects")
        debut, fin = w.get("startDate"), w.get("dueDate")
        if "date" in w and not (debut or fin):  # jalon : une seule date
            debut = fin = w.get("date")
        return Enreg(
            id=str(w["id"]),
            champs={
                "projet": projet,
                "parent": _id_lien(liens.get("parent"), "work_packages"),
                "titre": str(w.get("subject") or "").strip(),
                "description": _brut(w.get("description")).strip(),
                "debut": debut,
                "fin": fin,
                "charge": duree_iso_vers_secondes(w.get("estimatedTime")),
                "avancement": w.get("percentageDone"),
                "assigne": _id_lien(liens.get("assignee"), "users"),
            },
            modifie_le=iso_vers_utc(w.get("updatedAt")),
            ref_autre=self.o.lire_champ(w, "lot", self.o.config.op_champ_ref, projet),
            libelle=f"#{w['id']} {w.get('subject') or ''}".strip(),
        )

    def lister(self) -> dict[str, Enreg]:
        if self.o.aucun_projet_actif():
            return {}
        lots = self.o.pages("/work_packages", [], sortBy=json.dumps([["id", "asc"]]))
        return {str(w["id"]): self._enreg(w) for w in lots}

    def lire(self, identifiant: str) -> Enreg | None:
        w = self._lire(f"/work_packages/{identifiant}")
        return None if w is None else self._enreg(w)

    def _corps(self, champs: Mapping[str, Any]) -> dict[str, Any]:
        corps: dict[str, Any] = {}
        liens: dict[str, Any] = {}
        if "projet" in champs:
            liens["project"] = _lien("projects", champs["projet"])
        if "parent" in champs:
            liens["parent"] = _lien("work_packages", champs["parent"])
        if "assigne" in champs:
            liens["assignee"] = _lien("users", champs["assigne"])
        if "titre" in champs:
            corps["subject"] = champs["titre"] or "(sans titre)"
        if "description" in champs:
            corps["description"] = {"raw": champs["description"] or ""}
        if "debut" in champs:
            corps["startDate"] = champs["debut"]
        if "fin" in champs:
            corps["dueDate"] = champs["fin"]
        if "charge" in champs:
            corps["estimatedTime"] = secondes_vers_duree_iso(champs["charge"])
        if "avancement" in champs:
            corps["percentageDone"] = champs["avancement"]
        if liens:
            corps["_links"] = liens
        return corps

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        projet = str(champs["projet"])
        self.o.assurer_membre(projet, champs.get("assigne"))
        corps = self._corps(champs)
        corps.setdefault("subject", "(sans titre)")
        corps.setdefault("_links", {})["type"] = _lien("types", self.o.type_par_defaut())
        if ref_autre:
            corps.update(self.o.valeur_champ("lot", self.o.config.op_champ_ref, ref_autre, projet))
        return str(self.o.http.post("/work_packages", corps)["id"])

    def _patch(self, identifiant: str, corps: dict[str, Any]) -> None:
        for essai in (1, 2):
            actuel = self.o.http.get(f"/work_packages/{identifiant}")
            try:
                self.o.http.patch(f"/work_packages/{identifiant}", {**corps, "lockVersion": actuel["lockVersion"]})
                return
            except ErreurApi as e:
                if e.statut != 409 or essai == 2:
                    raise

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        if "assigne" in champs:
            projet = champs.get("projet") or (self.lire(identifiant) or Enreg("", {})).champs.get("projet")
            self.o.assurer_membre(projet, champs["assigne"])
        corps = self._corps(champs)
        if corps:
            self._patch(identifiant, corps)
        return None

    def supprimer(self, identifiant: str) -> None:
        self.o.http.delete(f"/work_packages/{identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        w = self.o.http.get(f"/work_packages/{identifiant}")
        projet = _id_lien((w.get("_links") or {}).get("project"), "projects")
        self._patch(identifiant, self.o.valeur_champ("lot", self.o.config.op_champ_ref, ref_autre, projet))

    def refus_suppression(self, identifiant: str) -> str | None:
        w = self._lire(f"/work_packages/{identifiant}")
        passe = duree_iso_vers_secondes((w or {}).get("spentTime")) or 0
        return f"{passe / 3600:g} h y sont pointées dans OpenProject" if passe else None


# ------------------------------------------------------------------------------- temps


class Temps(_Base):
    type = "temps"
    embarque_ref = True

    def _enreg(self, t: Mapping[str, Any]) -> Enreg:
        liens = t.get("_links") or {}
        return Enreg(
            id=str(t["id"]),
            champs={
                "projet": _id_lien(liens.get("project"), "projects"),
                "tache": _id_lien(liens.get("entity") or liens.get("workPackage"), "work_packages"),
                "date": t.get("spentOn"),
                "duree": duree_iso_vers_secondes(t.get("hours")) or 0,
                "utilisateur": _id_lien(liens.get("user"), "users"),
                "note": texte_simple(_brut(t.get("comment"))),
            },
            modifie_le=iso_vers_utc(t.get("updatedAt")),
            ref_autre=self.o.lire_champ(t, "temps", self.o.config.op_champ_ref),
            libelle=f"{t.get('spentOn')} {(liens.get('user') or {}).get('title', '')}".strip(),
        )

    def lister(self) -> dict[str, Enreg]:
        if self.o.aucun_projet_actif():
            return {}
        exclus = self.o.exclus()
        resultat: dict[str, Enreg] = {}
        for t in self.o.pages("/time_entries", sortBy=json.dumps([["id", "asc"]])):
            if t.get("ongoing"):
                continue  # chronomètre en cours : synchronisé à l'arrêt
            e = self._enreg(t)
            if e.champs["utilisateur"] not in exclus:
                resultat[e.id] = e
        return resultat

    def lire(self, identifiant: str) -> Enreg | None:
        t = self._lire(f"/time_entries/{identifiant}")
        return None if t is None else self._enreg(t)

    def _corps(self, champs: Mapping[str, Any]) -> dict[str, Any]:
        corps: dict[str, Any] = {}
        liens: dict[str, Any] = {}
        if "projet" in champs:
            liens["project"] = _lien("projects", champs["projet"])
        if "tache" in champs:
            liens["entity"] = _lien("work_packages", champs["tache"])
        if "utilisateur" in champs:
            liens["user"] = _lien("users", champs["utilisateur"])
        if "date" in champs:
            corps["spentOn"] = champs["date"]
        if "duree" in champs:
            corps["hours"] = secondes_vers_duree_iso(champs["duree"] or 0)
        if "note" in champs:
            corps["comment"] = {"raw": champs["note"] or ""}
        if liens:
            corps["_links"] = liens
        return corps

    def creer(self, champs: Mapping[str, Any], ref_autre: str | None) -> str:
        self.o.assurer_membre(champs.get("projet"), champs.get("utilisateur"))
        corps = self._corps(champs)
        if not champs.get("tache"):
            corps.get("_links", {}).pop("entity", None)  # temps sans lot : rattaché au seul projet
        if self.o.config.op_activite:
            corps.setdefault("_links", {})["activity"] = {
                "href": f"{API}/time_entries/activities/{self.o.config.op_activite}"
            }
        if ref_autre:
            corps.update(self.o.valeur_champ("temps", self.o.config.op_champ_ref, ref_autre))
        return str(self.o.http.post("/time_entries", corps)["id"])

    def modifier(self, identifiant: str, champs: Mapping[str, Any]) -> str | None:
        if "utilisateur" in champs or "projet" in champs:
            actuel = self.lire(identifiant)
            base = dict(actuel.champs) if actuel else {}
            self.o.assurer_membre(
                champs.get("projet", base.get("projet")), champs.get("utilisateur", base.get("utilisateur"))
            )
        corps = self._corps(champs)
        if corps:
            self.o.http.patch(f"/time_entries/{identifiant}", corps)
        return None

    def supprimer(self, identifiant: str) -> None:
        self.o.http.delete(f"/time_entries/{identifiant}")

    def poser_ref_autre(self, identifiant: str, ref_autre: str | None) -> None:
        self.o.http.patch(
            f"/time_entries/{identifiant}",
            self.o.valeur_champ("temps", self.o.config.op_champ_ref, ref_autre),
        )


def adaptateurs(o: OpenProject) -> dict[str, Any]:
    return {
        "utilisateur": Utilisateurs(o),
        "projet": Projets(o),
        "membre": Membres(o),
        "tache": Lots(o),
        "temps": Temps(o),
    }
