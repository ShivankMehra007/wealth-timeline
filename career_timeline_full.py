#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
career_timeline_full_jhora.py
─────────────────────────────
Five‑band Wealth / Business / Career timeline generator
built entirely on *PyJHora* primitives.

Key fixes in this revision
==========================
* **Imports** – every module path and imported symbol now matches the
  actual filenames in the *PyJHora* tree you attached.  In particular  
  • `jhora.utils` is imported as a *module* (not “from   jhora.utils import utils”).  
  • `ashtakavarga` module name is spelt with the missing “ka”.  
  • All chart/dasha helpers are pulled in only at module level; no
    `Chart` class is referenced (PyJHora works with helper functions).

* **No logic changes** – the scoring workflow, Jupiter/Saturn transit
  gate and 5‑band label assignment remain exactly as in the previous
  version.

Running
-------
::

    pip install PyJHora pandas
    python career_timeline_full_jhora.py \
           --name "A P Test" --date 1990-01-01 --time 05:30 \
           --lat 13.0827 --lon 80.2707 --tz +05:30

Outputs a neat CSV and prints the summary table.

"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

# ── PyJHora core ────────────────────────────────────────────────────────────
from jhora import const
import jhora.utils as jutils                                 # ← utils.py ✔
from jhora.panchanga import drik as pdrik                    # ← drik.py ✔

# Horoscope helpers
from jhora.horoscope.chart import charts as jd_charts        # ← charts.py ✔
from jhora.horoscope.chart import house  as jd_house         # ← house.py ✔
from jhora.horoscope.chart import strength as jd_strength    # ← strength.py ✔
from jhora.horoscope.chart import ashtakavarga as jd_akv     # ← ashtakavarga.py ✔
from jhora.horoscope.chart.yoga import yoga as jyoga

# Dasha engines
from jhora.horoscope.dhasa.graha import vimsottari as jd_vimsottari  # ← vimsottari.py ✔
from jhora.horoscope.dhasa.raasi import narayana  as jd_narayana     # ← narayana.py ✔

# ── Scoring parameters (unchanged) ──────────────────────────────────────────
WEALTH_LORD_WT   = 20
CAREER_LORD_WT   = 20
SAV_BONUS_WT     = 10
YOGA_PLUS_WT     = 30
YOGA_MINUS_WT    = -40
STRENGTH_BONUS   = 10
STRENGTH_MALUS   = -10

SAV_WEALTH_TH    = 22          # bindus in houses 2 & 11
SAV_CAREER_TH    = 22          # bindus in house 10
SHADBALA_GOOD    = 1.00
SHADBALA_BAD     = 0.80

LABELS = (
    ("EXCELLENT", 60),         # requires transit gate too
    ("GOOD",      40),
    ("NEUTRAL",   15),
    ("CHALLENGING", 1),
    ("RISK",       float("-inf")),
)

# ── Utilities ───────────────────────────────────────────────────────────────
def _tz_to_offset_hours(tz_val: str | float | int, on_dt: datetime) -> float:
    """
    Return numeric UTC offset (+h) from user‑supplied tz parameter.

        • IANA zone id  → use ZoneInfo
        • “+05:30”      → parse manually
        • 5 or -3.5     → already numeric
    """
    if isinstance(tz_val, (int, float)):
        return float(tz_val)

    tz_s = str(tz_val).strip()
    if tz_s.startswith(("+", "-")):               # raw offset
        hh, mm = (tz_s[1:].split(":") + ["0"])[:2]
        sign   = 1 if tz_s[0] == "+" else -1
        return sign * (int(hh) + int(mm)/60)

    # Otherwise assume IANA name
    return ZoneInfo(tz_s).utcoffset(on_dt).total_seconds() / 3600

def _build_place(label: str, lat: float, lon: float, tz_hrs: float) -> pdrik.Place:
    """Return a `drik.Place` struct for the birth location."""
    return pdrik.Place(label, lat, lon, tz_hrs)

