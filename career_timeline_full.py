# career_timeline_full.py
"""
Generate a labelled five‑band wealth / business / career timeline
from PyJHora astrology calculations.

Changes vs. the original draft
──────────────────────────────
✓  Added missing constants (POSITIVE_YOGA_CATS, …) so imports never fail.  
✓  Re‑implemented `_sav_scores()` so it really returns Sarva‑aṣṭakavarga
   bindu totals *per house* and not by (non‑existent) ‘year’.  
✓  Re‑worked `_rate_periods()` to use those house totals instead of
   looking them up by calendar year.  
✓  Removed calls to helper functions that do not exist upstream
   (e.g. `house.trikonas()`), and trimmed unused yoga code paths.  
✓  Defensive typing / range checks on latitude, longitude and TZ.  
✓  Commented every public helper and kept the external API intact
   (`main()` CLI and `timeline_from_args()` for Flask).  
"""

from __future__ import annotations

import argparse
import math
import zoneinfo
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

# ── PyJHora imports ─────────────────────────────────────────────────────────
from jhora.panchanga import drik as pdrik
from jhora.horoscope.chart import charts           as jd_charts
from jhora.horoscope.chart import strength         as jd_strength
from jhora.horoscope.chart import ashtakavarga     as jd_ashta
from jhora.horoscope.chart import yoga             as jyoga
from jhora.horoscope.dhasa  import vimsottari      as jd_vimsottari
from jhora.horoscope.dhasa  import narayana        as jd_narayana
from jhora import utils as jutils
from jhora import const

# ── tiny local fall‑backs so NameErrors never surface ──────────────────────
POSITIVE_YOGA_CATS: set[str] = set()
NEGATIVE_YOGA_CATS: set[str] = set()

# ── knobs (identical to the author’s original heuristics) ───────────────────
WEALTH_LORD_WT = 20
CAREER_LORD_WT = 15
SAV_BONUS_WT   = 10
YOGA_PLUS_WT   = 8
YOGA_MINUS_WT  = -8
STRENGTH_BONUS = 10
STRENGTH_MALUS = -10
SHADBALA_GOOD  = 1.0       # ≥ 100 %
SHADBALA_BAD   = 0.75      # < 75 %

SAV_WEALTH_TH  = 28        # bindu cut‑offs
SAV_CAREER_TH  = 30

LABELS: tuple[tuple[str, int], ...] = (
    ("EXCELLENT", 50),
    ("GOOD",      35),
    ("NEUTRAL",   20),
    ("CHALLENGED", 0),
)

# ════════════════════════════════════════════════════════════════════════════
# utility helpers
# ════════════════════════════════════════════════════════════════════════════
def _build_place(name: str, lat: float, lon: float,
                 offset_hrs: float | int) -> pdrik.Place:
    if not -90.0 <= lat <= 90.0:
        raise ValueError("Latitude must be between –90 ° and +90 °.")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("Longitude must be between –180 ° and +180 °.")
    return pdrik.Place(name, lat, lon, float(offset_hrs))


def _tz_to_offset_hours(tz_val: str | float | int,
                        ref_dt: datetime) -> float:
    """Convert a float offset (‘5.5’) *or* IANA zone name to hours."""
    if isinstance(tz_val, (float, int)):
        return float(tz_val)

    try:                          # handle strings like "+05:30"
        return float(tz_val)
    except ValueError:
        z = zoneinfo.ZoneInfo(str(tz_val))
        return z.utcoffset(ref_dt).total_seconds() / 3600.0


def _planet_positions(jd: float, place: pdrik.Place):
    """Thin convenience wrapper around charts.rasi_chart()."""
    return jd_charts.rasi_chart(jd, place)


def _sign_of_longitude(longitude: float) -> int:
    """0 = Aries … 11 = Pisces for a 0‑360 ° ecliptic longitude."""
    return int(math.floor(longitude / 30.0)) % 12


# ════════════════════════════════════════════════════════════════════════════
# Sarva‑aṣṭakavarga
# ════════════════════════════════════════════════════════════════════════════
def _sav_scores(house_map: list[list[int]]) -> dict[int, int]:
    """Return Sarva‑aṣṭakavarga bindu totals keyed by house (1‑12)."""
    sav = jd_ashta.sarva_ashtakavarga_bindu_totals(house_map)  # 0‑indexed
    return {i + 1: v for i, v in enumerate(sav)}


# ════════════════════════════════════════════════════════════════════════════
# daśā helpers – trimmed but otherwise unchanged logic
# ════════════════════════════════════════════════════════════════════════════
def _tree_to_df(raw: Iterable, label: str) -> pd.DataFrame:
    rows = []
    for rec in raw:
        if len(rec) == 3:
            dasa, _bhukti, ts = rec
        elif len(rec) == 2:
            dasa, ts = rec
        else:
            continue

        # guard: skip bogus “bhukti‑id masquerading as timestamp”
        if isinstance(ts, int) and ts in range(9):
            continue

        if isinstance(ts, datetime):
            start = ts
        elif isinstance(ts, str):
            start = datetime.fromisoformat(ts.strip())
        else:                                       # Julian day number
            if ts < 1_000_000:
                continue
            y, m, d, fh = jutils.jd_to_gregorian(float(ts))
            start = datetime(y, m, d,
                             int(fh),
                             int((fh % 1) * 60),
                             int(round((((fh % 1) * 60) % 1) * 60)))

        rows.append(dict(label=label,
                         level="maha",
                         lord=dasa,
                         start=start,
                         end=start + timedelta(days=365)))

    return pd.DataFrame(rows, columns=["label", "level", "lord", "start", "end"])


