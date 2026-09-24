"""Conversions entre les représentations Dolibarr, OpenProject et canoniques.

Règles du canonique : textes riches en Markdown, durées en secondes arrondies à la minute,
dates en « AAAA-MM-JJ » dans le fuseau de l'entreprise, horodatages en UTC.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import markdown as _markdown
from markdownify import markdownify as _markdownify

# ------------------------------------------------------------------------------- durées

_DUREE_ISO = re.compile(
    r"^P(?:(?P<w>\d+(?:\.\d+)?)W)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def arrondir_minute(secondes: float | int) -> int:
    return round(secondes / 60) * 60


def duree_iso_vers_secondes(valeur: str | None) -> int | None:
    """« PT1H30M », « P1DT2H » (jour = 24 h, format de sortie d'OpenProject) → secondes."""
    if valeur is None or valeur == "":
        return None
    m = _DUREE_ISO.match(valeur.strip())
    if not m or valeur.strip() in ("P", "PT"):
        raise ValueError(f"durée ISO 8601 illisible : {valeur!r}")
    parts = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    total = parts["w"] * 7 * 86400 + parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]
    return arrondir_minute(total)


def secondes_vers_duree_iso(secondes: int | None) -> str | None:
    if secondes is None:
        return None
    minutes = arrondir_minute(secondes) // 60
    heures, reste = divmod(minutes, 60)
    if heures and reste:
        return f"PT{heures}H{reste}M"
    if heures:
        return f"PT{heures}H"
    return f"PT{reste}M"


# -------------------------------------------------------------------------------- dates


def horodatage_vers_date(horodatage: int | float | str | None, fuseau: ZoneInfo) -> str | None:
    """Horodatage Unix (tel que renvoyé par l'API Dolibarr) → date locale."""
    if horodatage in (None, "", 0, "0"):
        return None
    assert horodatage is not None
    return datetime.fromtimestamp(int(float(horodatage)), tz=fuseau).date().isoformat()


def date_vers_horodatage(jour: str | None, fuseau: ZoneInfo, heure: time = time(0, 0)) -> int | None:
    if not jour:
        return None
    return int(datetime.combine(date.fromisoformat(jour), heure, tzinfo=fuseau).timestamp())


def date_vers_texte_gmt(jour: str, fuseau: ZoneInfo) -> str:
    """Minuit local → « AAAA-MM-JJ HH:MM:SS » en GMT, format attendu par addtimespent.

    Dolibarr reconvertit ce GMT dans le fuseau du serveur avant d'en extraire la date :
    envoyer minuit local garantit que la date enregistrée est bien ``jour``.
    """
    local = datetime.combine(date.fromisoformat(jour), time(0, 0), tzinfo=fuseau)
    return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def texte_local_vers_date(valeur: str | None) -> str | None:
    """« 2026-07-15 00:00:00 » (heure locale du serveur Dolibarr) → « 2026-07-15 »."""
    if not valeur:
        return None
    return str(valeur)[:10]


def iso_vers_utc(valeur: str | None) -> datetime | None:
    if not valeur:
        return None
    dt = datetime.fromisoformat(valeur.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def horodatage_vers_utc(horodatage: int | float | str | None) -> datetime | None:
    if horodatage in (None, "", 0, "0"):
        return None
    assert horodatage is not None
    return datetime.fromtimestamp(int(float(horodatage)), tz=UTC)


def maintenant() -> datetime:
    return datetime.now(UTC)


def age(depuis: datetime) -> timedelta:
    return maintenant() - depuis


# -------------------------------------------------------------------------------- texte


def texte_normalise(valeur: str | None) -> str:
    """Pour comparer deux textes : espaces fusionnés, bords retirés, fins de ligne unifiées."""
    if not valeur:
        return ""
    return re.sub(r"\s+", " ", valeur.replace("\r\n", "\n")).strip()


def cle_nom(valeur: str | None) -> str:
    """Nom comparable : sans accents, sans casse, espaces fusionnés."""
    if not valeur:
        return ""
    decompose = unicodedata.normalize("NFKD", valeur)
    sans_accents = "".join(c for c in decompose if not unicodedata.combining(c))
    return texte_normalise(sans_accents).casefold()


def html_vers_markdown(html: str | None) -> str:
    if not html or not html.strip():
        return ""
    if "<" not in html:
        return html.replace("\r\n", "\n").strip()
    md = _markdownify(html, heading_style="ATX", bullets="-", strip=["span", "font"])
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def markdown_vers_html(md: str | None) -> str:
    if not md or not md.strip():
        return ""
    return str(_markdown.markdown(md, extensions=["sane_lists", "nl2br"])).strip()


def texte_simple(valeur: str | None) -> str:
    """Notes de temps : texte brut des deux côtés, on retire seulement le balisage éventuel."""
    if not valeur:
        return ""
    if "<" in valeur:
        valeur = html_vers_markdown(valeur)
    return valeur.replace("\r\n", "\n").strip()


# ----------------------------------------------------------------------- identifiant OP

_IDENTIFIANT_INTERDIT = re.compile(r"[^a-z0-9_-]+")


def identifiant_op(code: str | None, repli: str) -> str:
    """Réf. Dolibarr « PJ2501-0001 » → identifiant OpenProject « pj2501-0001 ».

    OpenProject exige : minuscules, chiffres, tirets, soulignés, une lettre en tête, 100 car. max.
    """
    base = cle_nom(code or "").replace(" ", "-")
    base = _IDENTIFIANT_INTERDIT.sub("-", base).strip("-_")
    if not base:
        base = _IDENTIFIANT_INTERDIT.sub("-", cle_nom(repli).replace(" ", "-")).strip("-_") or "projet"
    if not base[0].isalpha():
        base = "p-" + base
    return base[:100]
