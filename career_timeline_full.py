# career_timeline_full.py
"""
PyJHora-based “wealth / career timeline” engine
(only ‘period’ and ‘rating’ are exposed).

Public helpers
──────────────
• timeline_from_args(...) – handy for Flask or CLI
• CLI usage:  python career_timeline_full.py --help
"""
from __future__ import annotations

import argparse
import math
import re
import zoneinfo
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

import pandas as pd

# ─── PyJHora ────────────────────────────────────────────────────────────────
from jhora import const, utils as jutils
from jhora.panchanga import drik as pdrik
from jhora.horoscope.chart import charts as jd_charts
from jhora.horoscope.chart import strength as jd_strength
from jhora.horoscope.chart import ashtakavarga as jd_ashta
from jhora.horoscope.dhasa.graha import vimsottari as jd_vimsottari
from jhora.horoscope.dhasa.raasi import narayana   as jd_narayana

# ─── heuristic weights & labels ─────────────────────────────────────────────
WEALTH_LORD_WT, CAREER_LORD_WT = 20, 15
SAV_BONUS_WT                    = 10
STRENGTH_BONUS, STRENGTH_MALUS  = 10, -10
SHADBALA_GOOD, SHADBALA_BAD     = 1.0, 0.75
SAV_WEALTH_TH, SAV_CAREER_TH    = 28, 30

LABELS: tuple[tuple[str, int], ...] = (
    ("VERY_FAVOURABLE", 45),
    ("FAVOURABLE",      30),
    ("AVERAGE",         15),
    ("CHALLENGING",      0),
)

# ═══════════════════════════════════════════════════════════════════════════
# basic helpers
# ═══════════════════════════════════════════════════════════════════════════
_JD_THRESHOLD = 1_720_000         # anything above ⇒ treat as Julian-Day #

def _to_dt(val) -> datetime | None:
    """Return a datetime from many PyJHora date flavours."""
    # 1) ISO string ----------------------------------------------------------
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.strip())
        except ValueError:
            pass
    # 2) (y,m,d,...) tuple/list ---------------------------------------------
    if isinstance(val, (tuple, list)) and len(val) >= 3:
        try:
            y, m, d = map(int, val[:3])
            hh = int(val[3]) if len(val) > 3 else 0
            mm = int(val[4]) if len(val) > 4 else 0
            return datetime(y, m, d, hh, mm)
        except Exception:                      # noqa: BLE001
            pass
    # 3) Julian-day number ---------------------------------------------------
    if isinstance(val, (int, float)) and val > _JD_THRESHOLD:
        y, m, d, fh = jutils.jd_to_gregorian(float(val))
        return datetime(y, m, d, int(fh), int(round((fh % 1) * 60)))
    # 4) anything else → None ------------------------------------------------
    return None


def _build_place(name: str, lat: float, lon: float,
                 offset_hrs: float) -> pdrik.Place:
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be −90…+90")
    if not -180 <= lon <= 180:
        raise ValueError("Longitude must be −180…+180")
    return pdrik.Place(name, lat, lon, float(offset_hrs))


def _tz_to_offset_hours(tz_val: str | float | int,
                        ref_dt: datetime) -> float:
    """Convert numeric, ‘+HH:MM’, or IANA time-zone → offset hours."""
    # plain number -----------------------------------------------------------
    if isinstance(tz_val, (int, float)):
        return float(tz_val)
    tz_str = str(tz_val).strip()
    try:                       # e.g. "5.5"
        return float(tz_str)
    except ValueError:
        pass
    # explicit sign & minutes "+05:30" --------------------------------------
    m = re.fullmatch(r'([+-])?(\d{1,2}):([0-5]\d)', tz_str)
    if m:
        sign  = -1 if m.group(1) == '-' else 1
        hours = int(m.group(2))
        mins  = int(m.group(3))
        return sign * (hours + mins / 60)
    # named zone -------------------------------------------------------------
    try:
        z = zoneinfo.ZoneInfo(tz_str)
        return z.utcoffset(ref_dt).total_seconds() / 3600
    except Exception as e:                 # noqa: BLE001
        raise ValueError(f"Unsupported time-zone value '{tz_val}'") from e


