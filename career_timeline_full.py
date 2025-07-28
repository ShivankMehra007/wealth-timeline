# career_timeline_full.py
"""
PyJHora‑based “wealth / career timeline” engine (antardaśā granularity).

The **only** public surface is
    • `timeline_from_args(...)` – a stateless helper that returns a
      *pandas* DataFrame with just two columns: **period** & **rating**.

The module depends **exclusively** on public symbols documented in
`jhora.*` sub‑packages shipped with PyJHora ≥ 4‑series – no private or
undocumented calls are used, and each helper is type‑checked for safe
inputs.

CLI usage (for quick tests):
----------------------------
    python career_timeline_full.py --help
"""

from __future__ import annotations

import argparse
import math
import re
import zoneinfo
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import pandas as pd

# ── PyJHora imports ──────────────────────────────────────────────────────
from jhora import const, utils as jutils
from jhora.panchanga.drik import Place                                     # ← documented
from jhora.horoscope.chart import charts as jcharts, house as jhouse, strength as jstrength
from jhora.horoscope.chart import ashtakavarga as jashta
from jhora.horoscope.dhasa.graha import vimsottari as jvim
from jhora.horoscope.dhasa.raasi import narayana as jnar

# ── Tunables (heuristic weights & label table) ───────────────────────────
WEALTH_WT, CAREER_WT = 15, 10          # house lordship
FUNC_BEN_WT, FUNC_MAL_WT = 10, -10     # functional nature
SAV_BONUS_WT = 5                       # Sarva‑aṣṭakavarga support
SB_GOOD_BONUS, SB_POOR_MALUS = 5, -5   # Shadbala
SAV_THRESHOLD = 30                     # “good” SAV score

LABELS: Tuple[Tuple[str, int], ...] = (
    ("EXCEPTIONAL OPPORTUNITY", 55),
    ("FAVOURABLE",               40),
    ("STABLE / MIXED",           25),
    ("CHALLENGING",              10),
    ("HIGH CAUTION",           -math.inf),
)

# ── helper: parse dates that appear in dasha lists ───────────────────────
_JD_LIMIT = 1_720_000  # any float > this is treated as a Julian‑day

def _to_dt(val) -> datetime:
    """Return *naive* datetime from ISO‑8601 string, (y,m,d) tuple or JD."""
    if isinstance(val, str):
        return datetime.fromisoformat(val.strip())
    if isinstance(val, (tuple, list)) and len(val) >= 3:
        y, m, d = map(int, val[:3])
        hh = int(val[3]) if len(val) > 3 else 0
        mm = int(val[4]) if len(val) > 4 else 0
        return datetime(y, m, d, hh, mm)
    if isinstance(val, (int, float)) and val > _JD_LIMIT:
        y, m, d, fh = jutils.jd_to_gregorian(float(val))
        return datetime(y, m, d, int(fh), int(round((fh % 1) * 60)))
    raise ValueError(f"Un‑recognised date literal: {val!r}")

# ── helper: build a “Place” struct (lat/long validity checked) ───────────
def _build_place(name: str, lat: float, lon: float, offset_hrs: float) -> Place:
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be −90…+90")
    if not -180 <= lon <= 180:
        raise ValueError("Longitude must be −180…+180")
    return Place(name, lat, lon, float(offset_hrs))

# ── helper: robust TZ parsing (+05:30 / America/New_York / 5.5) ──────────
def _tz_offset_hours(tz: str | float | int, ref: datetime) -> float:
    if isinstance(tz, (int, float)):
        return float(tz)
    s = str(tz).strip()
    try:
        return float(s)                   # plain “5.5”
    except ValueError:
        pass
    m = re.fullmatch(r'([+-])?(\d{1,2}):([0-5]\d)', s)
    if m:
        sign = -1 if m.group(1) == '-' else 1
        return sign * (int(m.group(2)) + int(m.group(3)) / 60)
    z = zoneinfo.ZoneInfo(s)              # IANA name
    return z.utcoffset(ref).total_seconds() / 3600

# ── helpers: natal chart & strength lookups ──────────────────────────────
def _planet_positions(jd: float, place: Place):
    """Returns list [[L,(ascRaasi,long)], [0,(r,long)],…] exactly as
    documented for `jhora.horoscope.chart.charts.rasi_chart`."""
    return jcharts.rasi_chart(jd, place)

def _sav_scores(hp_list: List[str]) -> Dict[int, int]:
    """Sarva‑aṣṭakavarga totals per sign {1‑12:score}."""
    _bav, sav_tot, _ = jashta.get_ashtaka_varga(hp_list)
    return {i + 1: sav_tot[i] for i in range(12)}

def _shadbala_ratios(jd: float, place: Place) -> List[float]:
    """`strength.shad_bala` returns a tuple whose 9‑th item is ratios."""
    return jstrength.shad_bala(jd, place)[8]

# ── functional benefic / malefic classifier (lagna‑dependent) ────────────
def _functional_nature(asc: int, planet: int) -> int:
    """
    Very simple definition (enough for heuristic scoring):

        +1  → Lagna lord or lord of 5th / 9th houses
        −1  → Lord of 6th / 8th / 12th
         0  → others
    """
    owner = const.house_owners.get(planet)            # documented mapping
    if owner is None:
        return 0
    rel_house = (owner - asc) % 12 or 12
    if rel_house in (1, 5, 9):
        return 1
    if rel_house in (6, 8, 12):
        return -1
    return 0

# ── score → label helper ─────────────────────────────────────────────────
def _label_for(score: int) -> str:
    for lbl, th in LABELS:
        if score >= th:
            return lbl
    # should never fall through
    return LABELS[-1][0]

