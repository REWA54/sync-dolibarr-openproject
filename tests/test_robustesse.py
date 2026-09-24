"""Résilience : pannes réseau, cycles concurrents, base d'état endommagée, sauvegardes, entretien."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_adaptateurs import Serveur, dolibarr

from dolop.__main__ import cmd_service, entretenir, main
from dolop.adaptateurs import ErreurApi
from dolop.alertes import Alertes
from dolop.config import Config
from dolop.cycle import Resultat
from dolop.etat import BaseIllisible, Etat
from dolop.execution import Bilan
from dolop.http import ClientHttp
from dolop.verrou import DejaEnCours, verrou

# ------------------------------------------------------------------------------ HTTP


def _client(gestion: httpx.MockTransport) -> ClientHttp:
    return ClientHttp("Test", "http://t", transport=gestion, pause=0)


def test_ecriture_rejouee_si_la_connexion_na_jamais_ete_etablie() -> None:
    appels: list[str] = []

    def serveur(requete: httpx.Request) -> httpx.Response:
        appels.append(requete.method)
        if len(appels) == 1:
            raise httpx.ConnectError("connexion refusée")
        return httpx.Response(200, json={"id": 7})

    assert _client(httpx.MockTransport(serveur)).post("/objets", {"a": 1}) == {"id": 7}
    assert appels == ["POST", "POST"], "la requête n'était jamais partie : la rejouer est sans risque"


def test_ecriture_jamais_rejouee_si_la_reponse_sest_perdue() -> None:
    appels: list[str] = []

    def serveur(_requete: httpx.Request) -> httpx.Response:
        appels.append("POST")
        raise httpx.ReadTimeout("réponse perdue")

    with pytest.raises(ErreurApi, match="injoignable"):
        _client(httpx.MockTransport(serveur)).post("/objets", {"a": 1})
    assert appels == ["POST"], "l'objet a peut-être été créé : on laisse le moteur retrouver son jumeau"


def test_lecture_retentee_apres_limitation_de_debit() -> None:
    reponses = iter([httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=[1])])
    assert _client(httpx.MockTransport(lambda _r: next(reponses))).get("/liste") == [1]


def test_attente_demandee_par_le_serveur_plafonnee() -> None:
    client = _client(httpx.MockTransport(lambda _r: httpx.Response(200)))
    assert client._attente(httpx.Response(429, headers={"Retry-After": "3600"}), 1) == 30.0
    assert client._attente(httpx.Response(429, headers={"Retry-After": "demain"}), 2) == 0.0


def test_dolibarr_page_suivante_en_404_vaut_fin_de_liste() -> None:
    s = Serveur()
    pleine = [{"id": i} for i in range(100)]
    s.route("GET", "/api/index.php/projects", lambda r: (200, pleine) if r.url.params["page"] == "0" else (404, {}))
    d, _ = dolibarr(s)
    assert len(d.pages("/projects")) == 100


def test_dolibarr_premiere_page_en_404_nest_pas_une_liste_vide() -> None:
    """Une adresse fausse ne doit jamais faire croire que tout a été supprimé."""
    s = Serveur()
    s.route("GET", "/api/index.php/projects", (404, {}))
    d, _ = dolibarr(s)
    with pytest.raises(ErreurApi):
        d.pages("/projects")


# ---------------------------------------------------------------------------- verrou


def test_un_seul_cycle_a_la_fois(tmp_path: Path) -> None:
    chemin = tmp_path / "etat.sqlite.verrou"
    with verrou(chemin):
        assert chemin.read_text().strip() == str(os.getpid())
        with pytest.raises(DejaEnCours, match=str(os.getpid())), verrou(chemin, attente=0.6):
            pass
    with verrou(chemin):  # relâché à la sortie, même après une erreur
        pass
    assert oct(chemin.stat().st_mode & 0o777) == "0o600"


def test_une_fois_refuse_pendant_quun_cycle_tourne(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr("dolop.__main__.ATTENTE_CYCLE", 0.0)
    with verrou(tmp_path / "etat.sqlite.verrou"):
        assert main(["une-fois"]) == 3
    assert "un autre cycle tourne déjà" in capsys.readouterr().err


# ------------------------------------------------------------------- base d'état


def _env(monkeypatch: pytest.MonkeyPatch, dossier: Path) -> None:
    for nom, valeur in {
        "DOLIBARR_URL": "http://dolibarr",
        "DOLIBARR_API_KEY": "x",
        "OPENPROJECT_URL": "http://op",
        "OPENPROJECT_API_KEY": "x",
        "BASE_ETAT": str(dossier / "etat.sqlite"),
    }.items():
        monkeypatch.setenv(nom, valeur)
    monkeypatch.delenv("ALERTE_WEBHOOK_URL", raising=False)


def test_sante_sans_base_repond_non_sans_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    assert main(["sante"]) == 1
    assert "base d'état" in capsys.readouterr().out
    assert not (tmp_path / "etat.sqlite").exists(), "la sonde de santé ne crée rien"


def test_sante_lit_la_base_sans_jamais_lecrire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, tmp_path)
    etat = Etat.ouvrir(tmp_path / "etat.sqlite")
    etat.poser_meta("dernier_succes", datetime.now(UTC).isoformat(timespec="seconds"))
    avant = (tmp_path / "etat.sqlite").stat().st_mtime_ns
    assert main(["sante"]) == 0
    assert main(["rapport"]) == 0
    assert (tmp_path / "etat.sqlite").stat().st_mtime_ns == avant
    etat.fermer()


def test_base_endommagee_arret_net_avec_consigne(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, tmp_path)
    (tmp_path / "etat.sqlite").write_bytes(b"pas une base SQLite" * 100)
    assert main(["service"]) == 4
    assert "sauvegarde" in capsys.readouterr().err
    with pytest.raises(BaseIllisible), Etat.lire_seulement(tmp_path / "etat.sqlite") as etat:
        etat.verifier_integrite()


def test_version_affichee(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as fin:
        main(["--version"])
    assert fin.value.code == 0
    assert capsys.readouterr().out.startswith("dolop ")


# --------------------------------------------------------------- sauvegardes, purge


def test_sauvegarde_a_chaud_restaurable(tmp_path: Path) -> None:
    etat = Etat.ouvrir(tmp_path / "etat.sqlite")
    etat.creer_lien("projet", "1", "9")
    copie = etat.sauvegarder(tmp_path / "sauvegardes" / "etat-1.sqlite")
    etat.creer_lien("projet", "2", "10")  # écrit après la sauvegarde : absent de la copie
    restauree = Etat.lire_seulement(copie)
    restauree.verifier_integrite()
    assert [(lien.dol_id, lien.op_id) for lien in restauree.liens()] == [("1", "9")]
    assert not list((tmp_path / "sauvegardes").glob("*.partiel"))
    etat.fermer()
    restauree.fermer()


def test_entretien_quotidien_sauvegarde_tourne_et_purge(tmp_path: Path) -> None:
    config = Config("http://d", "x", "http://o", "x", base=tmp_path / "etat.sqlite", sauvegardes=2)
    etat = Etat.ouvrir(config.base)
    alertes = Alertes(etat, None)
    vieux = etat.debuter_cycle("service")
    etat.cx.execute("UPDATE cycles SET debut = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", vieux))
    etat.journaliser(vieux, "projet", "créer", "op", "9")
    recent = etat.debuter_cycle("service")

    entretenir(config, etat, alertes)
    assert [c["id"] for c in etat.derniers_cycles()] == [recent]
    assert etat.journal(vieux) == []
    assert len(list(config.dossier_sauvegardes.glob("etat-*.sqlite"))) == 1

    entretenir(config, etat, alertes)  # moins de 24 h après : rien
    assert len(list(config.dossier_sauvegardes.glob("etat-*.sqlite"))) == 1

    for jour in ("20200101", "20200102", "20200103"):
        (config.dossier_sauvegardes / f"etat-{jour}-000000.sqlite").write_bytes(b"")
    etat.poser_meta("derniere_sauvegarde", (datetime.now(UTC) - timedelta(days=2)).isoformat())
    entretenir(config, etat, alertes)
    gardees = sorted(p.name for p in config.dossier_sauvegardes.glob("etat-*.sqlite"))
    assert gardees[0] == "etat-20200103-000000.sqlite", "les plus anciennes partent"
    assert len(gardees) == 2 and gardees[1] > "etat-2026", "la sauvegarde du jour reste"
    etat.fermer()


def test_sauvegarde_impossible_alerte_puis_retablie(tmp_path: Path) -> None:
    config = Config("http://d", "x", "http://o", "x", base=tmp_path / "etat.sqlite")
    etat = Etat.ouvrir(config.base)
    envoyees: list[tuple[str, str]] = []
    alertes = Alertes(etat, lambda t, m: envoyees.append((t, m)))
    config.dossier_sauvegardes.parent.mkdir(exist_ok=True)
    config.dossier_sauvegardes.write_text("un fichier à la place du dossier")
    entretenir(config, etat, alertes)
    assert any("Sauvegarde quotidienne" in m for _, m in envoyees)
    config.dossier_sauvegardes.unlink()
    entretenir(config, etat, alertes)
    assert any("rétabli" in t for t, _ in envoyees)
    etat.fermer()


def test_fichiers_du_service_prives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Base, sauvegardes et verrou contiennent des données personnelles : 0600."""
    _env(monkeypatch, tmp_path)
    ancien = os.umask(0o022)
    try:
        etat = Etat.ouvrir(tmp_path / "etat.sqlite")
        etat.fermer()
        (tmp_path / "etat.sqlite").chmod(0o600)
        assert main(["sauvegarder", str(tmp_path / "copie.sqlite")]) == 0
        assert oct((tmp_path / "copie.sqlite").stat().st_mode & 0o777) == "0o600"
    finally:
        os.umask(ancien)
    with sqlite3.connect(tmp_path / "copie.sqlite") as cx:
        assert cx.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    cx.close()


def test_service_survit_a_une_erreur_imprevue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    config = Config("http://d", "x", "http://o", "x", base=tmp_path / "etat.sqlite", intervalle=0)
    appels: list[str] = []

    def faux_cycle(*_: object, mode: str) -> Resultat:
        appels.append(mode)
        if len(appels) == 1:
            raise RuntimeError("panne imprévue hors du cycle")
        os.kill(os.getpid(), signal.SIGTERM)  # docker stop
        return Resultat(len(appels), "réussi", Bilan())

    monkeypatch.setattr("dolop.__main__.executer_cycle", faux_cycle)
    monkeypatch.setattr("dolop.__main__.construire", lambda _c: None)
    anciens = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with Etat.ouvrir(config.base) as etat:
            assert cmd_service(config, etat) == 0
    finally:
        for s, gestion in anciens.items():
            signal.signal(s, gestion)
    assert appels == ["service", "service"], "le service a continué après l'erreur"
    assert "panne imprévue hors du cycle" in caplog.text
    assert "arrêté proprement" in caplog.text