def _planet_positions(jd: float, place: pdrik.Place):
    return jd_charts.rasi_chart(jd, place)


def _sign_of_longitude(lon: float) -> int:
    """0 = Aries … 11 = Pisces"""
    return int(lon // 30) % 12


# ═══════════════════════════════════════════════════════════════════════════
# Sarva-aṣṭakavarga
# ═══════════════════════════════════════════════════════════════════════════
def _sav_scores(house_to_planet_list: List[str]) -> Dict[int, int]:
    _binna, sav_totals, _ = jd_ashta.get_ashtaka_varga(house_to_planet_list)
    return {i + 1: sav_totals[i] for i in range(12)}


# ═══════════════════════════════════════════════════════════════════════════
# daśā helpers
# ═══════════════════════════════════════════════════════════════════════════
def _tree_to_df(raw, label: str) -> pd.DataFrame:
    """
    Walk any daśā tree (structure varies wildly between engines) and
    return only mahā-daśā rows with robust start/end datetimes.
    """
    rows: list[dict] = []

    def walk(node):
        if not isinstance(node, (list, tuple)):
            return
        # leaf candidate: leading int-ish lord + at least one date afterwards
        if node and isinstance(node[0], (int, float)):
            lord = int(node[0])
            dates = [(_to_dt(x), idx) for idx, x in enumerate(node[1:], 1)
                     if _to_dt(x) is not None]
            if dates:
                start_dt, start_idx = dates[0]
                # explicit end date present?
                end_dt = dates[1][0] if len(dates) > 1 else None
                if not end_dt:
                    # look for duration (float/int ≤ 120 yrs)
                    dur = next((float(x) for x in node[start_idx + 1:]
                                if isinstance(x, (int, float)) and 0 < x <= 120),
                               None)
                    end_dt = start_dt + timedelta(days=365.25 * (dur or 1.0))
                rows.append(dict(system=label, level="maha",
                                 lord=lord, start=start_dt, end=end_dt))
                return
        # recurse into children ---------------------------------------------
        for child in node:
            walk(child)

    walk(raw)
    cols = ["system", "level", "lord", "start", "end"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


# ═══════════════════════════════════════════════════════════════════════════
# daśā helpers
# ═══════════════════════════════════════════════════════════════════════════
def _dashas(dob: datetime, place: pdrik.Place,
            start_age: int = 18, span: int = 62):
    """
    Return Vimsottari & Narayana mahā‑daśās whose starts fall in the
    window [dob+start_age, dob+start_age+span).
    """
    win1 = dob + timedelta(days=365.25 * start_age)
    win2 = dob + timedelta(days=365.25 * (start_age + span))

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second)
    )

    # ── Vimsottari (graha) timeline ─────────────────────────────────────────
    vim_raw = jd_vimsottari.get_vimsottari_dhasa_bhukthi(jd_birth, place)

    # ── Narayana (rāśi) timeline – D10 variant ─────────────────────────────
    nar_raw = jd_narayana.narayana_dhasa_for_divisional_chart(
        jd_birth,                          # jd_at_dob
        place,                             # place struct
        (dob.year, dob.month, dob.day),    # dob tuple
        0,                                 # years_from_dob  (0 = divisional only)
        10                                 # divisional_chart_factor  (D‑10)
    )

    # flatten both trees to DataFrames with start/end datetimes
    vim_df = _tree_to_df(vim_raw, "vim")
    nar_df = _tree_to_df(nar_raw, "nar")

    # keep rows whose **start** lies inside the viewing window
    vim_df = vim_df[(win1 <= vim_df.start) & (vim_df.start <= win2)]
    nar_df = nar_df[(win1 <= nar_df.start) & (nar_df.start <= win2)]
    return vim_df, nar_df



# ═══════════════════════════════════════════════════════════════════════════
# transit trigger – Jupiter / Saturn over natal Lagna sign
# ═══════════════════════════════════════════════════════════════════════════
def _transit_key_hit(mid: datetime, natal_pp) -> bool:
    jd_mid = jutils.julian_day_number(
        (mid.year, mid.month, mid.day), (mid.hour, mid.minute, mid.second))
    tr_pp = _planet_positions(jd_mid, _build_place("geo", 0.0, 0.0, 0.0))
    asc_sign = natal_pp[0][1][0]                     # Lagna index 0
    jup_sign = _sign_of_longitude(tr_pp[const._JUPITER + 1][1][1])
    sat_sign = _sign_of_longitude(tr_pp[const._SATURN  + 1][1][1])
    return asc_sign in (jup_sign, sat_sign)