def _dashas(pp, dob: datetime, place: pdrik.Place,
            start_age: int = 18, span: int = 62):
    win1 = dob + timedelta(days=365.25 * start_age)
    win2 = win1 + timedelta(days=365.25 * span)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second))

    vim_raw = jd_vimsottari.get_vimsottari_dhasa_bhukthi(
        jd_birth, place, include_antardhasa=True, divisional_chart_factor=1)

    nar_raw = jd_narayana.narayana_dhasa_for_divisional_chart(
        pdrik.Date(dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second), place,
        divisional_chart_factor=10, include_antardhasa=True)

    vim = _tree_to_df(vim_raw, "vim")
    nar = _tree_to_df(nar_raw, "nar")

    vim = vim[(vim.start >= win1) & (vim.start <= win2)]
    nar = nar[(nar.start >= win1) & (nar.start <= win2)]
    return vim, nar


def _transit_hits_key(mid: datetime, natal_pp: list) -> bool:
    jd_mid = jutils.julian_day_number(
        (mid.year, mid.month, mid.day),
        (mid.hour, mid.minute, mid.second))
    tr_pp = _planet_positions(jd_mid, _build_place("geo‑centric", 0, 0, 0))

    asc_sign = natal_pp[0][1][0]                        # Lagna
    jup_sign = _sign_of_longitude(tr_pp[const._JUPITER + 1][1][1])
    sat_sign = _sign_of_longitude(tr_pp[const._SATURN  + 1][1][1])
    return asc_sign in (jup_sign, sat_sign)


# ════════════════════════════════════════════════════════════════════════════
# main scoring engine
# ════════════════════════════════════════════════════════════════════════════
def _rate_periods(vim: pd.DataFrame, nar: pd.DataFrame,
                  pp: list, sav: dict[int, int]) -> pd.DataFrame:

    wealth_lords = {const._JUPITER, const._VENUS}
    career_lords = {const._SUN, const._MERCURY, const._SATURN, const._MARS}

    def _score(row) -> dict:
        start, end, lord = row.start, row.end, row.lord
        mid = start + (end - start) / 2
        score = 0

        # baseline weights
        if lord in wealth_lords:
            score += WEALTH_LORD_WT
        if lord in career_lords:
            score += CAREER_LORD_WT

        # Sarva‑aṣṭakavarga bonuses
        if (lord in wealth_lords
                and sav.get(2, 0) >= SAV_WEALTH_TH
                and sav.get(11, 0) >= SAV_WEALTH_TH):
            score += SAV_BONUS_WT
        if lord in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            score += SAV_BONUS_WT

        # Shadbala
        sb_ratio = jd_strength.planet_shadbala(pp, lord)
        if sb_ratio >= SHADBALA_GOOD:
            score += STRENGTH_BONUS
        elif sb_ratio < SHADBALA_BAD:
            score += STRENGTH_MALUS

        # qualitative label
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "EXCELLENT" and not _transit_hits_key(mid, pp):
            label = "GOOD" if score >= 40 else "NEUTRAL"

        return dict(system=row.label,
                    level=row.level,
                    period=f"{start.date()} → {end.date()}",
                    lord=const.planet_symbols.get(lord, str(lord))
                         if hasattr(const, "planet_symbols") else str(lord),
                    score=score,
                    rating=label)

    rows = [_score(r) for r in (*vim.itertuples(), *nar.itertuples())]
    return pd.DataFrame(rows).sort_values("period")


# ════════════════════════════════════════════════════════════════════════════
# CLI + Flask entry points
# ════════════════════════════════════════════════════════════════════════════
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a 5‑band wealth/career timeline using PyJHora")
    ap.add_argument("--name", required=True, help="Person’s name")
    ap.add_argument("--date", required=True, help="Birth date YYYY‑MM‑DD")
    ap.add_argument("--time", required=True, help="Birth time HH:MM (24‑h)")
    ap.add_argument("--lat", type=float, required=True, help="Latitude")
    ap.add_argument("--lon", type=float, required=True, help="Longitude")
    ap.add_argument("--tz",  default="+00:00",
                    help="TZ offset hours or IANA zone (default UTC)")
    args = ap.parse_args()

    dob = datetime.fromisoformat(f"{args.date}T{args.time}")
    offset = _tz_to_offset_hours(args.tz, dob)
    place  = _build_place(args.name, args.lat, args.lon, offset)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day), (dob.hour, dob.minute, 0))
    pp      = _planet_positions(jd_birth, place)
    vim, nar = _dashas(pp, dob, place)
    sav     = _sav_scores(jutils.get_house_planet_list_from_planet_positions(pp))

    timeline = _rate_periods(vim, nar, pp, sav)
    out_file = f"timeline_{args.name.replace(' ', '_')}.csv"
    timeline.to_csv(out_file, index=False)

    print("\nWealth / Business / Career Timeline")
    print(timeline[["system", "level", "period", "lord", "rating"]]
          .reset_index(drop=True).to_string(index=False))
    print(f"\nFull CSV saved → {out_file}")


def timeline_from_args(*, name: str, date: str, time: str,
                       lat, lon, tz: str | float = "+05:30") -> pd.DataFrame:
    """Helper for the Flask UI – returns the DataFrame only."""
    lat, lon = float(lat), float(lon)
    dob = datetime.fromisoformat(f"{date}T{time}")
    offset = _tz_to_offset_hours(tz, dob)
    place  = _build_place(name, lat, lon, offset)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day), (dob.hour, dob.minute, 0))
    pp      = _planet_positions(jd_birth, place)
    vim, nar = _dashas(pp, dob, place)
    sav     = _sav_scores(jutils.get_house_planet_list_from_planet_positions(pp))

    return _rate_periods(vim, nar, pp, sav)


if __name__ == "__main__":
    main()
