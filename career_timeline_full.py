# career_timeline_full.py
"""
PyJHora‑based “wealth / career timeline” engine.

Public surface
──────────────
• `timeline_from_args(...)`  – stateless helper for a Flask endpoint  
• CLI usage:  python career_timeline_full.py --help
"""

from __future__ import annotations

import argparse
import math
import zoneinfo
from datetime import datetime, timedelta
from typing import Iterable, Dict, List

import pandas as pd

# ── PyJHora core ────────────────────────────────────────────────────────────
from jhora import const
import jhora.utils as jutils                                 # ← utils.py ✔
from jhora.panchanga import drik as pdrik                    # ← drik.py ✔

# Horoscope helpers
from jhora.horoscope.chart import charts as jd_charts        # ← charts.py ✔
from jhora.horoscope.chart import house  as jd_house         # ← house.py ✔
from jhora.horoscope.chart import strength as jd_strength    # ← strength.py ✔
from jhora.horoscope.chart import ashtakavarga as jd_ashta    # ← ashtakavarga.py ✔
import jhora.horoscope.chart.yoga as jyoga

# Dasha engines
from jhora.horoscope.dhasa.graha import vimsottari as jd_vimsottari  # ← vimsottari.py ✔
from jhora.horoscope.dhasa.raasi import narayana  as jd_narayana     # ← narayana.py ✔

# ── heuristic weights ────────────────────────────────────────────────────
WEALTH_LORD_WT, CAREER_LORD_WT = 20, 15
SAV_BONUS_WT = 10
STRENGTH_BONUS, STRENGTH_MALUS = 10, -10
SHADBALA_GOOD, SHADBALA_BAD = 1.0, 0.75
SAV_WEALTH_TH, SAV_CAREER_TH = 28, 30

LABELS: tuple[tuple[str, int], ...] = (
    ("EXCELLENT", 50),
    ("GOOD",       35),
    ("NEUTRAL",    20),
    ("CHALLENGED",  0),
)

# ════════════════════════════════════════════════════════════════════════
# basic helpers
# ════════════════════════════════════════════════════════════════════════
def _build_place(name: str, lat: float, lon: float, offset_hrs: float) -> pdrik.Place:
    if not -90.0 <= lat <= 90.0:
        raise ValueError("Latitude must be −90…+90 °")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("Longitude must be −180…+180 °")
    return pdrik.Place(name, lat, lon, float(offset_hrs))


def _tz_to_offset_hours(tz_val: str | float | int, ref_dt: datetime) -> float:
    if isinstance(tz_val, (float, int)):
        return float(tz_val)
    try:
        return float(tz_val)  # handles "+05.5"
    except ValueError:
        z = zoneinfo.ZoneInfo(str(tz_val))
        return z.utcoffset(ref_dt).total_seconds() / 3600.0


def _planet_positions(jd: float, place: pdrik.Place):
    return jd_charts.rasi_chart(jd, place)


def _sign_of_longitude(lon: float) -> int:
    return int(lon // 30) % 12


# ════════════════════════════════════════════════════════════════════════
# Sarva‑aṣṭakavarga
# ════════════════════════════════════════════════════════════════════════
def _sav_scores(house_to_planet_list: List[str]) -> Dict[int, int]:
    _binna, sav_totals, _ = jd_ashta.get_ashtaka_varga(house_to_planet_list)
    return {i + 1: sav_totals[i] for i in range(12)}


# ════════════════════════════════════════════════════════════════════════
# daśā helpers
# ════════════════════════════════════════════════════════════════════════
def _tree_to_df(raw: Iterable, label: str) -> pd.DataFrame:
    rows = []
    for dasa_lord, bhukti_lord, start_str in raw:
        if dasa_lord != bhukti_lord:            # keep only mahā‑daśā rows
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.strip())
        except Exception:
            continue
        rows.append(dict(system=label,
                         level="maha",
                         lord=int(dasa_lord),
                         start=start_dt,
                         end=start_dt + timedelta(days=365)))  # ≈1 year
    return pd.DataFrame(rows)


def _dashas(dob: datetime, place: pdrik.Place,
            start_age: int = 18, span_years: int = 62):
    win1 = dob + timedelta(days=365.25 * start_age)
    win2 = win1 + timedelta(days=365.25 * span_years)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second))

    vim_raw = jd_vimsottari.get_vimsottari_dhasa_bhukthi(
        jd_birth, place, include_antardhasa=True)
    nar_raw = jd_narayana.narayana_dhasa_for_divisional_chart(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second),
        place,
        divisional_chart_factor=10,
        include_antardhasa=True)

    vim = _tree_to_df(vim_raw, "vim")
    nar = _tree_to_df(nar_raw, "nar")

    vim = vim[(vim.start >= win1) & (vim.start <= win2)]
    nar = nar[(nar.start >= win1) & (nar.start <= win2)]
    return vim, nar


