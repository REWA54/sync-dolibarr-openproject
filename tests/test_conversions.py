from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from dolop.conversions import (
    cle_nom,
    date_vers_horodatage,
    date_vers_texte_gmt,
    duree_iso_vers_secondes,
    horodatage_vers_date,
    html_vers_markdown,
    identifiant_op,
    markdown_vers_html,
    secondes_vers_duree_iso,
)
from dolop.cycle import MARQUEUR_COMMENTAIRES, remplacer_bloc

PARIS = ZoneInfo("Europe/Paris")


@pytest.mark.parametrize(
    ("iso", "secondes"),
    [
        ("PT1H", 3600),
        ("PT1H30M", 5400),
        ("PT0.5H", 1800),
        ("P1DT2H", 26 * 3600),  # OpenProject écrit les jours de 24 h
        ("PT26H", 26 * 3600),
        ("PT15M", 900),
        ("PT1H29M59S", 5400),  # arrondi à la minute
        ("PT0S", 0),
    ],
)
def test_durees_iso(iso: str, secondes: int) -> None:
    assert duree_iso_vers_secondes(iso) == secondes


def test_duree_illisible_leve_une_erreur() -> None:
    with pytest.raises(ValueError, match="illisible"):
        duree_iso_vers_secondes("1h30")


def test_aller_retour_des_durees() -> None:
    for s in (0, 60, 900, 3600, 5400, 26 * 3600):
        assert duree_iso_vers_secondes(secondes_vers_duree_iso(s)) == s
    assert duree_iso_vers_secondes(None) is None and secondes_vers_duree_iso(None) is None


def test_temps_saisi_a_23h30_reste_le_bon_jour() -> None:
    # 23 h 30 à Paris le 24/09 = 21 h 30 UTC : la date doit rester le 24.
    horodatage = int(datetime(2026, 9, 24, 21, 30, tzinfo=UTC).timestamp())
    assert horodatage_vers_date(horodatage, PARIS) == "2026-09-24"


def test_minuit_local_envoye_en_gmt_a_dolibarr() -> None:
    assert date_vers_texte_gmt("2026-09-24", PARIS) == "2026-09-23 22:00:00"  # heure d'été
    assert date_vers_texte_gmt("2026-12-01", PARIS) == "2026-11-30 23:00:00"  # heure d'hiver


def test_dates_autour_du_changement_d_heure() -> None:
    for jour in ("2026-03-29", "2026-10-25", "2026-10-26"):
        assert horodatage_vers_date(date_vers_horodatage(jour, PARIS), PARIS) == jour


def test_identifiant_openproject() -> None:
    assert identifiant_op("PJ2501-0001", "x") == "pj2501-0001"
    assert identifiant_op("2026/Été spécial", "x") == "p-2026-ete-special"
    assert identifiant_op("", "Site Acme") == "site-acme"


def test_noms_compares_sans_accents_ni_casse() -> None:
    assert cle_nom("  Château   Élégance ") == cle_nom("chateau elegance")


def test_html_markdown_aller_retour_stable() -> None:
    md = html_vers_markdown("<p>Texte <strong>gras</strong></p><ul><li>un</li><li>deux</li></ul>")
    assert "**gras**" in md and "- un" in md
    assert html_vers_markdown(markdown_vers_html(md)) == md
    assert html_vers_markdown("texte brut\r\nsur deux lignes") == "texte brut\nsur deux lignes"


def test_bloc_de_commentaires_remplace_sans_toucher_au_reste() -> None:
    bloc1 = f"<p>{MARQUEUR_COMMENTAIRES}</p>\n<p>premier</p>"
    bloc2 = f"<p>{MARQUEUR_COMMENTAIRES}</p>\n<p>second</p>"
    note = remplacer_bloc("<p>Note de l'utilisateur</p>", bloc1)
    note = remplacer_bloc(note, bloc2)
    assert note == f"<p>Note de l'utilisateur</p>\n{bloc2}"
    assert remplacer_bloc(note, "") == "<p>Note de l'utilisateur</p>"
    assert remplacer_bloc(None, bloc1) == bloc1
