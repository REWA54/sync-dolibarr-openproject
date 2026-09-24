"""Mémoire du service (SQLite) : liens, instantanés, créations en cours, journal, alertes.

Perdre cette base ne crée pas de doublons : les identifiants sont aussi embarqués dans les objets
(attribut supplémentaire Dolibarr, champ personnalisé OpenProject) et les liens se reconstituent
au cycle suivant. On perd seulement les instantanés, donc la mémoire de « qui a changé quoi ».
D'où les sauvegardes quotidiennes, faites à chaud par l'API de sauvegarde de SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .modele import Cote, EnCours, Lien

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liens (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    dol_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    snap_dol TEXT,
    snap_op TEXT,
    rompu INTEGER NOT NULL DEFAULT 0,
    cree_le TEXT NOT NULL,
    maj_le TEXT NOT NULL,
    UNIQUE (type, dol_id),
    UNIQUE (type, op_id)
);
CREATE TABLE IF NOT EXISTS en_cours (
    type TEXT NOT NULL,
    cible TEXT NOT NULL,
    source_id TEXT NOT NULL,
    depuis TEXT NOT NULL,
    PRIMARY KEY (type, cible, source_id)
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    debut TEXT NOT NULL,
    fin TEXT,
    mode TEXT NOT NULL,
    statut TEXT NOT NULL DEFAULT 'en cours',
    resume TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    cycle INTEGER NOT NULL,
    horodatage TEXT NOT NULL,
    type TEXT NOT NULL,
    action TEXT NOT NULL,
    cote TEXT,
    objet_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS journal_cycle ON journal (cycle);
CREATE TABLE IF NOT EXISTS alertes (
    cle TEXT PRIMARY KEY,
    titre TEXT NOT NULL,
    message TEXT NOT NULL,
    permanente INTEGER NOT NULL,
    premiere TEXT NOT NULL,
    derniere_levee TEXT NOT NULL,
    dernier_envoi TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS commentaires (
    op_id TEXT PRIMARY KEY,
    maj_op TEXT,
    empreinte TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    cle TEXT PRIMARY KEY,
    valeur TEXT
);
"""


def _maintenant() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Une requête écrite en entier par côté : aucun texte n'est jamais assemblé dans du SQL.
_LIEN_PAR_ID: dict[Cote, str] = {
    "dol": "SELECT * FROM liens WHERE type = ? AND dol_id = ?",
    "op": "SELECT * FROM liens WHERE type = ? AND op_id = ?",
}
_CHANGER_ID: dict[Cote, str] = {
    "dol": "UPDATE liens SET dol_id = ?, maj_le = ? WHERE id = ?",
    "op": "UPDATE liens SET op_id = ?, maj_le = ? WHERE id = ?",
}


class BaseIllisible(Exception):
    """La base d'état est absente ou endommagée."""


