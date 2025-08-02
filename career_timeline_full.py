"""
Divisional‑confirmed “wealth / career timeline” engine – drop‑in
replacement for the previous *career_timeline_full.py*.

Changes vs previous version
───────────────────────────
1. **Divisional‑chart confirmation** (logic A from the spec)
   • Wealth indications (lords Jupiter/Venus) are double‑checked in D‑2 (Hora)
     **and** D‑11 (Ekadaśāṁśa).
   • Career indications (lords Sun/Mercury/Mars/Saturn) are confirmed in the
     D‑10 (Daśāṁśa).
   • The dignity of the antar‑daśā lord in the varga is compared with its
     dignity in D‑1:  
       – same or better ⇒ +10 pts  
       – one tier worse ⇒ +5 pts  
       – ≥ two tiers worse ⇒ –5 pts
2. Sarva‑Aṣṭakavarga, Śad‑bala and transit veto are unchanged.
3. API & surface remain identical apart from the internal `_rate_periods()`
   now receiving a new `vargas` argument – populated automatically by
   `timeline_from_args()` – so callers need **no** change.

Only the two columns **period** and **rating** are returned, exactly as before.
"""
from __future__ import annotations

import argparse
import re
import zoneinfo
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

import pandas as pd
from jhora.horoscope.chart import yoga as jd_yoga
from jhora.horoscope.chart import raja_yoga as jd_raja

# ── PyJHora ────────────────────────────────────────────────────────────────
from jhora import const, utils as jutils
from jhora.panchanga import drik as pdrik
from jhora.horoscope.chart import charts as jd_charts
from jhora.horoscope.chart import strength as jd_strength
from jhora.horoscope.chart import ashtakavarga as jd_ashta
from jhora.horoscope.dhasa.graha import vimsottari as jd_vimsottari
from jhora.horoscope.dhasa.raasi import narayana as jd_narayana

# ── heuristic weights & labels ─────────────────────────────────────────────
WEALTH_LORD_WT, CAREER_LORD_WT = 20, 15
SAV_BONUS_WT = 10
STRENGTH_BONUS, STRENGTH_MALUS = 10, -10
DIV_EQUAL_BONUS, DIV_MINOR_BONUS, DIV_PENALTY = 10, 5, -5
SHADBALA_GOOD, SHADBALA_BAD = 1.0, 0.75
SAV_WEALTH_TH, SAV_CAREER_TH = 28, 30
# ―― Yoga/Doṣa weights ――
RAJA_BONUS = 15
DHANA_BONUS = 12
VIPAREETA_BONUS = 7
KEMADRUMA_PENALTY = -12

LABELS: tuple[tuple[str, int], ...] = (
    ("EXCELLENT", 50),
    ("GOOD",       35),
    ("NEUTRAL",    20),
    ("CHALLENGED",  0),
)

# ═══════════════════════════════════════════════════════════════════════════
# basic helpers
# ═══════════════════════════════════════════════════════════════════════════
_JD_THRESHOLD = 1_720_000  # anything above ⇒ treat as Julian‑Day number


def _to_dt(val) -> datetime | None:
    """Return *datetime* from ISO string, (y,m,d,…) tuple or a Julian‑day."""
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.strip())
        except ValueError:
            pass
    if isinstance(val, (tuple, list)) and len(val) >= 3:
        try:
            y, m, d = map(int, val[:3])
            hh = int(val[3]) if len(val) > 3 else 0
            mm = int(val[4]) if len(val) > 4 else 0
            return datetime(y, m, d, hh, mm)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(val, (int, float)) and val > _JD_THRESHOLD:
        y, m, d, fh = jutils.jd_to_gregorian(float(val))
        return datetime(y, m, d, int(fh), int(round((fh % 1) * 60)))
    return None


def _build_place(name: str, lat: float, lon: float, offset_hrs: float) -> pdrik.Place:
    if not -90 <= lat <= 90:
        raise ValueError("Latitude must be −90…+90")
    if not -180 <= lon <= 180:
        raise ValueError("Longitude must be −180…+180")
    return pdrik.Place(name, lat, lon, float(offset_hrs))


