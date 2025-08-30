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
# Expanded yoga bonuses (logic J)
WEALTH_YOGA_BONUS = 8      # generic dhan/wealth‑centred yogas
VASMATI_BONUS = 8          # Vasumatī
VEENA_BONUS = 8            # Veena yoga
LAKSHMI_BONUS = 10         # Lakshmi yoga
GAJA_KESARI_BONUS = 10        # Gaja‑Kesari
CHANDRA_MANGALA_BONUS = 10 # Chandra‑Mangala
SUNAPHA_ANAPHA_BONUS = 6
UBHAYACHARI_BONUS = 6
ADHI_BONUS = 8
PM_PURUSHA_BONUS = 12      # each of the five Mahā‑purusha yogas
AMALA_BONUS = 6
PARIVARTANA_DHANA_BONUS = 8
PARIVARTANA_RAJA_BONUS = 12
# ── New *negative* yoga / doṣa penalties (wealth‑career hindrances) ──
DARIDRA_PENALTY = -15
ARTHA_NIDRA_PENALTY = -12
CHORA_PENALTY = -10
KUBERA_BHANGA_PENALTY = -12
ALABDHA_BHAGYA_PENALTY = -10
ROGA_PENALTY = -10
KANTAKA_SATURN_PENALTY = -7  # sits in _transit_bonus too
KEMADRUMA_PENALTY = -12  # (already defined)
GRAHANA_DOSHA_PENALTY = -10
PAAP_KARTARI_PENALTY = -10
PANCHA_PAAPA_PENALTY = -12
DVIR_ROGA_PENALTY = -8
CHATRU_ASHRA_PENALTY = -8
KALA_SARPA_PENALTY = -20
SHRAPIT_PENALTY = -12
GURU_CHANDALA_PENALTY = -15
RAHU_KETU_2_8_PENALTY = -8
# Dosha penalties / bonuses
COMBUST_PENALTY = -6
WAR_WIN_BONUS = 3
WAR_LOSE_PENALTY = -7
RETRO_BENEFIC_BONUS = 4
RETRO_MALEFIC_PENALTY = -4
DEBILITATION_PENALTY = -8

# global re‑normalisation factor (logic I)
RAW_SCALE = 0.8  # rescales final score so outputs still ~ −30 … +90

BENEFICS_NATURAL = {const._JUPITER, const._VENUS, const._MOON, const._MERCURY}

# ── Functional benefic/ malefic lookup upgrade (logic H) ───────────────
# Helper categorisation keys
FUNC_YOGA = "Y"   # yoga‑kāraka (owns kendra & trine)
FUNC_BENE = "+"   # functional benefic
FUNC_MALE = "-"   # functional malefic
FUNC_NEUT = "0"   # mixed/neutral