def _json(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _lire_json(v: str | None) -> Any:
    return None if v is None else json.loads(v)


class Etat:
    def __init__(self, connexion: sqlite3.Connection, *, schema: bool = True):
        self.cx = connexion
        self.cx.row_factory = sqlite3.Row
        if schema:
            self.cx.executescript(_SCHEMA)
            self.cx.commit()

    @classmethod
    def ouvrir(cls, chemin: str | Path) -> Etat:
        chemin = Path(chemin)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        # timeout : une lecture concurrente (« dolop rapport ») fait attendre, jamais échouer.
        cx = sqlite3.connect(chemin, isolation_level=None, timeout=30)
        try:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.execute("PRAGMA synchronous=FULL")
            cx.execute("PRAGMA foreign_keys=ON")
            return cls(cx)
        except sqlite3.DatabaseError as e:
            cx.close()
            raise BaseIllisible(f"base d'état illisible ({chemin}) : {e}") from e

    @classmethod
    def lire_seulement(cls, chemin: str | Path) -> Etat:
        """Pour « sante » et « rapport » : aucune écriture, pas même le schéma."""
        chemin = Path(chemin)
        if not chemin.exists():
            raise BaseIllisible(f"base d'état absente : {chemin}")
        cx = sqlite3.connect(f"{chemin.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None, timeout=30)
        return cls(cx, schema=False)

    def fermer(self) -> None:
        self.cx.close()

    def __enter__(self) -> Etat:
        return self

    def __exit__(self, *_: object) -> None:
        self.fermer()

    def verifier_integrite(self) -> None:
        try:
            resultat = [r[0] for r in self.cx.execute("PRAGMA quick_check").fetchall()]
        except sqlite3.DatabaseError as e:
            raise BaseIllisible(f"base d'état endommagée : {e}") from e
        if resultat != ["ok"]:
            raise BaseIllisible("base d'état endommagée : " + " ; ".join(map(str, resultat[:5])))

    def sauvegarder(self, destination: str | Path) -> Path:
        """Copie cohérente de la base, faite à chaud (un cycle peut tourner en même temps)."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        provisoire = destination.with_name(destination.name + ".partiel")
        cible = sqlite3.connect(provisoire)
        try:
            self.cx.backup(cible)
        finally:
            cible.close()
        provisoire.replace(destination)  # atomique : jamais de sauvegarde à moitié écrite
        return destination

    def purger(self, conservation: timedelta) -> int:
        """Oublie les cycles (et leur journal) plus anciens que ``conservation``. Renvoie leur nombre."""
        limite = (datetime.now(UTC) - conservation).isoformat(timespec="seconds")
        self.cx.execute("DELETE FROM journal WHERE cycle IN (SELECT id FROM cycles WHERE debut < ?)", (limite,))
        return self.cx.execute("DELETE FROM cycles WHERE debut < ?", (limite,)).rowcount

    @classmethod
    def en_memoire(cls) -> Etat:
        return cls(sqlite3.connect(":memory:", isolation_level=None))

    def copie_en_memoire(self) -> Etat:
        """Pour simuler : on travaille sur une copie, la vraie base n'est jamais touchée."""
        cible = sqlite3.connect(":memory:", isolation_level=None)
        self.cx.backup(cible)
        return Etat(cible)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.cx.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.cx.execute("ROLLBACK")
            raise
        self.cx.execute("COMMIT")

    # --------------------------------------------------------------------------- liens

    def _lien(self, row: sqlite3.Row) -> Lien:
        return Lien(
            id=row["id"],
            type=row["type"],
            dol_id=row["dol_id"],
            op_id=row["op_id"],
            snap_dol=_lire_json(row["snap_dol"]),
            snap_op=_lire_json(row["snap_op"]),
            rompu=bool(row["rompu"]),
        )

    def liens(self, type_: str | None = None) -> list[Lien]:
        if type_ is None:
            rows = self.cx.execute("SELECT * FROM liens ORDER BY id").fetchall()
        else:
            rows = self.cx.execute("SELECT * FROM liens WHERE type = ? ORDER BY id", (type_,)).fetchall()
        return [self._lien(r) for r in rows]

    def lien_par_paire(self, type_: str, dol_id: str, op_id: str) -> Lien | None:
        row = self.cx.execute(
            "SELECT * FROM liens WHERE type = ? AND dol_id = ? AND op_id = ?", (type_, dol_id, op_id)
        ).fetchone()
        return None if row is None else self._lien(row)

    def lien_par_id(self, type_: str, cote: Cote, identifiant: str) -> Lien | None:
        row = self.cx.execute(_LIEN_PAR_ID[cote], (type_, identifiant)).fetchone()
        return None if row is None else self._lien(row)

    def creer_lien(
        self,
        type_: str,
        dol_id: str,
        op_id: str,
        snap_dol: dict[str, Any] | None = None,
        snap_op: dict[str, Any] | None = None,
    ) -> Lien:
        t = _maintenant()
        cur = self.cx.execute(
            "INSERT INTO liens (type, dol_id, op_id, snap_dol, snap_op, cree_le, maj_le) VALUES (?,?,?,?,?,?,?)",
            (type_, dol_id, op_id, _json(snap_dol), _json(snap_op), t, t),
        )
        assert cur.lastrowid is not None
        return Lien(cur.lastrowid, type_, dol_id, op_id, snap_dol, snap_op)

    def maj_instantanes(self, lien_id: int, snap_dol: dict[str, Any], snap_op: dict[str, Any]) -> None:
        self.cx.execute(
            "UPDATE liens SET snap_dol = ?, snap_op = ?, maj_le = ? WHERE id = ?",
            (_json(snap_dol), _json(snap_op), _maintenant(), lien_id),
        )

    def changer_id(self, lien_id: int, cote: Cote, nouvel_id: str) -> None:
        self.cx.execute(_CHANGER_ID[cote], (nouvel_id, _maintenant(), lien_id))

    def rompre(self, lien_id: int) -> None:
        self.cx.execute("UPDATE liens SET rompu = 1, maj_le = ? WHERE id = ?", (_maintenant(), lien_id))

    def supprimer_lien(self, lien_id: int) -> None:
        self.cx.execute("DELETE FROM liens WHERE id = ?", (lien_id,))

    # ---------------------------------------------------------------- créations en cours

    def en_cours(self) -> list[EnCours]:
        rows = self.cx.execute("SELECT type, cible, source_id FROM en_cours").fetchall()
        return [EnCours(r["type"], r["cible"], r["source_id"]) for r in rows]

    def noter_en_cours(self, type_: str, cible: Cote, source_id: str) -> None:
        self.cx.execute(
            "INSERT OR REPLACE INTO en_cours (type, cible, source_id, depuis) VALUES (?,?,?,?)",
            (type_, cible, source_id, _maintenant()),
        )

    def retirer_en_cours(self, type_: str, cible: Cote, source_id: str) -> None:
        self.cx.execute(
            "DELETE FROM en_cours WHERE type = ? AND cible = ? AND source_id = ?", (type_, cible, source_id)
        )

    # ------------------------------------------------------------------ cycles & journal

    def debuter_cycle(self, mode: str) -> int:
        cur = self.cx.execute("INSERT INTO cycles (debut, mode) VALUES (?, ?)", (_maintenant(), mode))
        assert cur.lastrowid is not None
        return cur.lastrowid

    def terminer_cycle(self, cycle: int, statut: str, resume: dict[str, Any]) -> None:
        self.cx.execute(
            "UPDATE cycles SET fin = ?, statut = ?, resume = ? WHERE id = ?",
            (_maintenant(), statut, _json(resume), cycle),
        )

    def derniers_cycles(self, n: int = 10) -> list[sqlite3.Row]:
        return self.cx.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def echecs_consecutifs(self) -> int:
        n = 0
        for row in self.cx.execute("SELECT statut FROM cycles WHERE mode = 'service' ORDER BY id DESC LIMIT 50"):
            if row["statut"] == "réussi":
                break
            if row["statut"] == "échec":
                n += 1
        return n

    def journaliser(
        self, cycle: int, type_: str, action: str, cote: str | None, objet_id: str | None, detail: Any = None
    ) -> None:
        self.cx.execute(
            "INSERT INTO journal (cycle, horodatage, type, action, cote, objet_id, detail) VALUES (?,?,?,?,?,?,?)",
            (cycle, _maintenant(), type_, action, cote, objet_id, _json(detail)),
        )

    def journal(self, cycle: int) -> list[sqlite3.Row]:
        return self.cx.execute("SELECT * FROM journal WHERE cycle = ? ORDER BY id", (cycle,)).fetchall()

    # -------------------------------------------------------------------------- alertes

    def alerte(self, cle: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.cx.execute("SELECT * FROM alertes WHERE cle = ?", (cle,)).fetchone()
        return row

    def lever_alerte(self, cle: str, titre: str, message: str, permanente: bool) -> None:
        t = _maintenant()
        self.cx.execute(
            """INSERT INTO alertes (cle, titre, message, permanente, premiere, derniere_levee, active)
               VALUES (?,?,?,?,?,?,1)
               ON CONFLICT(cle) DO UPDATE SET titre = excluded.titre, message = excluded.message,
                   derniere_levee = excluded.derniere_levee,
                   premiere = CASE WHEN alertes.active = 1 THEN alertes.premiere ELSE excluded.premiere END,
                   dernier_envoi = CASE WHEN alertes.active = 1 THEN alertes.dernier_envoi ELSE NULL END,
                   active = 1""",
            (cle, titre, message, int(permanente), t, t),
        )

    def noter_envoi(self, cle: str) -> None:
        self.cx.execute("UPDATE alertes SET dernier_envoi = ? WHERE cle = ?", (_maintenant(), cle))

    def alertes_actives(self, permanentes: bool | None = None) -> list[sqlite3.Row]:
        if permanentes is None:
            return self.cx.execute("SELECT * FROM alertes WHERE active = 1 ORDER BY premiere").fetchall()
        return self.cx.execute(
            "SELECT * FROM alertes WHERE active = 1 AND permanente = ? ORDER BY premiere", (int(permanentes),)
        ).fetchall()

    def eteindre_alerte(self, cle: str) -> None:
        self.cx.execute("UPDATE alertes SET active = 0 WHERE cle = ?", (cle,))

    # ------------------------------------------------------------------- commentaires

    def commentaires(self, op_id: str) -> tuple[str | None, str | None]:
        row = self.cx.execute("SELECT maj_op, empreinte FROM commentaires WHERE op_id = ?", (op_id,)).fetchone()
        return (None, None) if row is None else (row["maj_op"], row["empreinte"])

    def noter_commentaires(self, op_id: str, maj_op: str | None, empreinte: str | None) -> None:
        self.cx.execute(
            "INSERT OR REPLACE INTO commentaires (op_id, maj_op, empreinte) VALUES (?,?,?)",
            (op_id, maj_op, empreinte),
        )

    # ---------------------------------------------------------------------------- méta

    def meta(self, cle: str) -> str | None:
        row = self.cx.execute("SELECT valeur FROM meta WHERE cle = ?", (cle,)).fetchone()
        return None if row is None else str(row["valeur"])

    def poser_meta(self, cle: str, valeur: str) -> None:
        self.cx.execute("INSERT OR REPLACE INTO meta (cle, valeur) VALUES (?, ?)", (cle, valeur))