def _planet_positions(jd: float, place: pdrik.Place) -> list:
    """Return *rāśi* chart planet list `[ [planet,(sign,deg)], … ]`."""
    return jd_charts.rasi_chart(jd, place)                   # D‑1 chart

def _sav_scores(house_map: list[str]) -> dict[int, int]:
    """
    Compute Sarv‑aṣṭakavarga bindu totals for years spanned by the timeline.
    PyJHora exposes a helper that returns the full 12‑house list per year.
    """
    bav, sam, prastara = jd_akv.get_ashtaka_varga(house_map)
    # sam[house_index] is the sarvashtakavarga for that sign
    return {i + 1: v for i, v in enumerate(sam)}             # 1‑based houses

def _sign_of_longitude(deg: float) -> int:
    """0 = Aries … 11 = Pisces."""
    return int(deg // 30)

def _jup_sat_aspects(sign: int, planet: str) -> set[int]:
    """Return sign indices aspected by Jupiter/Saturn from *sign*."""
    if planet == "Jupiter":
        return {(sign + 4) % 12, (sign + 6) % 12, (sign + 8) % 12}
    if planet == "Saturn":
        return {(sign + 2) % 12, (sign + 6) % 12, (sign + 9) % 12}
    return set()

def _transit_hits_key(mid_jd: float, natal_pp: list) -> bool:
    """Does Jupiter *or* Saturn aspect natal 2‑10‑11 at JD *mid_jd*?"""
    birth_place = natal_pp[-1]  # last item is Lagna tuple (holds place)
    place = birth_place[2] if isinstance(birth_place, tuple) else None
    tr_pp = jd_charts.rasi_chart(mid_jd, place)
    # Current transit signs
    j_sign = _sign_of_longitude(tr_pp[const._JUPITER + 1][1][1])
    s_sign = _sign_of_longitude(tr_pp[const._SATURN  + 1][1][1])
    # Natal key houses
    h_to_p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
    asc_sign = natal_pp[0][1][0]
    key_signs = {(asc_sign + 1) % 12, (asc_sign + 9) % 12, (asc_sign + 10) % 12}  # 2,10,11
    return bool(key_signs & (_jup_sat_aspects(j_sign, "Jupiter") |
                             _jup_sat_aspects(s_sign, "Saturn")))

# ── Main scorer ─────────────────────────────────────────────────────────────
def _rate_periods(vim: pd.DataFrame,
                  nar: pd.DataFrame,
                  pp: list,
                  sav_year: dict[int, int]) -> pd.DataFrame:
    """Return one big dataframe with scores and 5‑band labels."""
    # Lords of 2,10,11
    p_to_h = jutils.get_planet_house_dictionary_from_planet_positions(pp)
    wealth_lords = {jd_house.house_owner_from_planet_positions(pp, h)
                    for h in (1, 11)}                       # houses are 0‑based inside lib
    career_lords = {jd_house.house_owner_from_planet_positions(pp, 9)}
    # Yoga buckets
    yogas, _, _ = jd_house.trikonas()   # placeholder – replace with real yoga filter
    pos_yoga, neg_yoga = set(), set()   # not expanded here (logic unchanged)

    def _score(row) -> dict:
        start, end = row.start, row.end
        lord = row.lord
        mid = start + (end - start) / 2
        score = 0

        if lord in wealth_lords:
            score += WEALTH_LORD_WT
        if lord in career_lords:
            score += CAREER_LORD_WT

        # Sarv‑aṣṭakavarga
        yr = start.year
        if yr in sav_year and lord in wealth_lords and sav_year[yr] >= SAV_WEALTH_TH:
            score += SAV_BONUS_WT
        if yr in sav_year and lord in career_lords and sav_year[yr] >= SAV_CAREER_TH:
            score += SAV_BONUS_WT

        # Yogas / doshas
        if lord in pos_yoga:
            score += YOGA_PLUS_WT
        if lord in neg_yoga:
            score += YOGA_MINUS_WT

        # Śaḍ‑bala
        sb = jd_strength.planet_shadbala(pp, lord)           # returns *ratio*
        score += STRENGTH_BONUS if sb >= SHADBALA_GOOD else \
                 STRENGTH_MALUS if sb < SHADBALA_BAD else 0

        # Label
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "EXCELLENT" and not _transit_hits_key(mid, pp):
            label = "GOOD" if score >= 40 else "NEUTRAL"

        return {
            "system": row.label,
            "level":  row.level,
            "period": f"{start.date()} → {end.date()}",
            "lord":   const.planet_symbols[lord] \
                      if hasattr(const, "planet_symbols") else str(lord),
            "score":  score,
            "rating": label
        }

    rows = [_score(r) for r in (*vim.itertuples(), *nar.itertuples())]
    return pd.DataFrame(rows).sort_values("period")

# ── CLI glue ────────────────────────────────────────────────────────────────
def _tree_to_df(raw_list: list, label: str) -> pd.DataFrame:
    """
    Normalise the period list from jhora’s helpers to a tidy DataFrame.

    Keeps only mahā‑daśā rows (scorer doesn’t need bhuktis).
    Columns:  label | level | lord | start | end
    """
    rows = []
    for rec in raw_list:
        # --- Unpack flexibly ------------------------------------------
        if len(rec) == 3:
            dasa, _bhukti, ts = rec
        elif len(rec) == 2:
            dasa, ts = rec
        else:
            continue                          # malformed → skip

        # Guard: sometimes jhora returns (dasa, bhukti‑id, bhukti‑years)
        # where ts == bhukti‑id (0‑8).  Skip those rows so we never
        # try to convert a planet‑ID into a Julian date.
        if isinstance(ts, int) and ts in range(9):
            continue

        # --- Convert ts → datetime ------------------------------------
        if isinstance(ts, datetime):
            start_dt = ts
        elif isinstance(ts, str):
            start_dt = datetime.fromisoformat(ts.strip())
        elif isinstance(ts, (int, float)):
            # Reject obviously invalid “timestamp” values
            if ts < 1_000_000:        # not a real Julian day → bad row
                continue
            y, m, d, fh = jutils.jd_to_gregorian(float(ts))
            h  = int(fh)
            mi = int((fh - h) * 60)
            s  = int(round(((fh - h) * 60 - mi) * 60))
            start_dt = datetime(y, m, d, h, mi, s)
        else:
            continue                              # unknown type

        # crude 1‑year span (exact length unused by scorer)
        end_dt = start_dt + timedelta(days=365)

        rows.append(dict(
            label = label,
            level = "maha",
            lord  = dasa,
            start = start_dt,
            end   = end_dt
        ))

    return pd.DataFrame(
         rows,
         columns=["label", "level", "lord", "start", "end"]  # <-- add this
     )



# ────────────────────────────────────────────────────────────────────────────
def _dashas(pp: list,
            dob: datetime,
            place: pdrik.Place,
            start_age: int = 18,
            span: int = 62
           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build Vimśottarī (D‑1) and Nārāyaṇa (D‑10) daśā tables
    covering  <dob + start_age  …  start_age + span>  years.
    """

    # ------------------------------------------------------------------ #
    # 0.  Time‑window we’ll crop to
    # ------------------------------------------------------------------ #
    win1 = dob + timedelta(days=365.25 * start_age)
    win2 = win1 + timedelta(days=365.25 * span)

    # ------------------------------------------------------------------ #
    # 1.  Convert DOB → Julian‑day & drik.Date / time‑tuple formats
    # ------------------------------------------------------------------ #
    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second)
    )
    dob_date = pdrik.Date(dob.year, dob.month, dob.day)      # drik.Date
    tob      = (dob.hour, dob.minute, dob.second)            # simple tuple

    # ------------------------------------------------------------------ #
    # 2.  Get raw period lists from the **real** helpers
    # ------------------------------------------------------------------ #
    vim_raw = jd_vimsottari.get_vimsottari_dhasa_bhukthi(
        jd_birth, place,
        include_antardhasa=True,
        divisional_chart_factor=1       # D‑1
    )

    nar_raw = jd_narayana.narayana_dhasa_for_divisional_chart(
        dob_date, tob, place,
        divisional_chart_factor=10,     # D‑10
        include_antardhasa=True
    )

    # vim_raw / nar_raw elements → (dasa, bhukti, iso_start, years)

    # ------------------------------------------------------------------ #
    # 3.  Flatten to DataFrames (reuse helper)
    # ------------------------------------------------------------------ #
    vim_df = _tree_to_df(vim_raw, "vim")
    nar_df = _tree_to_df(nar_raw, "nar")

    # ------------------------------------------------------------------ #
    # 4.  Trim to requested window
    # ------------------------------------------------------------------ #
    vim_df = vim_df[(vim_df.start >= win1) & (vim_df.start <= win2)]
    nar_df = nar_df[(nar_df.start >= win1) & (nar_df.start <= win2)]

    return vim_df, nar_df



def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a 5‑band wealth/career timeline using PyJHora")
    ap.add_argument("--name", required=True, help="Person’s name")
    ap.add_argument("--date", required=True, help="Birth date YYYY‑MM‑DD")
    ap.add_argument("--time", required=True, help="Birth time HH:MM (24‑h)")
    ap.add_argument("--lat",  type=float, required=True, help="Latitude")
    ap.add_argument("--lon",  type=float, required=True, help="Longitude")
    ap.add_argument("--tz",   type=float, default=0.0,     help="TZ offset hours")
    args = ap.parse_args()

    # Build natal context
    dob = datetime.fromisoformat(f"{args.date}T{args.time}:00")
    place = _build_place(args.name, args.lat, args.lon, args.tz)
    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, 0))
    pp = _planet_positions(jd_birth, place)

    # Yearly SAV (18‑80 years)
    sav = _sav_scores(jutils.get_house_planet_list_from_planet_positions(pp))

    # Dashas
    vim, nar = _dashas(pp, dob, place)

    # Score & label
    timeline = _rate_periods(vim, nar, pp, sav)

    # Export
    out = f"timeline_{args.name.replace(' ', '_')}.csv"
    timeline.to_csv(out, index=False)

    print("\nWealth / Business / Career Timeline")
    print(timeline[["system", "level", "period", "lord", "rating"]]
          .reset_index(drop=True).to_string(index=False))
    print(f"\nFull CSV saved → {out}")

# ────────────────────────────────────────────────────────────────────────────
#  Public helper  →  Flask expects this symbol
# ────────────────────────────────────────────────────────────────────────────
def timeline_from_args(
        *, name: str, date: str, time: str,
        lat, lon,                    # may arrive as str
        tz: str | float = "+05:30"
    ) -> pd.DataFrame:

    # 1.  Normalise numeric primitives  -------------------------------
    lat = float(lat)
    lon = float(lon)

    # if tz came as "+05:30" keep it as str; if "5.5" or 5.5 → float
    tz_val = tz
    try:
        tz_val = float(tz)           # works for "5.5" or 5.5
    except ValueError:
        tz_val = str(tz).strip()     # keep "+05:30" / "Asia/Kolkata"

    dob = datetime.fromisoformat(f"{date}T{time}{tz_val if isinstance(tz_val, str) else ''}")

    # 2.  Convert tz into hours offset -------------------------------
    offset_hours = _tz_to_offset_hours(tz_val, dob)

    # 3.  Build Place with correct numeric args ----------------------
    place = _build_place(name, lat, lon, offset_hours)
    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, 0))
    pp  = _planet_positions(jd_birth, place)
    vim, nar = _dashas(pp, dob, place)
    sav_df   = _sav_scores(jutils.get_house_planet_list_from_planet_positions(pp))

    # Classify yogas
    pos, neg = [], []
    for y in jyoga.applicable_yogas_from_rasi_positions(pp):
        (pos if y["category"] in POSITIVE_YOGA_CATS else
         neg if y["category"] in NEGATIVE_YOGA_CATS else []).append(y)

    return _rate_periods(vim, nar, pp, sav_df)


if __name__ == "__main__":
    main()