LABELS: tuple[tuple[str, int], ...] = (
    ("EXTREMELY GOOD", 80),
    ("VERY VERY GOOD", 60),
    ("VERY GOOD", 40),
    ("GOOD", 20),
    ("SLIGHTLY GOOD", 0),
    ("SLIGHTLY BAD", -20),
    ("BAD", -40),
    ("VERY BAD", -60),
    ("VERY VERY BAD", -80),
    ("EXTREMELY BAD", -999),
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
# Functional benefic helper (BUILD‑TIME logic)
# ═══════════════════════════════════════════════════════════════════════════

def _functional_role(planet: int, asc_sign: int) -> str:
    """Return role key for *planet* given ascendant sign (0‑based)."""
    # derive houses ruled by planet
    houses = [((s - asc_sign) % 12) + 1 for s, lord in enumerate(_SIGN_LORD) if lord == planet]
    is_trine   = any(h in (1, 5, 9) for h in houses)
    is_kendra  = any(h in (1, 4, 7, 10) for h in houses)
    is_dusht   = any(h in (6, 8, 12) for h in houses)
    if is_trine and is_kendra:
        return FUNC_YOGA
    if is_trine and not is_dusht:
        return FUNC_BENE
    if is_dusht and not is_trine and not is_kendra:
        return FUNC_MALE
    return FUNC_NEUT

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

# ───────────────────────────────────────────────────────────────────────────
# Dosha flags helper
# ───────────────────────────────────────────────────────────────────────────

def _dosha_maps(natal_pp):
    """Return (combust_set, retro_set, war_dict) where war_dict maps planet→±pts."""
    combust, retro, war_dict = set(), set(), {}
    # combustion
    for func in (getattr(jd_charts, "planets_in_combustion", None),
                 getattr(jd_strength, "planets_in_combustion", None)):
        if func:
            try:
                combust = set(func(natal_pp))
                break
            except Exception:
                pass
    # retrograde
    for func in (getattr(jd_charts, "planets_in_retrograde", None),
                 getattr(pdrik, "planets_in_retrograde", None)):
        if func:
            try:
                retro = set(func(natal_pp))
                break
            except Exception:
                pass
    # graha‑yuddha
    for func in (getattr(pdrik, "planets_in_graha_yudh", None),
                 getattr(jd_charts, "planets_in_graha_yudh", None)):
        if func:
            try:
                pairs = func(natal_pp)
                for item in pairs or []:
                    if isinstance(item, (tuple, list)) and len(item) == 2:
                        winner, loser = item
                        war_dict[winner] = WAR_WIN_BONUS
                        war_dict[loser] = WAR_LOSE_PENALTY
                break
            except Exception:
                pass
    return combust, retro, war_dict

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
    """Return cumulative bonus/penalty for yogas & doṣas involving *planet*.

    The function now covers 25+ classical wealth/career yogas via PyJHora’s
    `hora.horoscope.chart.yoga` helpers. We *only* add the bonus when the
    yoga is present **and** the given *planet* participates (so the same
    yoga can boost multiple planets if applicable)."""
    score = 0
    # Existing Raja / Viparīta / Dhana / Kemadruma logic ——————————————
    try:
        jd_raja_yoga_pairs = jd_raja.get_raja_yoga_pairs(h2p)
        for p1, p2 in jd_raja_yoga_pairs:
            if planet in (p1, p2):
                score += RAJA_BONUS
                try:
                    if jd_raja.vipareetha_raja_yoga(p2h, p1, p2):
                        score += VIPAREETA_BONUS
                except Exception:
                    pass
                # Dharmakarma adhipati (9th+10th) speciality
                try:
                    if jd_raja.dharma_karmadhipati_yoga(p2h, p1, p2):
                        score += RAJA_BONUS  # same weight as Raja
                except Exception:
                    pass
    except Exception:
        pass

    # Dhana‑yoga heuristic (lord/occupant of 2 & 11)
    try:
        if p2h.get(planet) in (1, 10):
            score += DHANA_BONUS
        elif any(str(planet) in str(h2p[i]).split('/') for i in (1, 10)):
            score += DHANA_BONUS
    except Exception:
        pass

        # ——— Expanded wealth yogas via direct function lookup ————————
    _yoga_map = {
        # positive
        "chandra_mangala_yoga": (CHANDRA_MANGALA_BONUS, {const._MOON, const._MARS}),
        "sunaphaa_yoga":       (SUNAPHA_ANAPHA_BONUS, {const._MOON}),
        "anaphaa_yoga":        (SUNAPHA_ANAPHA_BONUS, {const._MOON}),
        "duradhara_yoga":      (SUNAPHA_ANAPHA_BONUS, {const._MOON}),
        "vasumati_yoga":       (VASMATI_BONUS, set(range(const._SATURN + 1))),
        "veenaa_yoga":         (VEENA_BONUS, BENEFICS_NATURAL),
        "lakshmi_yoga":        (LAKSHMI_BONUS, BENEFICS_NATURAL),
        "gaja_kesari_yoga":    (GAJA_KESARI_BONUS, {const._MOON, const._JUPITER}),
        "ubhaya_chara_yoga":   (UBHAYACHARI_BONUS, {const._SUN}),
        "adhi_yoga":           (ADHI_BONUS, BENEFICS_NATURAL),
        "amala_yoga":          (AMALA_BONUS, set(range(const._SATURN + 1))),
        # negative yogas / doṣas
        "daridra_yoga":        (DARIDRA_PENALTY, set(range(const._SATURN + 1))),
        "artha_nidra_yoga":    (ARTHA_NIDRA_PENALTY, set(range(const._SATURN + 1))),
        "chora_yoga":          (CHORA_PENALTY, set(range(const._SATURN + 1))),
        "kubera_bhanga_yoga":  (KUBERA_BHANGA_PENALTY, {const._JUPITER}),
        "alabdhabhāgya_yoga":  (ALABDHA_BHAGYA_PENALTY, set(range(const._SATURN + 1))),
        "roga_yoga":           (ROGA_PENALTY, set(range(const._SATURN + 1))),
        "grahana_dosha":       (GRAHANA_DOSHA_PENALTY, {const._SUN, const._MOON}),
        "paap_kartari_yoga":   (PAAP_KARTARI_PENALTY, set(range(const._SATURN + 1))),
        "panchaka_paapa_yoga": (PANCHA_PAAPA_PENALTY, set(range(const._SATURN + 1))),
        "dvi_roga_yoga":       (DVIR_ROGA_PENALTY, set(range(const._SATURN + 1))),
        "kala_sarpa_dosha":    (KALA_SARPA_PENALTY, set(range(const._SATURN + 1))),
        "shrapit_dosha":       (SHRAPIT_PENALTY, {const._SATURN, const._RAHU}),
        "guru_chandala_yoga":  (GURU_CHANDALA_PENALTY, {const._JUPITER, const._RAHU}),
    }
    for func_name, (wt, parts) in _yoga_map.items():
        if planet not in parts:
            continue
        func = getattr(jd_yoga, func_name, None)
        if not func:
            continue
        try:
            if func(h2p, p2h, asc_house):
                score += wt
        except Exception:
            pass

    # ——— Pancha‑Mahā‑Purusha special check (strength‑based) ———————
    _pmp_funcs = {
        "ruchaka_yoga": const._MARS,
        "bhadra_yoga":  const._MERCURY,
        "maalavya_yoga": const._VENUS,
        "hamsa_yoga":   const._JUPITER,
        "sasa_yoga":    const._SATURN,
    }
    for fname, p in _pmp_funcs.items():
        if planet != p:
            continue
        func = getattr(jd_yoga, fname, None)
        if func:
            try:
                if func(h2p, p2h, asc_house):
                    score += PM_PURUSHA_BONUS
            except Exception:
                pass

    # Kemadruma doṣa (unchanged)
    if planet == const._MOON and asc_house is not None:
        try:
            if jd_yoga.kemadruma_yoga(h2p, p2h, asc_house):
                score += KEMADRUMA_PENALTY
        except Exception:
            pass
    return score

# ════════════════════════════════════════════════════════════════

def _rate_periods(
    vim_df: pd.DataFrame,
    nar_df: pd.DataFrame,
    sb_strengths: List[float],
    sav: Dict[int, int],
    natal_pp,
    vargas: Dict[str, List[str]],
) -> pd.DataFrame:
    """Core scoring – now **adds Transit Overlay** (logic F)."""

    wealth_lords = {const._JUPITER, const._VENUS}
    career_lords = {const._SUN, const._MERCURY, const._SATURN, const._MARS}

    # —— natal basics ————————————————————————————————
    asc_sign   = natal_pp[0][1][0]
    moon_sign  = natal_pp[const._MOON + 1][1][0]
    tenth_sign = (asc_sign + 9) % 12
    tenth_lord = _SIGN_LORD[tenth_sign]

    d1_levels = {p: _dignity_level(p, natal_pp[p + 1][1][0])
                 for p in range(const._SATURN + 1)}

    _h2p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
    _p2h = jutils.get_planet_house_dictionary_from_planet_positions(natal_pp)
    _asc_house = next((i for i, cell in enumerate(_h2p) if "L" in str(cell).split("/")), None)

    _combust_set, _retro_set, _war_dict = _dosha_maps(natal_pp)

    # functional roles table for this lagna
    _func_role = {p: _functional_role(p, asc_sign) for p in range(const._SATURN + 1)}
    _func_benefics = {p for p, r in _func_role.items() if r in (FUNC_YOGA, FUNC_BENE)}


    # —— helper: dignities in vargas (kept from earlier) ————————————
    def _varga_level(chart_key: str, planet: int) -> int | None:
        sign = _planet_sign_in_chart(vargas[chart_key], planet)
        return _dignity_level(planet, sign) if sign is not None else None

    def _divisional_bonus(planet: int) -> int:
        base = d1_levels.get(planet, 0)
        adj: List[int] = []
        if planet in wealth_lords:
            for k in ("D2", "D11"):
                lvl = _varga_level(k, planet)
                if lvl is None:
                    continue
                diff = lvl - base
                adj.append(DIV_EQUAL_BONUS if diff >= 0 else
                           DIV_MINOR_BONUS if diff == -1 else DIV_PENALTY)
        elif planet in career_lords:
            lvl = _varga_level("D10", planet)
            if lvl is not None:
                diff = lvl - base
                adj.append(DIV_EQUAL_BONUS if diff >= 0 else
                           DIV_MINOR_BONUS if diff == -1 else DIV_PENALTY)
        return sum(adj)

    # —— helper: single‑planet base score (re‑used for node dispositors) ——
    def _planet_base(p: int) -> int:
        sc = 0
        if p in wealth_lords:
            sc += WEALTH_LORD_WT
        if p in career_lords:
            sc += CAREER_LORD_WT
        if p in wealth_lords and all(sav.get(h, 0) >= SAV_WEALTH_TH for h in (2, 11)):
            sc += SAV_BONUS_WT
        if p in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            sc += SAV_BONUS_WT
        ratio = sb_strengths[p] if 0 <= p < len(sb_strengths) else 1.0
        sc += STRENGTH_BONUS if ratio >= SHADBALA_GOOD else STRENGTH_MALUS if ratio < SHADBALA_BAD else 0
        sc += _divisional_bonus(p)
        sc += _yoga_bonus(p, _h2p, _p2h, _asc_house)
        if p in _combust_set:
            sc += COMBUST_PENALTY
        if p in _retro_set:
            sc += RETRO_BENEFIC_BONUS if p in _func_benefics else RETRO_MALEFIC_PENALTY
        sc += _war_dict.get(p, 0)
        if d1_levels.get(p, 0) == -2:
            sc += DEBILITATION_PENALTY
        return sc

    # —— helper: transit overlay ————————————————————————————————
    def _transit_bonus(mid_dt: datetime, antar_lord: int) -> tuple[int, int]:
        """Return (delta_score, positive_hit_count)."""
        jd_mid = jutils.julian_day_number((mid_dt.year, mid_dt.month, mid_dt.day),
                                          (mid_dt.hour, mid_dt.minute, mid_dt.second))
        tr_pp  = _planet_positions(jd_mid, _build_place("geo", 0.0, 0.0, 0.0))
        def tr_sign(pid: int):
            try:
                return _sign_of_longitude(tr_pp[pid + 1][1][1])
            except (IndexError, TypeError):
                return None

        jup_s  = tr_sign(const._JUPITER)
        sat_s  = tr_sign(const._SATURN)
        mars_s = tr_sign(const._MARS)
        rahu_s = tr_sign(const._RAHU) if hasattr(const, "_RAHU") else None
        ketu_s = (rahu_s + 6) % 12 if rahu_s is not None else None

        positives = 0
        delta = 0

        # Jupiter aspect 5/9 on Lagna or antar‑lord natal sign
        natal_al_sign = natal_pp[antar_lord + 1][1][0]
        if ((jup_s - asc_sign) % 12 in (4, 8)) or ((jup_s - natal_al_sign) % 12 in (4, 8)):
            delta += 5
            positives += 1

        # Saturn kantaka (4/8) or sade‑sati start/end
        if (sat_s - asc_sign) % 12 in (3, 7):
            delta -= 7
        if (sat_s - moon_sign) % 12 in (11, 1):
            delta -= 7

        # Mars aspect/conjunction on 10th‑lord
        diff_m = (mars_s - natal_pp[tenth_lord + 1][1][0]) % 12
        if diff_m in (0, 4, 8):
            if tenth_lord in BENEFICS_NATURAL:
                delta += 3
                positives += 1
            else:
                delta -= 3

        # Node conjunct 2nd/11th cusp
        for n_sign in (rahu_s, ketu_s):
            if n_sign is None:
                continue
            if n_sign in ((asc_sign + 1) % 12, (asc_sign + 10) % 12):
                dispos = _SIGN_LORD[n_sign]
                strong = d1_levels.get(dispos, 0) >= 1
                delta += 4 if strong else -4
                if strong:
                    positives += 1

        return delta, positives

    # —— main row scorer ————————————————————————————————
    def _score(row) -> Dict[str, object]:
        start, end, lord = row.start, row.end, row.lord
        mid = start + (end - start) / 2

        # ① base score (handles nodes internally)
        if lord in (const._RAHU, const._KETU):
            node_sign = natal_pp[lord + 1][1][0]
            dispositor = _SIGN_LORD[node_sign]
            score = _planet_base(dispositor)
            house_idx = _p2h.get(lord, None)
            if house_idx in (2, 5, 10):         # 3/6/11 from Lagna
                score += 6
            elif house_idx in (3, 4, 7, 11):    # 4/5/8/12
                score -= 6
            if house_idx == 9 and d1_levels.get(dispositor, 0) >= 2:
                score += 4
        else:
            score = _planet_base(lord)

                # ② transit overlay
        t_delta, t_pos = _transit_bonus(mid, lord)
        score += t_delta

        # re‑normalise (logic I)
        score = int(round(score * RAW_SCALE))

        # ③ final label with cap rule
        label = next(lbl for lbl, th in LABELS if score >= th)
        # downgrade rule: zero positive transits ⇒ at most VERY VERY GOOD
        if t_pos == 0 and label == "EXTREMELY GOOD":
            label = "VERY VERY GOOD"
        return {"period": f"{start.date()} → {end.date()}", "rating": label}

    combined = pd.concat([vim_df, nar_df], ignore_index=True)
    return pd.DataFrame([_score(r) for r in combined.itertuples(index=False)])        

    def planet_base(p: int) -> int:
        """Core score for a given planet using same rules (lordship→dosha)"""
        bs = 0
        if p in wealth_lords:
            bs += WEALTH_LORD_WT
        if p in career_lords:
            bs += CAREER_LORD_WT
        if p in wealth_lords and all(sav.get(h, 0) >= SAV_WEALTH_TH for h in (2, 11)):
            bs += SAV_BONUS_WT
        if p in career_lords and sav.get(10, 0) >= SAV_CAREER_TH:
            bs += SAV_BONUS_WT
        ratio = sb_strengths[p] if 0 <= p < len(sb_strengths) else 1.0
        if ratio >= SHADBALA_GOOD:
            bs += STRENGTH_BONUS
        elif ratio < SHADBALA_BAD:
            bs += STRENGTH_MALUS
        bs += _divisional_bonus(p)
        bs += _yoga_bonus(p, _h2p, _p2h, _asc_house)
        if p in _combust_set:
            bs += COMBUST_PENALTY
        if p in _retro_set:
            bs += (RETRO_BENEFIC_BONUS if p in BENEFICS_NATURAL else RETRO_MALEFIC_PENALTY)
        bs += _war_dict.get(p, 0)
        if d1_levels.get(p, 0) == -2:
            bs += DEBILITATION_PENALTY
        return bs

        # Special handling for nodes ------------------------------------------------
        if lord in (const._RAHU, const._KETU):
            node_sign = natal_pp[lord + 1][1][0]
            dispositor = _SIGN_LORD[node_sign]
            score = planet_base(dispositor)
            house_idx = _p2h.get(lord, None)
            if house_idx in (2, 5, 10):           # 3/6/11 from Lagna
                score += 6
            elif house_idx in (3, 4, 7, 11):      # 4/5/8/12
                score -= 6
            # foreign‑gain: node in 10th (house 9) + exalted/own dispositor
            if house_idx == 9 and d1_levels.get(dispositor, 0) >= 2:
                score += 4
        else:
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

            # Divisional & yoga
            score += _divisional_bonus(lord)
            score += _yoga_bonus(lord, _h2p, _p2h, _asc_house)

            # Dosha flags
            if lord in _combust_set:
                score += COMBUST_PENALTY
            if lord in _retro_set:
                score += RETRO_BENEFIC_BONUS if lord in _func_benefics else RETRO_MALEFIC_PENALTY
            score += _war_dict.get(lord, 0)
            if d1_levels.get(lord, 0) == -2:
                score += DEBILITATION_PENALTY

        # label & transit veto
        label = next(lbl for lbl, th in LABELS if score >= th)
        if label == "EXCELLENT" and not _transit_key_hit(mid, natal_pp):
            label = "GOOD" if score >= 40 else "NEUTRAL"

        return dict(period=f"{start.date()} → {end.date()}", rating=label)(lbl for lbl, th in LABELS if score >= th)
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

    # === Planetary Summary Output (Vedic Navagraha) ===
    try:
        h2p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
        p2h = jutils.get_planet_house_dictionary_from_planet_positions(natal_pp)
    except Exception:
        h2p, p2h = [], {}

    asc_sign = natal_pp[0][1][0] if natal_pp and natal_pp[0] else 0
    # retro set via existing helper (degrades gracefully)
    try:
        _comb, _retro_set, _war = _dosha_maps(natal_pp)
    except Exception:
        _retro_set = set()

    SIGN_NAMES = [
        "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
        "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
    ]
    PLANET_NAMES = {
        const._SUN: "Sun", const._MOON: "Moon", const._MARS: "Mars",
        const._MERCURY: "Mercury", const._JUPITER: "Jupiter", const._VENUS: "Venus",
        const._SATURN: "Saturn", getattr(const, "_RAHU", 7): "Rahu", getattr(const, "_KETU", 8): "Ketu",
    }
    NAKS = [
        "Ashwini","Bharani","Krittika","Rohini","Mrigashirsha","Ardra","Punarvasu","Pushya","Ashlesha",
        "Magha","Purva Phalguni","Uttara Phalguni","Hasta","Chitra","Swati","Vishakha","Anuradha","Jyeshtha",
        "Mula","Purva Ashadha","Uttara Ashadha","Shravana","Dhanishta","Shatabhisha","Purva Bhadrapada","Uttara Bhadrapada","Revati"
    ]
    SEG = 360.0 / 27.0
    QTR = SEG / 4.0

    rows = []
    navagraha = [const._SUN, const._MOON, const._MARS, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN]
    # include nodes if available in constants
    if hasattr(const, "_RAHU"):
        navagraha.append(const._RAHU)
    if hasattr(const, "_KETU"):
        navagraha.append(const._KETU)

    for pid in navagraha:
        # guard for presence in natal_pp structure
        idx = pid + 1
        if idx >= len(natal_pp) or not natal_pp[idx]:
            continue
        try:
            sign_idx = int(natal_pp[idx][1][0])
        except Exception:
            # derive from longitude if needed
            try:
                lon = float(natal_pp[idx][1][1]) % 360.0
                sign_idx = int(lon // 30)
            except Exception:
                sign_idx = 0
        sign_name = SIGN_NAMES[sign_idx]

        # longitude & in-sign degree
        try:
            lon = float(natal_pp[idx][1][1]) % 360.0
        except Exception:
            lon = float(sign_idx * 30.0)
        within = lon - (sign_idx * 30.0)
        d = int(within)
        m = int(round((within - d) * 60))
        if m == 60:
            d += 1; m = 0
        longitude_str = f"{d:02d}°{m:02d}′ {sign_name}"

        # nakshatra & pada
        nak_idx = int((lon // SEG) % 27)
        rem = (lon % SEG)
        pada = int(rem // QTR) + 1
        nak_name = NAKS[nak_idx]

        # house number (whole-sign by default)
        house_idx = p2h.get(pid)
        if house_idx is None:
            house_idx = (sign_idx - asc_sign) % 12
        house_no = house_idx + 1

        # lord of that house (rashi lord)
        house_sign = (asc_sign + house_idx) % 12
        try:
            house_lord_pid = _SIGN_LORD[house_sign]
            house_lord = PLANET_NAMES.get(house_lord_pid, str(house_lord_pid))
        except Exception:
            house_lord = "?"

        motion = "Retrograde" if pid in _retro_set or pid in (getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)) else "Direct"

        rows.append({
            "planet": PLANET_NAMES.get(pid, str(pid)),
            "sign": sign_name,
            "house": house_no,
            "house_lord": house_lord,
            "longitude": longitude_str,
            "nakshatra": nak_name,
            "pada": pada,
            "motion": motion,
        })

        df = pd.DataFrame(rows, columns=["planet","sign","house","house_lord","longitude","nakshatra","pada","motion"])
    # Build complete HTML fragment (table + heading); app.py will render as-is.
    table_html = df.to_html(index=False, classes="table table-striped table-sm")
    html_out = f"""
<div class=\"container\"> 
  <h2 class=\"h5 mb-3 text-center\">Navagraha Summary</h2>
  <style>
    /* center-align entire table and all headers/cells */
    .table { margin-left: auto; margin-right: auto; }
    .table th, .table td { text-align: center !important; vertical-align: middle; }
  </style>
  <div class=\"table-responsive\">
    {table_html}
  </div>
</div>
"""
    return html_out

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
