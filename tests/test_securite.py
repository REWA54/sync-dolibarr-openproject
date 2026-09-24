"""Garde-fous de sécurité : secrets, contenu hostile venu d'un outil, identifiants embarqués falsifiés."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from faux import Banc, projet

from dolop.alertes import Alertes, ErreurWebhook, envoi_webhook
from dolop.config import Config, ErreurConfig
from dolop.conversions import html_vers_markdown, markdown_vers_html
from dolop.cycle import Commentaire, rendre_commentaires
from dolop.etat import Etat

ENV = {
    "DOLIBARR_URL": "http://dolibarr",
    "DOLIBARR_API_KEY": "jeton-dolibarr-secret",
    "OPENPROJECT_URL": "https://openproject.example.org",
    "OPENPROJECT_API_KEY": "jeton-openproject-secret",
}

# --------------------------------------------------------------------------- secrets


def test_les_secrets_napparaissent_jamais_dans_la_representation_de_la_config() -> None:
    config = Config.depuis_env({**ENV, "ALERTE_WEBHOOK_URL": "https://ha.example.org/api/webhook/abc123"})
    texte = repr(config)
    assert "jeton-dolibarr-secret" not in texte
    assert "jeton-openproject-secret" not in texte
    assert "abc123" not in texte


def test_secret_lu_dans_un_fichier_docker(tmp_path: Path) -> None:
    fichier = tmp_path / "cle"
    fichier.write_text("jeton-du-fichier\n")
    env = {k: v for k, v in ENV.items() if k != "DOLIBARR_API_KEY"}
    config = Config.depuis_env({**env, "DOLIBARR_API_KEY_FILE": str(fichier)})
    assert config.dolibarr_cle == "jeton-du-fichier"


def test_secret_en_double_ou_fichier_illisible_refuse(tmp_path: Path) -> None:
    with pytest.raises(ErreurConfig, match="tous deux"):
        Config.depuis_env({**ENV, "DOLIBARR_API_KEY_FILE": str(tmp_path / "cle")})
    env = {k: v for k, v in ENV.items() if k != "DOLIBARR_API_KEY"}
    with pytest.raises(ErreurConfig, match="lecture"):
        Config.depuis_env({**env, "DOLIBARR_API_KEY_FILE": str(tmp_path / "absent")})


@pytest.mark.parametrize(
    ("variable", "valeur", "motif"),
    [
        ("DOLIBARR_URL", "dolibarr.example.org", "http"),
        ("DOLIBARR_URL", "ftp://dolibarr", "http"),
        ("OPENPROJECT_URL", "https://moi:motdepasse@op.example.org", "identifiants"),
        ("INTERVALLE_SECONDES", "0", "au moins 10"),
        ("SEUIL_POURCENT", "150", "entre 1 et 100"),
        ("FUSEAU", "Mars/Olympus", "fuseau"),
        ("ALERTE_WEBHOOK_URL", "file:///etc/passwd", "http"),
    ],
)
def test_configuration_dangereuse_ou_fausse_refusee_au_demarrage(variable: str, valeur: str, motif: str) -> None:
    with pytest.raises(ErreurConfig, match=motif):
        Config.depuis_env({**ENV, variable: valeur})


def test_fuseau_du_conteneur_repris_si_valide() -> None:
    assert Config.depuis_env({**ENV, "TZ": "America/Montreal"}).fuseau_nom == "America/Montreal"
    assert Config.depuis_env({**ENV, "TZ": ":/etc/localtime"}).fuseau_nom == "Europe/Paris"
    assert Config.depuis_env({**ENV, "TZ": "UTC", "FUSEAU": "Europe/Brussels"}).fuseau_nom == "Europe/Brussels"


def test_lechec_du_webhook_ne_devoile_pas_son_adresse(caplog: pytest.LogCaptureFixture) -> None:
    url = "https://ha.example.org/api/webhook/cle-secrete-du-webhook"
    envoi = envoi_webhook(url, transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    with pytest.raises(ErreurWebhook) as e:
        envoi("titre", "message")
    assert "cle-secrete" not in str(e.value)

    def coupe(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connexion refusée vers {url}")

    alertes = Alertes(Etat.en_memoire(), envoi_webhook(url, transport=httpx.MockTransport(coupe)))
    with caplog.at_level(logging.ERROR):
        alertes.lever("test", "quelque chose")
    assert "cle-secrete" not in caplog.text
    assert "webhook injoignable" in caplog.text
    alertes.etat.fermer()


# ------------------------------------------------------------- contenu hostile (XSS)


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        '<img src="x" onerror="alert(1)">',
        "[clic](javascript:alert(1))",
        '<a href="javascript:alert(1)">clic</a>',
        "<iframe src='https://attaquant.example'></iframe>",
        '<p style="background:url(javascript:alert(1))">texte</p>',
    ],
)
def test_html_ecrit_dans_dolibarr_est_assaini(hostile: str) -> None:
    html = markdown_vers_html(f"Avant\n\n{hostile}\n\nAprès").casefold()
    for interdit in ("<script", "onerror", "javascript:", "<iframe", "style="):
        assert interdit not in html, html
    assert "avant" in html and "après" in html


def test_assainissement_garde_la_mise_en_forme_et_les_mentions() -> None:
    md = "**gras** et _italique_\n\n- un\n- deux\n\n[site](https://example.org)"
    html = markdown_vers_html(md)
    assert "<strong>gras</strong>" in html and "<li>un</li>" in html
    assert 'href="https://example.org"' in html
    assert html_vers_markdown(html).startswith("**gras**")
    mention = '<mention class="mention" data-id="6" data-type="user">@alice</mention> merci'
    assert "@alice" in markdown_vers_html(mention)


def test_commentaire_openproject_hostile_neutralise_dans_la_note_dolibarr() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    bloc = rendre_commentaires(
        [Commentaire(datetime(2026, 9, 24, tzinfo=UTC), "<b>Mallory</b>", "<script>vol()</script>coucou")],
        ZoneInfo("Europe/Paris"),
    )
    assert "<script" not in bloc and "&lt;b&gt;Mallory" in bloc and "coucou" in bloc


# ------------------------------------------------- identifiants embarqués falsifiés


def test_identifiant_openproject_saisi_a_la_main_dans_dolibarr_naccroche_pas_un_projet_etranger() -> None:
    """L'attribut Dolibarr « ID OpenProject » est modifiable par tout utilisateur : un identifiant
    tapé à la main (erreur ou malveillance) ne doit pas relier le projet à un projet sans rapport,
    par exemple un projet archivé d'OpenProject que l'utilisateur n'a pas le droit de voir."""
    b = Banc()
    cache = b.op["projet"].ajouter("op900", **projet("Archives RH", actif=False))
    b.dol["projet"].ajouter("dp1", ref_autre=cache, **projet("Site vitrine"))
    r = b.cycle()
    assert r.statut == "réussi", r.message
    assert b.op["projet"].objets[cache] == projet("Archives RH", actif=False), "projet étranger intact"
    assert b.etat.lien_par_id("projet", "op", cache) is None
    jumeau = b.dol["projet"].refs["dp1"]
    assert jumeau and jumeau != cache, "le projet Dolibarr a reçu son propre jumeau"
    assert b.op["projet"].objets[jumeau]["titre"] == "Site vitrine"


def test_identifiant_dolibarr_seul_mais_meme_identite_est_relie() -> None:
    """Base d'état perdue et champ OpenProject vide (type de lot sans le champ) : même identité, on relie."""
    b = Banc()
    b.op["projet"].ajouter("op900", **projet("Site vitrine"))
    b.dol["projet"].ajouter("dp1", ref_autre="op900", **projet("Site vitrine"))
    assert b.cycle().statut == "réussi"
    lien = b.etat.lien_par_id("projet", "op", "op900")
    assert lien is not None and lien.dol_id == "dp1"
    assert len(b.op["projet"].objets) == 1, "aucun doublon"