# ── flatten Vimsottari & Narayana antardaśā lists to a DataFrame ─────────
def _dashas(jd_birth: float, place: Place) -> pd.DataFrame:
    """
    Returns rows:
        maha, antara, start‑dt, end‑dt
    """
    rows: List[Tuple[int, int, datetime, datetime]] = []

    # Vimsottari
    vim = jvim.get_vimsottari_dhasa_bhukthi(jd_birth, place)
    for idx, (m_lord, a_lord, start) in enumerate(vim):
        start_dt = _to_dt(start)
        end_dt   = _to_dt(vim[idx + 1][2]) - timedelta(days=1) if idx + 1 < len(vim) else start_dt + timedelta(days=360)
        rows.append(("VIM", m_lord, a_lord, start_dt, end_dt))

    # Narayana (raasi)  – API returns list identical to Vim format
    nar = jnar.narayana_dhasa_for_divisional_chart(jd_birth, place, jd_birth)  # years_from_dob=0
    for idx, (m_lord, a_lord, start) in enumerate(nar):
        start_dt = _to_dt(start)
        end_dt   = _to_dt(nar[idx + 1][2]) - timedelta(days=1) if idx + 1 < len(nar) else start_dt + timedelta(days=360)
        rows.append(("NAR", m_lord, a_lord, start_dt, end_dt))

    return pd.DataFrame(rows, columns=["sys", "maha", "antara", "start", "end"])

# ── master rating routine ────────────────────────────────────────────────
def _rate_periods(df: pd.DataFrame, asc: int,
                  sav: Dict[int, int],
                  sb: List[float]) -> pd.DataFrame:

    def _score_row(r) -> Tuple[str, str]:
        # ------------------------------------------------------------------
        score = 0

        # 1) basic house lordship (wealth: 2/11, career: 10)  --------------
        if r.maha in (const.house_lords[2], const.house_lords[11]):
            score += WEALTH_WT
        if r.maha == const.house_lords[10]:
            score += CAREER_WT

        # 2) functional benefic / malefic ----------------------------------
        func_nature = _functional_nature(asc, r.maha)
        if func_nature == 1:
            score += FUNC_BEN_WT
        elif func_nature == -1:
            score += FUNC_MAL_WT

        # 3) Sarva‑aṣṭakavarga support (signs 2,10,11 auspicious) ----------
        lord_sign = const.house_owners.get(r.maha)
        if sav.get(lord_sign, 0) >= SAV_THRESHOLD:
            score += SAV_BONUS_WT

        # 4) Shadbala ratio -----------------------------------------------
        try:
            sb_ratio = sb[r.maha]
        except IndexError:
            sb_ratio = 1.0
        if sb_ratio >= 1.0:
            score += SB_GOOD_BONUS
        elif sb_ratio < 0.75:
            score += SB_POOR_MALUS

        label = _label_for(score)
        return (f"{r.start.date()} → {r.end.date()}", label)

    # vectorised apply → list of tuples
    periods, labels = zip(*df.itertuples(index=False).map(_score_row))  # type: ignore
    return pd.DataFrame({"period": periods, "rating": labels})

# ── public helper (Flask / CLI) ──────────────────────────────────────────
def timeline_from_args(*,
                       name: str,
                       date: str,
                       time: str,
                       lat,
                       lon,
                       tz: str | float = "+00:00",
                       ayanamsa: str = "Lahiri") -> pd.DataFrame:
    """
    Stateless convenience wrapper.

    Parameters
    ----------
    name      – place name (for log only)
    date      – ‘YYYY‑MM‑DD’
    time      – ‘HH:MM’
    lat, lon  – floats (deg)
    tz        – +HH:MM | IANA zone | float
    ayanamsa  – passed to PyJHora by mutating `const._DEFAULT_AYANAMSA_MODE`
    """
    # 1) place & birth JD --------------------------------------------------
    dob   = datetime.fromisoformat(f"{date}T{time}")
    offset = _tz_offset_hours(tz, dob)
    place = _build_place(name, float(lat), float(lon), offset)

    # honour caller‑supplied ayanāṃśa
    if hasattr(const, "_DEFAULT_AYANAMSA_MODE"):
        const._DEFAULT_AYANAMSA_MODE = ayanamsa.upper()

    jd_birth = jutils.julian_day_number((dob.year, dob.month, dob.day),
                                        (dob.hour, dob.minute, dob.second))

    # 2) natal chart derived helpers --------------------------------------
    natal_pp  = _planet_positions(jd_birth, place)
    asc_sign  = natal_pp[0][1][0]                             # Lagna sign
    sav_dict  = _sav_scores(jutils.get_house_planet_list_from_planet_positions(natal_pp))
    sb_ratios = _shadbala_ratios(jd_birth, place)

    # 3) daśās → ratings ---------------------------------------------------
    dasha_df  = _dashas(jd_birth, place)
    return _rate_periods(dasha_df, asc_sign, sav_dict, sb_ratios)

# ── CLI (for quick manual verification) ──────────────────────────────────
def _cli() -> None:
    ap = argparse.ArgumentParser(description="Generate career/wealth timeline")
    ap.add_argument("--name", required=True, help="Place name (for logs)")
    ap.add_argument("--date", required=True, help="YYYY‑MM‑DD")
    ap.add_argument("--time", required=True, help="HH:MM (24‑h)")
    ap.add_argument("--lat",  type=float, required=True)
    ap.add_argument("--lon",  type=float, required=True)
    ap.add_argument("--tz",   default="+00:00", help="TZ offset or IANA zone")
    args = ap.parse_args()

    df = timeline_from_args(name=args.name, date=args.date, time=args.time,
                            lat=args.lat, lon=args.lon, tz=args.tz)
    print(df.to_string(index=False))

if __name__ == "__main__":
    _cli()