# ═══════════════════════════════════════════════════════════════════════════
# scoring engine
# ═══════════════════════════════════════════════════════════════════════════
def _rate_periods(vim: pd.DataFrame,
                  nar: pd.DataFrame,
                  sb_strengths: List[float],
                  sav: Dict[int, int],
                  natal_pp) -> pd.DataFrame:
    wealth_lords = {const._JUPITER, const._VENUS}
    career_lords = {const._SUN, const._MERCURY, const._SATURN, const._MARS}

    def _score(row) -> Dict[str, object]:
        start, end, lord = row.start, row.end, row.lord
        mid = start + (end - start) / 2
        score = 0

        # lordship ----------------------------------------------------------
        if lord in wealth_lords:
            score += WEALTH_LORD_WT
        if lord in career_lords:
            score += CAREER_LORD_WT

        # Sarva-aṣṭakavarga support -----------------------------------------
        if lord in wealth_lords and all(sav.get(h, 0) >= SAV_WEALTH_TH
                                        for h in (2, 11)):
            score += SAV_BONUS_WT
        if lord in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            score += SAV_BONUS_WT

        # Shadbala strength --------------------------------------------------
        sb_ratio = sb_strengths[lord] if 0 <= lord < len(sb_strengths) else 1.0
        if sb_ratio >= SHADBALA_GOOD:
            score += STRENGTH_BONUS
        elif sb_ratio < SHADBALA_BAD:
            score += STRENGTH_MALUS

        # label & transit veto ----------------------------------------------
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "VERY_FAVOURABLE" and not _transit_key_hit(mid, natal_pp):
            label = "FAVOURABLE" if score >= 40 else "AVERAGE"

        return dict(period=f"{start.date()} → {end.date()}",
                    rating=label)

    combined = pd.concat([vim, nar], ignore_index=True)
    scored   = [_score(r) for r in combined.itertuples(index=False)]
    return pd.DataFrame(scored).sort_values("period")


# ═══════════════════════════════════════════════════════════════════════════
# public entry point (Flask helper)
# ═══════════════════════════════════════════════════════════════════════════
def timeline_from_args(*, name: str, date: str, time: str,
                       lat, lon, tz: str | float = "+00:00") -> pd.DataFrame:
    lat, lon = float(lat), float(lon)
    dob      = datetime.fromisoformat(f"{date}T{time}")
    offset   = _tz_to_offset_hours(tz, dob)
    place    = _build_place(name, lat, lon, offset)

    jd_birth = jutils.julian_day_number(
        (dob.year, dob.month, dob.day),
        (dob.hour, dob.minute, dob.second)
    )
    natal_pp     = _planet_positions(jd_birth, place)
    sav_scores   = _sav_scores(
        jutils.get_house_planet_list_from_planet_positions(natal_pp))
    sb_strengths = jd_strength.shad_bala(jd_birth, place)[8]

    vim_df, nar_df = _dashas(dob, place)
    return _rate_periods(vim_df, nar_df, sb_strengths, sav_scores, natal_pp)


# ═══════════════════════════════════════════════════════════════════════════
# simple CLI  →  `python career_timeline_full.py --help`
# ═══════════════════════════════════════════════════════════════════════════
def _cli() -> None:
    ap = argparse.ArgumentParser(description="Generate wealth/career timeline")
    ap.add_argument("--name", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--time", required=True, help="HH:MM (24-h)")
    ap.add_argument("--lat",  type=float, required=True)
    ap.add_argument("--lon",  type=float, required=True)
    ap.add_argument("--tz",   default="+00:00",
                    help="TZ offset hours, '+HH:MM' or IANA zone")
    args = ap.parse_args()

    df  = timeline_from_args(name=args.name, date=args.date, time=args.time,
                             lat=args.lat, lon=args.lon, tz=args.tz)
    out = f"timeline_{args.name.replace(' ', '_')}.csv"
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    _cli()