def _tz_to_offset_hours(tz_val: str | float | int, ref_dt: datetime) -> float:
    """Numeric, "+HH:MM", or IANA zone → offset (hours)."""
    if isinstance(tz_val, (int, float)):
        return float(tz_val)
    tz_str = str(tz_val).strip()
    try:
        return float(tz_str)  # e.g. "5.5"
    except ValueError:
        pass
    m = re.fullmatch(r"([+-])?(\d{1,2}):([0-5]\d)", tz_str)
    if m:
        sign = -1 if m.group(1) == '-' else 1
        hrs, mins = int(m.group(2)), int(m.group(3))
        return sign * (hrs + mins / 60)
    try:
        z = zoneinfo.ZoneInfo(tz_str)
        return z.utcoffset(ref_dt).total_seconds() / 3600
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Unsupported time‑zone value '{tz_val}'") from e


def _planet_positions(jd: float, place: pdrik.Place):
    return jd_charts.rasi_chart(jd, place)


def _sign_of_longitude(lon: float) -> int:
    """0 = Aries … 11 = Pisces"""
    return int(lon // 30) % 12

# ═══════════════════════════════════════════════════════════════════════════
# dignity helpers – classical relationships hard‑coded (books‑safe)
# ═══════════════════════════════════════════════════════════════════════════
# ­Sign lords (0 = Aries) – Rahu/Ketu not used here
_SIGN_LORD = [const._MARS,   # Aries
              const._VENUS,  # Taurus
              const._MERCURY,  # Gemini
              const._MOON,   # Cancer
              const._SUN,    # Leo
              const._MERCURY,  # Virgo
              const._VENUS,  # Libra
              const._MARS,   # Scorpio
              const._JUPITER,  # Sagittarius
              const._SATURN,   # Capricorn
              const._SATURN,   # Aquarius
              const._JUPITER]  # Pisces

# Exaltation / debilitation signs
_EXALTS = {
    const._SUN: 0, const._MOON: 1, const._MARS: 9, const._MERCURY: 5,
    const._JUPITER: 3, const._VENUS: 11, const._SATURN: 6,
}
_DEBILS = {k: (v + 6) % 12 for k, v in _EXALTS.items()}

# Planetary friendships (per BPHS – permanent)
_FRIENDS = {
    const._SUN: {const._MOON, const._MARS, const._JUPITER},
    const._MOON: {const._SUN, const._MERCURY},
    const._MARS: {const._SUN, const._MOON, const._JUPITER},
    const._MERCURY: {const._SUN, const._VENUS},
    const._JUPITER: {const._SUN, const._MOON, const._MARS},
    const._VENUS: {const._MERCURY, const._SATURN},
    const._SATURN: {const._MERCURY, const._VENUS},
}

_NEUTRALS = {
    const._SUN: {const._MERCURY},
    const._MOON: {const._MARS, const._JUPITER, const._SATURN},
    const._MARS: {const._VENUS, const._SATURN},
    const._MERCURY: {const._MARS, const._JUPITER, const._SATURN},
    const._JUPITER: {const._SATURN},
    const._VENUS: {const._MARS, const._JUPITER},
    const._SATURN: {const._JUPITER},
}

# any lord not in friends or neutrals ⇒ enemy

def _dignity_level(planet: int, sign: int) -> int:
    """Return integer tier: +3 exalt … –2 debilitation."""
    if sign == _EXALTS.get(planet):
        return 3
    if sign == _DEBILS.get(planet):
        return -2
    if _SIGN_LORD[sign] == planet:
        return 2  # own
    lord = _SIGN_LORD[sign]
    if lord in _FRIENDS.get(planet, set()):
        return 1
    if lord in _NEUTRALS.get(planet, set()):
        return 0
    return -1  # enemy


def _planet_sign_in_chart(house_to_planet_list: List[object], planet: int) -> int | None:
    """Locate *planet* in a house‑to‑planet mapping (Rāśi or divisional).

    The helper copes with three observed cell formats produced by PyJHora:
      1. A **string**      e.g. "L/5/2"  (slash‑separated ids)
      2. A **list/tuple**  e.g. ["L", 5]  *or* ["L", [5, 26.8]]
      3. Empty string / list for vacant signs.
    """
    for sign_idx, cell in enumerate(house_to_planet_list):
        if cell in (None, "", []):
            continue

        # normalise → iterable of token strings
        if isinstance(cell, str):
            tokens: Iterable[str] = cell.split("/")
        elif isinstance(cell, (list, tuple)):
            # flatten nested lists like ["L", [5, 26.8]] → ["L", "5"]
            flat: List[str] = []
            for item in cell:
                if isinstance(item, (list, tuple)):
                    flat.extend(map(str, item))
                else:
                    flat.append(str(item))
            tokens = flat
        else:
            tokens = [str(cell)]

        for t in tokens:
            if t == "L":
                continue
            try:
                if int(float(t)) == planet:
                    return sign_idx
            except ValueError:
                continue
    return None

# ═══════════════════════════════════════════════════════════════════════════
# Sarva‑aṣṭakavarga helper
# ═══════════════════════════════════════════════════════════════════════════

def _sav_scores(house_to_planet_list: List[str]) -> Dict[int, int]:
    _binna, sav_totals, _ = jd_ashta.get_ashtaka_varga(house_to_planet_list)
    return {i + 1: sav_totals[i] for i in range(12)}

# ═══════════════════════════════════════════════════════════════════════════
# daśā helpers
# ═══════════════════════════════════════════════════════════════════════════

def _tree_to_df(raw, label: str) -> pd.DataFrame:
    """Flatten any daśā tree & keep *mahā* daśās only (robust date parsing)."""
    rows: list[dict] = []

    def walk(node):
        if not isinstance(node, (list, tuple)):
            return
        if len(node) >= 2 and (dt := _to_dt(node[-1])) is not None:
            try:
                lord = int(float(node[0]))
            except Exception:  # noqa: BLE001
                lord = None
            if lord is not None:
                rows.append(dict(system=label, level="maha", lord=lord,
                                 start=dt, end=dt + timedelta(days=365)))
                return
        for child in node:
            walk(child)

    walk(raw)
    cols = ["system", "level", "lord", "start", "end"]
    return (pd.DataFrame(rows, columns=cols)
            if rows else pd.DataFrame(columns=cols))


def _dashas(dob: datetime, place: pdrik.Place, start_age: int = 18, span: int = 62):
    win1 = dob + timedelta(days=365.25 * start_age)
    win2 = dob + timedelta(days=365.25 * (start_age + span))

    jd_birth = jutils.julian_day_number((dob.year, dob.month, dob.day),
                                        (dob.hour, dob.minute, dob.second))

    vim_raw = jd_vimsottari.get_vimsottari_dhasa_bhukthi(jd_birth, place)
    nar_raw = jd_narayana.narayana_dhasa_for_divisional_chart(
        (dob.year, dob.month, dob.day), (dob.hour, dob.minute, dob.second),
        place, divisional_chart_factor=10)

    vim_df = _tree_to_df(vim_raw, "vim")
    nar_df = _tree_to_df(nar_raw, "nar")

    vim_df = vim_df[(win1 <= vim_df.start) & (vim_df.start <= win2)]
    nar_df = nar_df[(win1 <= nar_df.start) & (nar_df.start <= win2)]
    return vim_df, nar_df

# ═══════════════════════════════════════════════════════════════════════════
# transit trigger – Jupiter/Saturn over natal Lagna sign
# ═══════════════════════════════════════════════════════════════════════════

def _transit_key_hit(mid: datetime, natal_pp) -> bool:
    jd_mid = jutils.julian_day_number((mid.year, mid.month, mid.day),
                                      (mid.hour, mid.minute, mid.second))
    tr_pp = _planet_positions(jd_mid, _build_place("geo", 0.0, 0.0, 0.0))
    asc_sign = natal_pp[0][1][0]  # Lagna index 0
    jup_sign = _sign_of_longitude(tr_pp[const._JUPITER + 1][1][1])
    sat_sign = _sign_of_longitude(tr_pp[const._SATURN + 1][1][1])
    return asc_sign in (jup_sign, sat_sign)

# ═══════════════════════════════════════════════════════════════════════════
# scoring engine (includes divisional confirmation)
# ═══════════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────
# Yoga & Doṣa evaluation helper
# ────────────────────────────────────────────────────────────────

def _yoga_bonus(planet: int, h2p: List[str], p2h: Dict[int, int], asc_house: int | None) -> int:
    """Return cumulative bonus/penalty for yogas & doṣas involving *planet*."""
    score = 0
    # Rāja‑yoga participation
    try:
        jd_raja_yoga_pairs = jd_raja.get_raja_yoga_pairs(h2p)
        for p1, p2 in jd_raja_yoga_pairs:
            if planet in (p1, p2):
                score += RAJA_BONUS
                # viparīta subtype check
                try:
                    if jd_raja.vipareetha_raja_yoga(p2h, p1, p2):
                        score += VIPAREETA_BONUS
                except Exception:
                    pass
                break
    except Exception:
        pass

    # Dhana‑yoga heuristic: occupant or lord of 2nd / 11th
    try:
        if p2h.get(planet) in (1, 10):  # houses are 0‑based
            score += DHANA_BONUS
        elif any(str(planet) in str(h2p[i]).split('/') for i in (1, 10)):
            score += DHANA_BONUS
    except Exception:
        pass

    # Kemadruma doṣa penalises Moon
    if planet == const._MOON and asc_house is not None:
        try:
            if jd_yoga.kemadruma_yoga(h2p, p2h, asc_house):
                score += KEMADRUMA_PENALTY
        except Exception:
            pass
    return score

# ════════════════════════════════════════════════════════════════

def _rate_periods(vim: pd.DataFrame, nar: pd.DataFrame,
                  sb_strengths: List[float], sav: Dict[int, int], natal_pp,
                  vargas: Dict[str, List[str]]) -> pd.DataFrame:

    wealth_lords = {const._JUPITER, const._VENUS}
    career_lords = {const._SUN, const._MERCURY, const._SATURN, const._MARS}

    # pre‑compute dignity levels of all planets in all relevant charts
    d1_levels = {
        p: _dignity_level(p, natal_pp[p + 1][1][0]) for p in range(const._SATURN + 1)
    }

    # helpers for yoga scoring
    _h2p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
    _p2h = jutils.get_planet_house_dictionary_from_planet_positions(natal_pp)
    _asc_house = next((i for i, cell in enumerate(_h2p) if 'L' in str(cell).split('/')), None)

        p: _dignity_level(p, natal_pp[p + 1][1][0]) for p in range(const._SATURN + 1)


    def _varga_level(chart_key: str, planet: int) -> int | None:
        sign = _planet_sign_in_chart(vargas[chart_key], planet)
        return _dignity_level(planet, sign) if sign is not None else None

    def _divisional_bonus(planet: int) -> int:
        base = d1_levels.get(planet, 0)
        adjustments: List[int] = []
        if planet in wealth_lords:
            for key in ("D2", "D11"):
                lvl = _varga_level(key, planet)
                if lvl is not None:
                    diff = lvl - base
                    if diff >= 0:
                        adjustments.append(DIV_EQUAL_BONUS)
                    elif diff == -1:
                        adjustments.append(DIV_MINOR_BONUS)
                    else:
                        adjustments.append(DIV_PENALTY)
        elif planet in career_lords:
            lvl = _varga_level("D10", planet)
            if lvl is not None:
                diff = lvl - base
                if diff >= 0:
                    adjustments.append(DIV_EQUAL_BONUS)
                elif diff == -1:
                    adjustments.append(DIV_MINOR_BONUS)
                else:
                    adjustments.append(DIV_PENALTY)
        return sum(adjustments)

    def _score(row) -> Dict[str, object]:
        start, end, lord = row.start, row.end, row.lord
        mid = start + (end - start) / 2
        score = 0

        # lordship
        if lord in wealth_lords:
            score += WEALTH_LORD_WT
        if lord in career_lords:
            score += CAREER_LORD_WT

        # Sarva‑aṣṭakavarga support
        if lord in wealth_lords and all(sav.get(h, 0) >= SAV_WEALTH_TH for h in (2, 11)):
            score += SAV_BONUS_WT
        if lord in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            score += SAV_BONUS_WT

        # Śad‑bala strength
        sb_ratio = sb_strengths[lord] if 0 <= lord < len(sb_strengths) else 1.0
        if sb_ratio >= SHADBALA_GOOD:
            score += STRENGTH_BONUS
        elif sb_ratio < SHADBALA_BAD:
            score += STRENGTH_MALUS

        # NEW: Divisional‑chart confirmation
        score += _divisional_bonus(lord)
            score += _yoga_bonus(lord, _h2p, _p2h, _asc_house)

        # label & transit veto
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "EXCELLENT" and not _transit_key_hit(mid, natal_pp):
            label = "GOOD" if score >= 40 else "NEUTRAL"

        return dict(period=f"{start.date()} → {end.date()}", rating=label)

    combined = pd.concat([vim, nar], ignore_index=True)
    return pd.DataFrame([_score(r) for r in combined.itertuples(index=False)])

# ═══════════════════════════════════════════════════════════════════════════
# public helper (Flask & CLI)
# ═══════════════════════════════════════════════════════════════════════════

def timeline_from_args(*, name: str, date: str, time: str, lat, lon,
                        tz: str | float = "+00:00") -> pd.DataFrame:
    lat, lon = float(lat), float(lon)
    dob = datetime.fromisoformat(f"{date}T{time}")
    offset = _tz_to_offset_hours(tz, dob)
    place = _build_place(name, lat, lon, offset)

    jd_birth = jutils.julian_day_number((dob.year, dob.month, dob.day),
                                        (dob.hour, dob.minute, dob.second))

    natal_pp = _planet_positions(jd_birth, place)
    sav = _sav_scores(jutils.get_house_planet_list_from_planet_positions(natal_pp))
    sb_strengths = jd_strength.shad_bala(jd_birth, place)[8]

    # Prepare divisional charts once (sign lists)
    vargas = {
        "D2": jutils.get_house_planet_list_from_planet_positions(
            jd_charts.divisional_chart(jd_birth, place, divisional_chart_factor=2)
        ),
        "D10": jutils.get_house_planet_list_from_planet_positions(
            jd_charts.divisional_chart(jd_birth, place, divisional_chart_factor=10)
        ),
        "D11": jutils.get_house_planet_list_from_planet_positions(
            jd_charts.divisional_chart(jd_birth, place, divisional_chart_factor=11)
        ),
    }

    vim_df, nar_df = _dashas(dob, place)

    return _rate_periods(vim_df, nar_df, sb_strengths, sav, natal_pp, vargas)

# ═══════════════════════════════════════════════════════════════════════════
# simple CLI for ad‑hoc testing (unchanged surface)
# ═══════════════════════════════════════════════════════════════════════════

def _cli() -> None:
    ap = argparse.ArgumentParser(description="Generate wealth/career timeline")
    ap.add_argument("--name", required=True)
    ap.add_argument("--date", required=True, help="YYYY‑MM‑DD")
    ap.add_argument("--time", required=True, help="HH:MM (24‑h)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--tz", default="+00:00", help="TZ offset hours or IANA zone")
    args = ap.parse_args()

    df = timeline_from_args(name=args.name, date=args.date, time=args.time,
                             lat=args.lat, lon=args.lon, tz=args.tz)
    out = f"timeline_{args.name.replace(' ', '_')}.csv"
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    _cli()
