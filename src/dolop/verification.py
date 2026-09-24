"""« dolop verifier » : chaque prérequis de la mise en route, vérifié en lecture seule."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any

from .adaptateurs import ErreurApi
from .config import Config
from .conversions import cle_nom
from .dolibarr import Dolibarr
from .http import Introuvable
from .openproject import OpenProject

# Droits Dolibarr du compte technique : (module, droit, sous-droit, libellé de l'écran Permissions).
DROITS_DOLIBARR: tuple[tuple[str, str, str | None, str], ...] = (
    ("projet", "lire", None, "Projets — lire"),
    ("projet", "creer", None, "Projets — créer/modifier"),
    ("projet", "supprimer", None, "Projets — supprimer"),
    ("projet", "all", "lire", "Projets — lire tous les projets"),
    ("projet", "all", "creer", "Projets — créer/modifier tous les projets"),
    ("projet", "all", "supprimer", "Projets — supprimer tous les projets"),
    ("societe", "lire", None, "Tiers — lire"),
    ("societe", "client", "voir", "Tiers — voir tous les tiers"),
    ("user", "user", "lire", "Utilisateurs — lire les autres utilisateurs"),
    ("user", "user", "creer", "Utilisateurs — créer/modifier"),
    ("user", "user", "supprimer", "Utilisateurs — supprimer ou désactiver"),
)


@dataclass(frozen=True)
class Point:
    ok: bool
    libelle: str
    conseil: str = ""
    # Invérifiable pour l'instant (pas encore de projet actif dans OpenProject) : ni vert ni rouge.
    attente: bool = False


def _droit(droits: Any, module: str, droit: str, sous: str | None) -> bool:
    valeur = (droits or {}).get(module, {})
    valeur = valeur.get(droit) if isinstance(valeur, dict) else None
    if sous is not None:
        valeur = valeur.get(sous) if isinstance(valeur, dict) else None
    return bool(valeur) and not isinstance(valeur, dict)


def verifier(config: Config) -> Iterator[Point]:
    yield from _dolibarr(config)
    yield from _openproject(config)


def _essai(libelle: str, conseil: str, appel: Callable[[], Any]) -> tuple[Point, Any]:
    try:
        return Point(True, libelle), appel()
    except ErreurApi as e:
        return Point(False, libelle, f"{conseil} ({str(e)[:160]})"), None


def _dolibarr(config: Config) -> Iterator[Point]:
    d = Dolibarr(config)
    point, info = _essai(
        "Dolibarr : connexion avec le jeton",
        "vérifier DOLIBARR_URL et DOLIBARR_API_KEY",
        lambda: d.http.get("/users/info", includepermissions=1),
    )
    yield point
    if info is None:
        return
    login = str(info.get("login"))
    yield Point(
        login.casefold() in config.exclure_logins,
        f"Dolibarr : le compte technique « {login} » est exclu de la synchronisation",
        f"ajouter {login} à EXCLURE_LOGINS",
    )
    manquants = [lib for mod, droit, sous, lib in DROITS_DOLIBARR if not _droit(info.get("rights"), mod, droit, sous)]
    yield Point(
        not manquants,
        f"Dolibarr : droits du compte « {login} »",
        f"fiche utilisateur → onglet Permissions, accorder : {' ; '.join(manquants)}",
    )
    if manquants:
        return
    point, attributs = _essai(
        "Dolibarr : lecture des attributs supplémentaires",
        "le compte technique doit être administrateur",
        lambda: d.http.get("/setup/extrafields"),
    )
    if attributs is None:
        yield point
        return
    for element, ecran in (("projet", "project_extrafields.php"), ("projet_task", "project_task_extrafields.php")):
        present = config.dolibarr_attribut in ((attributs or {}).get(element) or {})
        quoi = "projets" if element == "projet" else "tâches"
        yield Point(
            present,
            f"Dolibarr : attribut « {config.dolibarr_attribut} » sur les {quoi}",
            f"le créer dans /projet/admin/{ecran} (code exact : {config.dolibarr_attribut})",
        )
    for chemin, quoi in (("/projects", "projets"), ("/tasks", "tâches"), ("/thirdparties", "tiers")):
        yield _essai(f"Dolibarr : lecture des {quoi}", "droits insuffisants", partial(d.pages, chemin))[0]
    yield _essai(
        "Dolibarr : lecture des temps passés",
        "droits insuffisants",
        lambda: d.pages("/projects/alltimespent"),
    )[0]


def _openproject(config: Config) -> Iterator[Point]:
    o = OpenProject(config)
    point, moi = _essai(
        "OpenProject : connexion avec le jeton",
        "vérifier OPENPROJECT_URL, OPENPROJECT_API_KEY (et OPENPROJECT_HOST si l'adresse est interne)",
        lambda: o.http.get("/users/me"),
    )
    yield point
    if moi is None:
        return
    login = str(moi.get("login"))
    yield Point(
        bool(moi.get("admin")), f"OpenProject : le compte « {login} » est administrateur", "cocher Administrateur"
    )
    yield Point(
        login.casefold() in config.exclure_logins,
        f"OpenProject : le compte technique « {login} » est exclu de la synchronisation",
        f"ajouter {login} à EXCLURE_LOGINS",
    )

    attente = o.aucun_projet_actif()
    if attente:
        pourquoi = "OpenProject ne répond à ces lectures qu'une fois un projet actif créé ; contrôlé au premier cycle"
        for libelle in (
            f"OpenProject : champ « {config.op_champ_ref} » des temps",
            f"OpenProject : type de lot « {config.op_type_tache} »",
            f"OpenProject : rôles « {config.op_role_chef} » et « {config.op_role_contributeur} »",
            f"OpenProject : champ « {config.op_champ_ref} » des lots, actif pour ce type",
        ):
            yield Point(True, libelle, pourquoi, attente=True)

    for genre, nom, ecran in (
        ("projet", config.op_champ_client, "/admin/settings/project_custom_fields"),
        ("projet", config.op_champ_ref, "/admin/settings/project_custom_fields"),
        ("temps", config.op_champ_ref, "/custom_fields?tab=TimeEntryCustomField"),
    ):
        if attente and genre == "temps":
            continue
        libelle = f"OpenProject : champ « {nom} » des {'projets' if genre == 'projet' else 'temps'}"
        try:
            trouve = o.champ(genre, nom)
        except ErreurApi as e:
            yield Point(False, libelle, f"lecture du schéma impossible ({str(e)[:160]})")
            continue
        yield Point(trouve is not None, libelle, f"le créer (type Texte) dans {ecran}")

    if attente:
        return
    point, type_id = _essai(
        f"OpenProject : type de lot « {config.op_type_tache} »",
        "régler OPENPROJECT_TYPE sur le nom exact d'un type existant",
        o.type_par_defaut,
    )
    yield point
    for canonique, nom in (("chef", config.op_role_chef), ("contributeur", config.op_role_contributeur)):
        yield _essai(
            f"OpenProject : rôle « {nom} »",
            "régler OPENPROJECT_ROLE_CHEF / OPENPROJECT_ROLE_CONTRIBUTEUR",
            partial(o.role, canonique),
        )[0]

    if type_id is not None:
        yield _champ_des_lots(o, config)


def _champ_des_lots(o: OpenProject, config: Config) -> Point:
    """Le champ des lots n'apparaît dans le schéma d'un projet que s'il y est actif pour le type utilisé.

    Chaque projet actif est contrôlé : sans le champ, la création d'un lot venu de Dolibarr y échoue.
    Deux réglages OpenProject distincts peuvent manquer : le champ activé pour le type (configuration
    du formulaire du type), et le champ « pour tous les projets ».
    """
    ref, type_ = config.op_champ_ref, config.op_type_tache
    libelle = f"OpenProject : champ « {ref} » des lots, actif pour le type « {type_} » dans chaque projet"
    sans_champ: list[str] = []
    sans_type: list[str] = []
    try:
        type_id = o.type_par_defaut()
        projets = o.projets(actifs=True)
        if not projets:
            return Point(True, f"{libelle} — invérifiable sans projet actif, contrôlé à la première création")
        for projet in projets:
            nom = f"« {projet.get('name') or projet['id']} »"
            try:
                schema = o.http.get(f"/work_packages/schemas/{projet['id']}-{type_id}")
            except Introuvable:
                sans_type.append(nom)
                continue
            if not any(
                cle.startswith("customField") and isinstance(desc, dict) and cle_nom(desc.get("name")) == cle_nom(ref)
                for cle, desc in schema.items()
            ):
                sans_champ.append(nom)
    except ErreurApi as e:
        return Point(False, libelle, f"lecture des schémas impossible ({str(e)[:160]})")

    conseils: list[str] = []
    avec_type = len(projets) - len(sans_type)
    if sans_champ and len(sans_champ) == avec_type:
        # Absent partout : le champ n'est pas activé pour ce type (cas vécu), pas un problème de projets.
        conseils.append(
            f"le champ n'est activé pour le type « {type_} » dans aucun projet : Administration → Lots de travaux "
            f"→ Types → « {type_} » → Configuration du formulaire → faire glisser « {ref} » des attributs "
            "inactifs vers un groupe, puis enregistrer"
        )
    elif sans_champ:
        conseils.append(
            f"Administration → Champs personnalisés → Lots de travaux → « {ref} » : cocher « Pour tous les "
            f"projets » (manquant dans : {', '.join(sans_champ)})"
        )
    if sans_type:
        conseils.append(
            f"type « {type_} » non activé dans : {', '.join(sans_type)} (paramètres du projet → Types de lots)"
        )
    if conseils:
        return Point(False, libelle, " ; ".join(conseils))
    return Point(True, libelle)


def rendre(points: list[Point]) -> tuple[str, bool]:
    lignes = []
    for p in points:
        lignes.append(f"{'⏳' if p.attente else '✅' if p.ok else '❌'} {p.libelle}")
        if (p.attente or not p.ok) and p.conseil:
            lignes.append(f"     → {p.conseil}")
    tout_bon = all(p.ok for p in points)
    return "\n".join(lignes), tout_bon


__all__ = ["DROITS_DOLIBARR", "Point", "rendre", "verifier"]