# ════════════════════════════════════════════════════════════════════════
# transit trigger – Jupiter / Saturn on Lagna sign
# ════════════════════════════════════════════════════════════════════════
def _transit_key_hit(mid: datetime, natal_pp) -> bool:
    jd_mid = jutils.julian_day_number(
        (mid.year, mid.month, mid.day),
        (mid.hour, mid.minute, mid.second))
    tr_pp = _planet_positions(jd_mid,
                              _build_place("geo", 0.0, 0.0, 0.0))
    asc_sign = natal_pp[0][1][0]                         # Lagna index 0
    jup_sign = _sign_of_longitude(tr_pp[const._JUPITER + 1][1][1])
    sat_sign = _sign_of_longitude(tr_pp[const._SATURN  + 1][1][1])
    return asc_sign in (jup_sign, sat_sign)


# ════════════════════════════════════════════════════════════════════════
# scoring engine
# ════════════════════════════════════════════════════════════════════════
def _rate_periods(vim: pd.DataFrame, nar: pd.DataFrame,
                  sb_strengths: List[float],
                  sav: Dict[int, int],
                  natal_pp) -> pd.DataFrame:

    wealth_lords = {const._JUPITER, const._VENUS}
    career_lords = {const._SUN, const._MERCURY, const._SATURN, const._MARS}

    def _score(row) -> Dict[str, object]:
        start, end, lord = row.start, row.end, row.lord
        mid = start + (end - start) / 2
        score = 0

        # lordship
        if lord in wealth_lords:
            score += WEALTH_LORD_WT
        if lord in career_lords:
            score += CAREER_LORD_WT

        # SAV strength
        if lord in wealth_lords and all(sav.get(h, 0) >= SAV_WEALTH_TH for h in (2, 11)):
            score += SAV_BONUS_WT
        if lord in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            score += SAV_BONUS_WT

        # Shadbala
        sb_ratio = sb_strengths[lord] if 0 <= lord < len(sb_strengths) else 1.0
        if sb_ratio >= SHADBALA_GOOD:
            score += STRENGTH_BONUS
        elif sb_ratio < SHADBALA_BAD:
            score += STRENGTH_MALUS

        # label
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "EXCELLENT" and not _transit_key_hit(mid, natal_pp):
            label = "GOOD" if score >= 40 else "NEUTRAL"

        return dict(system=row.system,
                    level=row.level,
                    period=f"{start.date()} → {end.date()}",
                    lord=const.planet_symbols.get(lord, str(lord))
                         if hasattr(const, "planet_symbols") else str(lord),
                    score=score,
                    rating=label)

    combined = pd.concat([vim, nar], ignore_index=True)
    return pd.DataFrame([_score(r) for r in combined.itertuples(index=False)]
                        ).sort_values("period")


# ════════════════════════════════════════════════════════════════════════
# public helper (for Flask)
# ════════════════════════════════════════════════════════════════════════
def timeline_from_args(*, name: str, date: str, time: str,
                       lat, lon, tz: str | float = "+00:00") -> pd.DataFrame:
    lat, lon = float(lat), float(lon)
    dob = datetime.fromisoformat(f"{date}T{time}")
    offset = _tz_to_offset_hours(tz, dob)
    place = _build_place(name, lat, lon, offset)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second))
    natal_pp = _planet_positions(jd_birth, place)

    h_to_p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
    sav = _sav_scores(h_to_p)

    sb_strengths = jd_strength.shad_bala(jd_birth, place)[8]

    vim_df, nar_df = _dashas(dob, place)

    return _rate_periods(vim_df, nar_df, sb_strengths, sav, natal_pp)


# ════════════════════════════════════════════════════════════════════════
# simple CLI for quick testing
# ════════════════════════════════════════════════════════════════════════
def _cli() -> None:
    ap = argparse.ArgumentParser(description="Generate wealth/career timeline")
    ap.add_argument("--name", required=True)
    ap.add_argument("--date", required=True, help="YYYY‑MM‑DD")
    ap.add_argument("--time", required=True, help="HH:MM (24‑h)")
    ap.add_argument("--lat",  type=float, required=True)
    ap.add_argument("--lon",  type=float, required=True)
    ap.add_argument("--tz",   default="+00:00",
                    help="TZ offset hours or IANA zone")
    args = ap.parse_args()

    df = timeline_from_args(name=args.name, date=args.date, time=args.time,
                            lat=args.lat, lon=args.lon, tz=args.tz)
    out = f"timeline_{args.name.replace(' ', '_')}.csv"
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    _cli()
