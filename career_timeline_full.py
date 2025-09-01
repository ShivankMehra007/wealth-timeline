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

BENEFICS_NATURAL = {const._JUPITER, const._VENUS, const._MOON, const._MERCURY}

# ── Functional benefic/ malefic lookup upgrade (logic H) ───────────────
# Helper categorisation keys
FUNC_YOGA = "Y"   # yoga‑kāraka (owns kendra & trine)
FUNC_BENE = "+"   # functional benefic
FUNC_MALE = "-"   # functional malefic
FUNC_NEUT = "0"   # mixed/neutral


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

    # === Planetary Summary Output (Vedic Navagraha) ===
    try:
        h2p = jutils.get_house_planet_list_from_planet_positions(natal_pp)
        p2h = jutils.get_planet_house_dictionary_from_planet_positions(natal_pp)
    except Exception:
        h2p, p2h = [], {}

    asc_sign = natal_pp[0][1][0] if natal_pp and natal_pp[0] else 0
    # retro set via existing helper (degrades gracefully)
    try:
        _combust_set, _retro_set, _war = _dosha_maps(natal_pp)
    except Exception:
        _combust_set, _retro_set, _war = set(), set(), {}

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

    # ── Reading based on Lagna-Lord house position ─────────────────────────
    def _planet_sign(pid: int) -> int:
        try:
            return int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                return int(lonx // 30)
            except Exception:
                return 0

    lagna_sign = asc_sign
    lagna_lord_pid = _SIGN_LORD[lagna_sign]
    lagna_lord_name = PLANET_NAMES.get(lagna_lord_pid, str(lagna_lord_pid))
    ll_house_idx = p2h.get(lagna_lord_pid)
    if ll_house_idx is None:
        ll_house_idx = (_planet_sign(lagna_lord_pid) - lagna_sign) % 12
    ll_house_no = ll_house_idx + 1

    # condition helpers
    afflicted = (lagna_lord_pid in _retro_set) or (lagna_lord_pid in _combust_set)
    is_benefic = lagna_lord_pid in BENEFICS_NATURAL
    is_malefic_natural = not is_benefic

    # 8th-lord nature (for house 8 note)
    h8_sign = (lagna_sign + 7) % 12
    h8_lord = _SIGN_LORD[h8_sign]
    h8_is_benefic = h8_lord in BENEFICS_NATURAL

    # benefic presence in 12th (association surrogate)
    has_benefic_in_12 = any(p2h.get(p) == 11 for p in BENEFICS_NATURAL)

    SIGN_TXT = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading_lines: list[str] = []
    header = f"Lagna lord {lagna_lord_name} is in the {SIGN_TXT[ll_house_idx]} house."

    if ll_house_no == 1:
        reading_lines += [
            "Sound health and longevity; courageous and valorous.",
            "Thoughtful yet fickle mind; relationships can be unstable.",
            "Tendency toward multiple partnerships or adultery.",
            "Owns and benefits from land/property.",
        ]
    elif ll_house_no == 2:
        reading_lines += [
            "Learned and prosperous; religious inclination and sobriety.",
            "Self‑respecting with multiple spouses/alliances possible.",
            "Many virtues; gains specifically through land, vehicles or livestock.",
        ]
    elif ll_house_no == 3:
        reading_lines += [
            "Very courageous—lion‑like—prosperous and wise.",
            "Self‑respecting; two marriages/alliances are possible.",
            "Strong support from siblings and relatives.",
        ]
    elif ll_house_no == 4:
        reading_lines += [
            "Comforts and property through mother/home; several brothers indicated.",
            "Sensual yet virtuous; good looks; long‑lived; devoted to both parents.",
            "Light appetite.",
        ]
    elif ll_house_no == 5:
        reading_lines += [
            "Quick to anger and proud; honoured by rulers/superiors.",
            "Ordinary comfort from children; risk that the first‑born may not survive.",
            "Long‑lived and inclined to virtuous deeds.",
        ]
    elif ll_house_no == 6:
        if afflicted:
            reading_lines += [
                "When afflicted: poor health and troubles from enemies/rivals.",
            ]
        else:
            reading_lines += [
                "Good health; destroys opponents; frugal and wealthy; gains from land/work.",
            ]

    elif ll_house_no == 7:
        reading_lines += [
            "Brilliant and attractive; spouse good‑looking and good‑natured.",
        ]
        if is_malefic_natural:
            reading_lines += [
                "Because the lagna‑lord is a natural malefic: risk of separation/bereft of spouse, detachment, poverty or kingship, and wandering in foreign lands.",
            ]
    elif ll_house_no == 8:
        reading_lines += [
            "Long‑lived and can accumulate wealth, yet prone to ill‑health.",
            "Adulterous tendencies; theft/gambling risks; quick temper; good for spiritual pursuits.",
        ]
        reading_lines += [
            ("Eye diseases/strain are likely." if not h8_is_benefic else "Good looks/appearance from benefic 8th‑lord influence."),
        ]
    elif ll_house_no == 9:
        reading_lines += [
            "Fortunate and learned; beloved of people; devotion to Viṣṇu or structured worship.",
            "Endowed with wife, sons and wealth; very famous.",
        ]
    elif ll_house_no == 10:
        reading_lines += [
            "Learned; honoured by rulers; full comforts from father; fame and wealth through own prowess.",
        ]
    elif ll_house_no == 11:
        reading_lines += [
            "Manifold gains and good qualities; multiple wives; famous; sons long‑lived; lives in comfort.",
        ]
    elif ll_house_no == 12:
        reading_lines += [
            "Bereft of bodily comforts; engaged in unworthy pursuits; foreign residence/work likely.",
        ]
        if not has_benefic_in_12:
            reading_lines += [
                "With no benefic support on the 12th: futile expenditure and easy anger.",
            ]
        else:
            reading_lines += [
                "Benefic association/aspect on the 12th reduces the affliction.",
            ]


    reading_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Lagna-lord</h3>"
        f"<p class='text-center mb-1'><em>{header}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_lines)
        + "</div>"
    )

    # --- Weakness checks (Avasthas & Shadbala) --------------------------------
    weak_note_html = ""

    # Recommended Shadbala thresholds (virupas)
    SHAD_THRESH = {
        const._SUN: 300,
        const._MOON: 360,
        const._MARS: 300,
        const._MERCURY: 420,
        const._JUPITER: 390,
        const._VENUS: 330,
        const._SATURN: 300,
    }

    def _iter_items(obj):
        if isinstance(obj, dict):
            return list(obj.items())
        if isinstance(obj, (list, tuple)):
            out = []
            for it in obj:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    out.append((it[0], it[1]))
            return out
        return []

    def _get_avastha_sets():
        sets = {"bala": set(), "mrita": set(), "sushupti": set()}
        # Baladi avasthas (Bala / Kumara / Yuva / Vriddha / Mrita)
        for mod in (jd_strength, jd_charts):
            for fname in ("baladi_avasthas", "get_baladi_avasthas", "baladi_avastha", "get_baladi_avastha"):
                func = getattr(mod, fname, None)
                if not func:
                    continue
                try:
                    res = func(natal_pp)
                    for k, v in _iter_items(res):
                        try:
                            pid = int(k)
                        except Exception:
                            try:
                                pid = int(str(k))
                            except Exception:
                                continue
                        txt = str(v).lower()
                        if "bala" in txt:
                            sets["bala"].add(pid)
                        if "mrita" in txt:
                            sets["mrita"].add(pid)
                    break
                except Exception:
                    continue
        # Jagradadi avasthas (Jagrat / Swapna / Sushupti)
        for mod in (jd_strength, jd_charts):
            for fname in ("jagradadi_avasthas", "jagratadi_avasthas", "get_jagradadi_avasthas", "get_jagratadi_avasthas"):
                func = getattr(mod, fname, None)
                if not func:
                    continue
                try:
                    res = func(natal_pp)
                    for k, v in _iter_items(res):
                        try:
                            pid = int(k)
                        except Exception:
                            try:
                                pid = int(str(k))
                            except Exception:
                                continue
                        txt = str(v).lower()
                        if "sushupt" in txt:
                            sets["sushupti"].add(pid)
                    break
                except Exception:
                    continue
        return sets

    avs = _get_avastha_sets()

    def _get_shadbala_result():
        for fname in ("get_shad_bala", "get_shadbala", "shad_bala", "shadbala", "compute_shad_bala"):
            func = getattr(jd_strength, fname, None)
            if not func:
                continue
            try:
                return func(natal_pp)
            except Exception:
                continue
        return None

    def _extract_shadbala_val(res, pid: int):
        if res is None:
            return None
        if isinstance(res, dict):
            val = res.get(pid, res.get(str(pid)))
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, (list, tuple)) and val:
                for item in val:
                    if isinstance(item, (int, float)):
                        return float(item)
                try:
                    return float(val[0])
                except Exception:
                    return None
            try:
                return float(val)
            except Exception:
                return None
        if isinstance(res, (list, tuple)):
            for it in res:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    k, v = it[0], it[1]
                    if k == pid or str(k) == str(pid):
                        if isinstance(v, (int, float)):
                            return float(v)
                        try:
                            return float(v)
                        except Exception:
                            return None
        return None

    sb_res = _get_shadbala_result()
    sb_val = _extract_shadbala_val(sb_res, lagna_lord_pid)
    sb_weak = False
    if lagna_lord_pid in SHAD_THRESH and sb_val is not None:
        sb_weak = sb_val < SHAD_THRESH[lagna_lord_pid]

    weak = (lagna_lord_pid in avs["bala"]) or (lagna_lord_pid in avs["mrita"]) or (lagna_lord_pid in avs["sushupti"]) or sb_weak
    if weak:
        weak_note_html = f"<p class='text-center mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {lagna_lord_name} is weak</p>"
    # Inject note directly into the Lagna-lord reading block
    # --- Mahadasha note for Lagna-lord --------------------------------------
    def _get_lon(pid: int) -> float:
        try:
            return float(natal_pp[pid + 1][1][1]) % 360.0
        except Exception:
            try:
                # derive from sign when longitude missing
                sidx = int(natal_pp[pid + 1][1][0])
                return float(sidx * 30.0)
            except Exception:
                return 0.0

    def _md_period_for(target_pid: int):
        """Return (start_dt, end_dt) of the Mahadasha of target_pid after birth (including birth if applicable)."""
        # Vimshottari order and year lengths
        KETU = getattr(const, "_KETU", -2)
        RAHU = getattr(const, "_RAHU", -1)
        order = [KETU, const._VENUS, const._SUN, const._MOON, const._MARS, RAHU, const._JUPITER, const._SATURN, const._MERCURY]
        years = {KETU:7, const._VENUS:20, const._SUN:6, const._MOON:10, const._MARS:7, RAHU:18, const._JUPITER:16, const._SATURN:19, const._MERCURY:17}
        # Moon nakshatra at birth -> starting lord & balance
        moon_lon = _get_lon(const._MOON)
        SEG = 360.0 / 27.0
        nak_idx = int(moon_lon // SEG) % 27
        start_lord = order[nak_idx % 9]
        frac = (moon_lon % SEG) / SEG  # elapsed fraction within nakshatra
        elapsed_yrs = frac * years[start_lord]
        remain_yrs = years[start_lord] - elapsed_yrs
        DAYS_PER_YEAR = 365.2425
        start_dt = dob - timedelta(days=elapsed_yrs * DAYS_PER_YEAR)
        end_dt = dob + timedelta(days=remain_yrs * DAYS_PER_YEAR)
        # build forward list up to ~120y from start
        seq = []
        seq.append((start_lord, start_dt, end_dt))
        idx = (order.index(start_lord) + 1) % 9
        cur_start = end_dt
        total_yrs = remain_yrs
        while total_yrs < 121:  # little buffer
            lord = order[idx]
            dur = years[lord]
            cur_end = cur_start + timedelta(days=dur * DAYS_PER_YEAR)
            seq.append((lord, cur_start, cur_end))
            cur_start = cur_end
            total_yrs += dur
            idx = (idx + 1) % 9
        # pick the MD for target_pid that ends after birth
        for lord, s, e in seq:
            if lord == target_pid and e > dob:
                return (s, e)
        return None

    md1 = _md_period_for(lagna_lord_pid)
    md1_note_html = ""
    if md1:
        _s, _e = md1
        md1_note_html = f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {lagna_lord_name}:</strong> {_s:%Y-%m-%d} – {_e:%Y-%m-%d}</p>"

    reading_html = reading_html.replace("</div>", f"{md1_note_html}{weak_note_html}</div>")

        # ── Reading based on 2nd‑house lord (wealth significator) ──────────────
    h2_sign = (lagna_sign + 1) % 12
    h2_lord_pid = _SIGN_LORD[h2_sign]
    h2_lord_name = PLANET_NAMES.get(h2_lord_pid, str(h2_lord_pid))
    h2l_house_idx = p2h.get(h2_lord_pid)
    if h2l_house_idx is None:
        h2l_house_idx = (_planet_sign(h2_lord_pid) - lagna_sign) % 12
    h2l_house_no = h2l_house_idx + 1

    # helpers for conditions
    def _planets_in_house(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    NAT_MALEFICS = {const._SUN, const._MARS, const._SATURN, getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)}
    is_h2_benefic = h2_lord_pid in BENEFICS_NATURAL
    is_h2_malefic_nat = h2_lord_pid in NAT_MALEFICS

    in_same_house = set(_planets_in_house(h2l_house_idx)) - {h2_lord_pid}
    has_malefic_assoc = any(p in NAT_MALEFICS for p in in_same_house)

    # exaltation check for 2nd‑lord
    try:
        h2_lord_sign_now = int(natal_pp[h2_lord_pid + 1][1][0])
    except Exception:
        try:
            _lon_tmp = float(natal_pp[h2_lord_pid + 1][1][1]) % 360.0
            h2_lord_sign_now = int(_lon_tmp // 30)
        except Exception:
            h2_lord_sign_now = (lagna_sign + h2l_house_idx) % 12
    is_exalted_h2 = (_EXALTS.get(h2_lord_pid) == h2_lord_sign_now)

    # association with Jupiter/Venus in the same house as 2nd‑lord
    has_guru_shukra_assoc = any(p in (const._JUPITER, const._VENUS) for p in in_same_house)

    SIGN_TXT2 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading2_lines: list[str] = []
    header2 = f"2nd‑house lord {h2_lord_name} is in the {SIGN_TXT2[h2l_house_idx]} house."

    if h2l_house_no == 1:
        reading2_lines += [
            "Wealthy and thrifty; harsh in temperament; enjoys many comforts.",
            "Endowed with sons; generous to others yet troublesome to own family.",
        ]
    elif h2l_house_no == 2:
        reading2_lines += [
            "Wealthy; earns well; enjoys comforts; proud.",
            "Two or three marriages/alliances likely; childlessness is possible; inclined to oppose others.",
        ]
    elif h2l_house_no == 3:
        reading2_lines += [
            "Virtuous, wise, valorous, but greedy and sensuous.",
        ]
        if is_h2_malefic_nat:
            reading2_lines.append("As a natural malefic 2nd‑lord in the 3rd: differences with co‑borns.")
        if is_h2_benefic:
            reading2_lines.append("As a natural benefic 2nd‑lord in the 3rd: opposed to the ruler.")
        if h2_lord_pid == const._MARS:
            reading2_lines.append("Mars as 2nd‑lord in the 3rd: thief‑like tendencies.")
        if has_malefic_assoc:
            reading2_lines.append("2nd‑lord joined malefics in the 3rd: speaks ill of the devas/virtuous.")
    elif h2l_house_no == 4:
        reading2_lines += [
            "Endowed with wealth; truthful; long‑lived; benefits from father.",
        ]
        if is_exalted_h2 or has_guru_shukra_assoc:
            reading2_lines.append("If exalted or joined by Jupiter/Venus: status akin to a king.")
        if h2_lord_pid == const._MARS:
            reading2_lines.append("Mars as 2nd‑lord in the 4th is a maraka (death‑inflicting).")
    elif h2l_house_no == 5:
        reading2_lines += [
            "Wealthy; famed for efficiency; blessed with several sons; capable of earning greatly; health is delicate.",
        ]
    elif h2l_house_no == 6:
        reading2_lines += [
            "Accumulates wealth; destroys enemies; profits through enemy matters/litigation.",
        ]
        if has_malefic_assoc:
            reading2_lines.append("With malefic association: loss of wealth; disease of anal region and breast.")
    elif h2l_house_no == 7:
        reading2_lines += [
            "Sensuous; spouse contributes to income (money‑earning wife).",
            "The native and spouse are prone to adultery.",
        ]
        if has_malefic_assoc:
            reading2_lines.append("Afflicted by malefics: becomes a physician.")
    elif h2l_house_no == 8:
        reading2_lines += [
            "Income from land/property; reduced comforts from wife; no support from elder brother; harmful to others.",
            "Lives on alms/charity; suicidal tendencies may arise.",
        ]
    elif h2l_house_no == 9:
        reading2_lines += [
            "Wealthy and industrious; childhood ill‑health; becomes healthy and comfortable later; good orator.",
        ]
    elif h2l_house_no == 10:
        reading2_lines += [
            "Sensuous, self‑respecting, learned; many relationships; lacks comfort from progeny; gains through the ruler/state.",
        ]
    elif h2l_house_no == 11:
        reading2_lines += [
            "Well‑known, efficient, respectable; continuously benefitting; wealthy; supports many people’s needs.",
        ]
    elif h2l_house_no == 12:
        reading2_lines += [
            "Courageous and laborious; deprived of comfort from the eldest child; likely to lose wealth.",
        ]
        if is_h2_benefic:
            reading2_lines.append("As a natural benefic 2nd‑lord in the 12th: renowned trader.")

    reading2_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 2nd‑house lord</h3>"
        f"<p class='text-center mb-1'><em>{header2}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading2_lines)
        + "</div>"
    )

    # Weakness checks for 2nd‑house lord (Avasthas & Shadbala)
    weak2_note_html = ""
    sb2_val = _extract_shadbala_val(sb_res, h2_lord_pid)
    sb2_weak = False
    if h2_lord_pid in SHAD_THRESH and sb2_val is not None:
        sb2_weak = sb2_val < SHAD_THRESH[h2_lord_pid]
    weak2 = (h2_lord_pid in avs["bala"]) or (h2_lord_pid in avs["mrita"]) or (h2_lord_pid in avs["sushupti"]) or sb2_weak
    if weak2:
        weak2_note_html = f"<p class='text-center mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {h2_lord_name} is weak</p>"
    # Inject note directly into the 2nd-lord reading block
    md2 = _md_period_for(h2_lord_pid)
    md2_note_html = ""
    if md2:
        _s2, _e2 = md2
        md2_note_html = f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {h2_lord_name}:</strong> {_s2:%Y-%m-%d} – {_e2:%Y-%m-%d}</p>"

    reading2_html = reading2_html.replace("</div>", f"{md2_note_html}{weak2_note_html}</div>")

    # ── Reading based on 3rd‑house lord (siblings/effort) ──────────────
    h3_sign = (lagna_sign + 2) % 12
    h3_lord_pid = _SIGN_LORD[h3_sign]
    h3_lord_name = PLANET_NAMES.get(h3_lord_pid, str(h3_lord_pid))
    h3l_house_idx = p2h.get(h3_lord_pid)
    if h3l_house_idx is None:
        h3l_house_idx = (_planet_sign(h3_lord_pid) - lagna_sign) % 12
    h3l_house_no = h3l_house_idx + 1

    def _malefic_aspects_house(target_idx: int) -> bool:
        """Return True if any natural malefic casts a classical aspect on target house.
        Uses 7th aspect for all; plus special aspects:
        Mars → 4th & 8th; Jupiter → 5th & 9th; Saturn → 3rd & 10th; Rahu/Ketu → 5th & 9th.
        """
        KETU = getattr(const, "_KETU", -2)
        RAHU = getattr(const, "_RAHU", -1)
        special = {
            const._SUN: {6},              # 7th
            const._MARS: {3, 6, 7},       # 4th, 7th, 8th (0-based offsets)
            const._JUPITER: {4, 6, 8},    # 5th, 7th, 9th
            const._SATURN: {2, 6, 9},     # 3rd, 7th, 10th
            RAHU: {4, 6, 8},              # treat like Jupiter
            KETU: {4, 6, 8},
        }
        for p in NAT_MALEFICS:
            p_house = p2h.get(p)
            if p_house is None:
                continue
            delta = (target_idx - p_house) % 12
            if delta in special.get(p, {6}):
                return True
        return False

    def _planets_in_house(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    in_same_house3 = set(_planets_in_house(h3l_house_idx)) - {h3_lord_pid}
    has_malefic_assoc3 = any(p in {const._SUN, const._MARS, const._SATURN, getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)} for p in in_same_house3)
    malefic_touches_3lord = has_malefic_assoc3 or _malefic_aspects_house(h3l_house_idx)

    SIGN_TXT3 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading3_lines: list[str] = []
    header3 = f"3rd‑house lord {h3_lord_name} is in the {SIGN_TXT3[h3l_house_idx]} house."

    if h3l_house_no == 1:
        reading3_lines += [
            "Courageous self‑starter; wealthy and valorous.",
            "Street‑smart but not formally educated; leans toward adultery; risk of forgery/cheating.",
        ]
    elif h3l_house_no == 2:
        reading3_lines += [
            "Covets others’ spouse and wealth; lacks valour; obese/indulgent.",
            "Reluctant to initiate ventures; deprived of comforts; short‑lived indications; opposed to own people.",
        ]
    elif h3l_house_no == 3:
        reading3_lines += [
            "Healthy and valorous; enjoys support from siblings.",
            "Blessed with sons, wealth and comforts; family and friends are helpful; devout toward teachers and deities.",
        ]
    elif h3l_house_no == 4:
        reading3_lines += [
            "Enjoys comforts; wealthy and wise; strained bond with mother; spouse tends to be harsh/cruel.",
        ]
    elif h3l_house_no == 5:
        reading3_lines += [
            "Virtuous; blessed with sons; long‑lived; constantly helps others.",
        ]
        if malefic_touches_3lord:
            reading3_lines.append("With malefic conjunction/aspect on the 3rd‑lord: spouse is cruel.")
    elif h3l_house_no == 6:
        reading3_lines += [
            "Enmity with brothers; very wealthy; little comfort from maternal uncle; desire toward maternal aunt; eye troubles; sickly.",
        ]
    elif h3l_house_no == 7:
        reading3_lines += [
            "Troubled childhood but later comfortable; follower of authority; spouse is good‑natured.",
        ]
    elif h3l_house_no == 8:
        reading3_lines += [
            "Thieving tendencies; servile; danger of severe punishment from rulers; adverse outcomes for siblings.",
        ]
    elif h3l_house_no == 9:
        reading3_lines += [
            "Gains fortune through women; little comfort from father; aided by children; learned.",
        ]
    elif h3l_house_no == 10:
        reading3_lines += [
            "Earns wealth through own efforts; many comforts; responsible for (or attached to) a wicked woman; honoured by rulers.",
        ]
    elif h3l_house_no == 11:
        reading3_lines += [
            "Foolish, weak, sickly and servile, yet courageous; earns through own efforts; indulges in physical pleasures.",
        ]
    elif h3l_house_no == 12:
        reading3_lines += [
            "Spends on immoral pursuits; harsh father; gains through women; opposes relatives and friends; foreign travel/residence indicated.",
        ]

    reading3_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 3rd‑house lord</h3>"
        f"<p class='text-center mb-1'><em>{header3}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading3_lines)
        + "</div>"
    )

    # Mahadasha note for 3rd‑house lord
    md3 = _md_period_for(h3_lord_pid)
    md3_note_html = ""
    if md3:
        _s3, _e3 = md3
        md3_note_html = f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {h3_lord_name}:</strong> {_s3:%Y-%m-%d} – {_e3:%Y-%m-%d}</p>"

    # Weakness note for 3rd‑house lord (Avasthas & Shadbala)
    weak3_note_html = ""
    sb3_val = _extract_shadbala_val(sb_res, h3_lord_pid)
    sb3_weak = False
    if h3_lord_pid in SHAD_THRESH and sb3_val is not None:
        sb3_weak = sb3_val < SHAD_THRESH[h3_lord_pid]
    weak3 = (h3_lord_pid in avs["bala"]) or (h3_lord_pid in avs["mrita"]) or (h3_lord_pid in avs["sushupti"]) or sb3_weak
    if weak3:
        weak3_note_html = f"<p class='text-center mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {h3_lord_name} is weak</p>"

    reading3_html = reading3_html.replace("</div>", f"{md3_note_html}{weak3_note_html}</div>")
    
        # ── Reading based on 4th-house lord (home/mother/real-estate/comforts) ──
    h4_sign = (lagna_sign + 3) % 12
    h4_lord_pid = _SIGN_LORD[h4_sign]
    h4_lord_name = PLANET_NAMES.get(h4_lord_pid, str(h4_lord_pid))
    h4l_house_idx = p2h.get(h4_lord_pid)
    if h4l_house_idx is None:
        h4l_house_idx = (_planet_sign(h4_lord_pid) - lagna_sign) % 12
    h4l_house_no = h4l_house_idx + 1

    SIGN_TXT4 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading4_lines: list[str] = []
    header4 = f"4th-house lord {h4_lord_name} is in the {SIGN_TXT4[h4l_house_idx]} house."

    # Natural benefic/malefic check for the 4th lord (used in House-6 condition)
    is_h4_benefic_nat = h4_lord_pid in BENEFICS_NATURAL
    NAT_MALEFICS = {const._SUN, const._MARS, const._SATURN,
                    getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)}
    is_h4_malefic_nat = h4_lord_pid in NAT_MALEFICS

    if h4l_house_no == 1:
        reading4_lines += [
            "Strong support from mother; comfort with home and vehicles.",
            "Educated; gains in land/real-estate and conveniences; generally virtuous.",
        ]
    elif h4l_house_no == 2:
        reading4_lines += [
            "Owns property; courageous and proud; large family setup.",
            "Magnetic charm; indulgent in physical pleasures.",
        ]
    elif h4l_house_no == 3:
        reading4_lines += [
            "Generous, talented and courageous; charitable; supported by servants.",
            "Wealth comes through personal effort; can be a source of trouble to parents.",
        ]
    elif h4l_house_no == 4:
        reading4_lines += [
            "Vast property and steady comforts; clever and composed.",
            "Advisory/ministerial capacity; well-informed and proud; attached to spouse.",
            "Raises father’s status/wealth; inclined to religious pursuits.",
        ]
    elif h4l_house_no == 5:
        reading4_lines += [
            "Enjoys physical comforts; widely liked; devoted to God.",
            "Earnings through own initiative; longevity indicated; benefits from father.",
        ]
    elif h4l_house_no == 6:
        reading4_lines += [
            "Deprivation of maternal comforts; short-tempered; morally wayward; brooding; adulterous tendencies.",
        ]
        # Conditional clause from the source:
        if is_h4_malefic_nat:
            reading4_lines.append("As a natural malefic 4th-lord in the 6th: brings bad name to the father.")
        if is_h4_benefic_nat:
            reading4_lines.append("As a natural benefic 4th-lord in the 6th: accumulates wealth.")
    elif h4l_house_no == 7:
        reading4_lines += [
            "Versed in many subjects; relinquishes father’s/ancestral property.",
            "Finds it hard to express confidently in assemblies.",
        ]
    elif h4l_house_no == 8:
        reading4_lines += [
            "Lacks home comforts; risks impotence; little help from parents.",
            "Cruel, sickly and morally compromised; base origins indicated.",
        ]
    elif h4l_house_no == 9:
        reading4_lines += [
            "Beloved and well-provided; proud and virtuous.",
            "Little help from father and often away from him; learned; Vishnu-oriented worship.",
        ]
    elif h4l_house_no == 10:
        reading4_lines += [
            "Honoured by authorities; robust health; many comforts; self-controlled.",
            "Technical/chemical know-how; father may have two marriages.",
        ]
    elif h4l_house_no == 11:
        reading4_lines += [
            "Generous, helpful and capable; charitable yet prone to ailments.",
            "Devoted to father; performs virtuous works.",
        ]
    elif h4l_house_no == 12:
        reading4_lines += [
            "Homelessness or fragile home base; foolish and indolent; wayward conduct.",
            "Father resides abroad or away from native.",
        ]

    reading4_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 4th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header4}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading4_lines)
        + "</div>"
    )

    # Mahadasha note for 4th-house lord
    md4 = _md_period_for(h4_lord_pid)
    md4_note_html = ""
    if md4:
        _s4, _e4 = md4
        md4_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h4_lord_name}:</strong> "
            f"{_s4:%Y-%m-%d} – {_e4:%Y-%m-%d}</p>"
        )

    # Weakness note for 4th-house lord (Avasthas & Śaḍbala)
    weak4_note_html = ""
    sb4_val = _extract_shadbala_val(sb_res, h4_lord_pid)
    sb4_weak = False
    if sb4_val is not None and h4_lord_pid in SHAD_THRESH:
        sb4_weak = sb4_val < SHAD_THRESH[h4_lord_pid]
    weak4 = (h4_lord_pid in avs["bala"]) or (h4_lord_pid in avs["mrita"]) or (h4_lord_pid in avs["sushupti"]) or sb4_weak
    if weak4:
        weak4_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h4_lord_name} is weak</p>"
        )

    # Attach the MD line and the weakness note directly inside this block
    reading4_html = reading4_html.replace("</div>", f"{md4_note_html}{weak4_note_html}</div>")
    
        # ── Reading based on 5th-house lord (children/intellect/creativity) ─────
    h5_sign = (lagna_sign + 4) % 12
    h5_lord_pid = _SIGN_LORD[h5_sign]
    h5_lord_name = PLANET_NAMES.get(h5_lord_pid, str(h5_lord_pid))
    h5l_house_idx = p2h.get(h5_lord_pid)
    if h5l_house_idx is None:
        h5l_house_idx = (_planet_sign(h5_lord_pid) - lagna_sign) % 12
    h5l_house_no = h5l_house_idx + 1

    # helpers for influence checks
    def _benefic_aspects_house(target_idx: int) -> bool:
        BENEFIC_SET = {const._JUPITER, const._VENUS, const._MERCURY, const._MOON}
        special = {
            const._JUPITER: {4, 6, 8},  # 5th, 7th, 9th (0-based deltas)
            const._VENUS:   {6},        # 7th
            const._MERCURY: {6},        # 7th
            const._MOON:    {6},        # 7th
        }
        for p in BENEFIC_SET:
            p_house = p2h.get(p)
            if p_house is None:
                continue
            delta = (target_idx - p_house) % 12
            if delta in special.get(p, {6}):
                return True
        return False

    def _planets_in_house(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    NAT_MALEFICS = {const._SUN, const._MARS, const._SATURN,
                    getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)}

    in_same_house5 = set(_planets_in_house(h5l_house_idx)) - {h5_lord_pid}
    has_benefic_assoc5 = any(p in {const._JUPITER, const._VENUS, const._MERCURY, const._MOON} for p in in_same_house5)
    has_malefic_assoc5 = any(p in NAT_MALEFICS for p in in_same_house5)

    # We already defined _malefic_aspects_house above (3rd-lord section) – reuse it:
    benefic_touches_5lord = has_benefic_assoc5 or _benefic_aspects_house(h5l_house_idx)
    malefic_touches_5lord = has_malefic_assoc5 or _malefic_aspects_house(h5l_house_idx)

    SIGN_TXT5 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading5_lines: list[str] = []
    header5 = f"5th-house lord {h5_lord_name} is in the {SIGN_TXT5[h5l_house_idx]} house."

    if h5l_house_no == 1:
        reading5_lines += [
            "Shrewd and learned; reputation rises.",
            "Deprived of comforts from progeny; tendency to squander others’ money.",
        ]
    elif h5l_house_no == 2:
        reading5_lines += [
            "Many sons and much wealth; widely known; favoured by women; talent in music/arts.",
        ]
    elif h5l_house_no == 3:
        reading5_lines += [
            "Well-regarded by siblings; persuasive and charming yet back-biting.",
            "Thrifty and self-focused; children tend to support siblings.",
        ]
    elif h5l_house_no == 4:
        reading5_lines += [
            "Comforts through mother/home; wealth and wisdom; advisory/preceptor roles.",
            "Follows ancestral vocation; devoted to mother.",
        ]
    elif h5l_house_no == 5:
        reading5_lines += [
            "Learning, pride and progeny; prominent and virtuous.",
        ]
        # Special condition from the source:
        if benefic_touches_5lord:
            reading5_lines.append("Under benefic influence: favourable for progeny.")
        if malefic_touches_5lord:
            reading5_lines.append("Under malefic influence: risk of childlessness or poor progeny comfort.")
    elif h5l_house_no == 6:
        reading5_lines += [
            "Illness to child; conflict with son; many enemies; poor health and status; financial strain.",
        ]
    elif h5l_house_no == 7:
        reading5_lines += [
            "Religiously inclined and dignified; helpful to others; blessed with sons; devout spouse and teachers.",
        ]
    elif h5l_house_no == 8:
        reading5_lines += [
            "Short-tempered and harsh; suffering and obstacles; adverse for progeny; respiratory ailments indicated.",
        ]
    elif h5l_house_no == 9:
        reading5_lines += [
            "High status for the child; family renown; literary/artistic gifts; handsome; honoured by authorities.",
        ]
    elif h5l_house_no == 10:
        reading5_lines += [
            "Fame akin to royalty; abundant pleasures; engages in virtuous/public works; supportive for mother.",
        ]
    elif h5l_house_no == 11:
        reading5_lines += [
            "Highly learned and wealthy; renowned; skilled author; many sons; steadfast friendships; brave; enjoys royal comforts.",
        ]
    elif h5l_house_no == 12:
        reading5_lines += [
            "Denied comfort from children or childless; foreign residence/travel likely.",
        ]

    reading5_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 5th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header5}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading5_lines)
        + "</div>"
    )

    # Mahadasha note for 5th-house lord
    md5 = _md_period_for(h5_lord_pid)
    md5_note_html = ""
    if md5:
        _s5, _e5 = md5
        md5_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h5_lord_name}:</strong> "
            f"{_s5:%Y-%m-%d} – {_e5:%Y-%m-%d}</p>"
        )

    # Weakness note for 5th-house lord (Avasthas & Śaḍbala)
    weak5_note_html = ""
    sb5_val = _extract_shadbala_val(sb_res, h5_lord_pid)
    sb5_weak = False
    if sb5_val is not None and h5_lord_pid in SHAD_THRESH:
        sb5_weak = sb5_val < SHAD_THRESH[h5_lord_pid]
    weak5 = (h5_lord_pid in avs["bala"]) or (h5_lord_pid in avs["mrita"]) or (h5_lord_pid in avs["sushupti"]) or sb5_weak
    if weak5:
        weak5_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h5_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    reading5_html = reading5_html.replace("</div>", f"{md5_note_html}{weak5_note_html}</div>")
    
        # ── Reading based on 6th-house lord (enemies/health/service) ─────────────
    h6_sign = (lagna_sign + 5) % 12
    h6_lord_pid = _SIGN_LORD[h6_sign]
    h6_lord_name = PLANET_NAMES.get(h6_lord_pid, str(h6_lord_pid))
    h6l_house_idx = p2h.get(h6_lord_pid)
    if h6l_house_idx is None:
        h6l_house_idx = (_planet_sign(h6_lord_pid) - lagna_sign) % 12
    h6l_house_no = h6l_house_idx + 1

    SIGN_TXT6 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    # helpers for conjunction / aspects
    def _planets_in_house(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    NAT_MALEFICS = {
        const._SUN, const._MARS, const._SATURN,
        getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)
    }
    BENEFIC_SET = {const._JUPITER, const._VENUS, const._MERCURY, const._MOON}

    in_same_house6 = set(_planets_in_house(h6l_house_idx)) - {h6_lord_pid}
    has_benefic_assoc6 = any(p in BENEFIC_SET for p in in_same_house6)
    has_malefic_assoc6 = any(p in NAT_MALEFICS for p in in_same_house6)

    # Use the previously defined aspect helpers (already in file)
    benefic_touches_6lord = has_benefic_assoc6 or _benefic_aspects_house(h6l_house_idx)
    malefic_touches_6lord = has_malefic_assoc6 or _malefic_aspects_house(h6l_house_idx)

    reading6_lines: list[str] = []
    header6 = f"6th-house lord {h6_lord_name} is in the {SIGN_TXT6[h6l_house_idx]} house."

    if h6l_house_no == 1:
        reading6_lines += [
            "Ill health or recurring ailments; proud yet widely known.",
            "Wealth through own effort; virtuous and courageous; opposed by relatives/siblings; overcomes enemies; dependable.",
        ]
        if benefic_touches_6lord:
            reading6_lines.append("Under benefic influence: health stabilises / improves.")
    elif h6l_house_no == 2:
        reading6_lines += [
            "Renowned in family; courageous; good oratory; foreign residence likely; dutiful yet ailing; earns and accumulates.",
        ]
    elif h6l_house_no == 3:
        reading6_lines += [
            "Hostility with brothers; quick-tempered; weak self-effort; suffers defeats; troublesome servants.",
        ]
    elif h6l_house_no == 4:
        reading6_lines += [
            "Little comfort from mother; brooding and hostile nature; fickle but manages to be rich.",
            "Mutual friction with father; father’s health suffers.",
        ]
    elif h6l_house_no == 5:
        reading6_lines += [
            "Friends and wealth are inconstant; strained relationship with children; selfish yet kind; suffers due to progeny.",
        ]
    elif h6l_house_no == 6:
        reading6_lines += [
            "Hostile to own community; friendly to outsiders; modest wealth; generally good health.",
        ]
    elif h6l_house_no == 7:
        reading6_lines += [
            "Deprived of marital pleasures; wealthy and virtuous; courageous.",
            "Spouse is hostile and short-tempered; potential issues with fertility.",
        ]
    elif h6l_house_no == 8:
        reading6_lines += [
            "Prone to illness; antagonistic to the virtuous; covets others’ wealth and partners; unclean habits.",
        ]
        # Classical cause-of-death indications by the 6L nature (only when 6L is in 8H)
        COD = {
            const._SATURN: "abdominal ailments",
            const._MARS: "snake bite",
            const._MERCURY: "poisoning / septicaemia",
            const._MOON: "hypothermia or water-borne disease",
            const._SUN: "attack by a lion or carnivorous beast",
            const._JUPITER: "deranged wisdom / mental illness",
            const._VENUS: "eye disease",
        }
        cod_txt = COD.get(h6_lord_pid)
        if cod_txt:
            reading6_lines.append(f"Classical death-cause indication: {cod_txt}.")
    elif h6l_house_no == 9:
        reading6_lines += [
            "Deals in wood or related trades; fluctuating income; irreverent to scriptures; opposed to brothers; lameness indicated.",
        ]
    elif h6l_house_no == 10:
        reading6_lines += [
            "Famous in the family; eloquent; detached from father; opposed to mother; enjoys comfort abroad.",
        ]
    elif h6l_house_no == 11:
        reading6_lines += [
            "Courageous, proud and virtuous; gains via opponents; risk of death through enemies; thefts; benefits through quadrupeds.",
        ]
    elif h6l_house_no == 12:
        reading6_lines += [
            "Hostile to learned people; squanders on vile pursuits; harms living beings; loses money via quadrupeds; a wandering fatalist.",
        ]

    reading6_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 6th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header6}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading6_lines)
        + "</div>"
    )

    # Mahadasha note for 6th-house lord
    md6 = _md_period_for(h6_lord_pid)
    md6_note_html = ""
    if md6:
        _s6, _e6 = md6
        md6_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h6_lord_name}:</strong> "
            f"{_s6:%Y-%m-%d} – {_e6:%Y-%m-%d}</p>"
        )

    # Weakness note for 6th-house lord (Avasthas & Śaḍbala)
    weak6_note_html = ""
    sb6_val = _extract_shadbala_val(sb_res, h6_lord_pid)
    sb6_weak = False
    if sb6_val is not None and h6_lord_pid in SHAD_THRESH:
        sb6_weak = sb6_val < SHAD_THRESH[h6_lord_pid]
    weak6 = (h6_lord_pid in avs["bala"]) or (h6_lord_pid in avs["mrita"]) or (h6_lord_pid in avs["sushupti"]) or sb6_weak
    if weak6:
        weak6_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h6_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    reading6_html = reading6_html.replace("</div>", f"{md6_note_html}{weak6_note_html}</div>")
    
        # ── Reading based on 7th-house lord (marriage/partnerships) ─────────────
    h7_sign = (lagna_sign + 6) % 12
    h7_lord_pid = _SIGN_LORD[h7_sign]
    h7_lord_name = PLANET_NAMES.get(h7_lord_pid, str(h7_lord_pid))
    h7l_house_idx = p2h.get(h7_lord_pid)
    if h7l_house_idx is None:
        h7l_house_idx = (_planet_sign(h7_lord_pid) - lagna_sign) % 12
    h7l_house_no = h7l_house_idx + 1

    SIGN_TXT7 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading7_lines: list[str] = []
    header7 = f"7th-house lord {h7_lord_name} is in the {SIGN_TXT7[h7l_house_idx]} house."

    if h7l_house_no == 1:
        reading7_lines += [
            "Adulterous tendencies; sharp but unscrupulous; attractive and pleasure-seeking.",
            "Strong attachment to spouse; prone to Vāta-related ailments.",
        ]
    elif h7l_house_no == 2:
        reading7_lines += [
            "Associates with multiple women; paradoxically abstains despite opportunity.",
            "Income via spouse/partners; sluggish to act.",
        ]
    elif h7l_house_no == 3:
        reading7_lines += [
            "Spiritual fortitude; affectionate nature; risk of miscarriage for the wife.",
        ]
    elif h7l_house_no == 4:
        reading7_lines += [
            "Truthful and religious; spouse may resort to adultery; dental issues indicated.",
            "Ties with the father’s adversaries.",
        ]
    elif h7l_house_no == 5:
        reading7_lines += [
            "Wealthy, proud and virtuous; contented.",
            "Wife is taken care of by the son.",
        ]
    elif h7l_house_no == 6:
        reading7_lines += [
            "Spouse has health issues; mutual hostility; quick temper and misery.",
            "Suffers at spouse’s hands.",
        ]
    elif h7l_house_no == 7:
        reading7_lines += [
            "Good spouse; learned; socially well-known; Vāta-related diseases indicated.",
        ]
    elif h7l_house_no == 8:
        reading7_lines += [
            "Spouse is ailing or morally compromised; separation or loss of spouse possible.",
            "Adulterous behavior and misery.",
        ]
    elif h7l_house_no == 9:
        reading7_lines += [
            "Constant inclination toward women; fame; agreeable nature.",
        ]
    elif h7l_house_no == 10:
        reading7_lines += [
            "Religiously inclined; gains wealth and progeny.",
            "Disobedient spouse; sensuous indulgence.",
        ]
    elif h7l_house_no == 11:
        reading7_lines += [
            "Earnings via spouse; more daughters indicated.",
            "Spouse is beautiful and virtuous.",
        ]
    elif h7l_house_no == 12:
        reading7_lines += [
            "Poverty; trade in garments; expenses through spouse; deceived by spouse.",
        ]

    reading7_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 7th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header7}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading7_lines)
        + "</div>"
    )

    # Mahadasha note for 7th-house lord
    md7 = _md_period_for(h7_lord_pid)
    md7_note_html = ""
    if md7:
        _s7, _e7 = md7
        md7_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h7_lord_name}:</strong> "
            f"{_s7:%Y-%m-%d} – {_e7:%Y-%m-%d}</p>"
        )

    # Weakness note for 7th-house lord (Avasthas & Śaḍbala)
    weak7_note_html = ""
    sb7_val = _extract_shadbala_val(sb_res, h7_lord_pid)
    sb7_weak = False
    if sb7_val is not None and h7_lord_pid in SHAD_THRESH:
        sb7_weak = sb7_val < SHAD_THRESH[h7_lord_pid]
    weak7 = (h7_lord_pid in avs["bala"]) or (h7_lord_pid in avs["mrita"]) or (h7_lord_pid in avs["sushupti"]) or sb7_weak
    if weak7:
        weak7_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h7_lord_name} is weak</p>"
        )

    # Attach the MD line and the weakness note directly inside this block
    reading7_html = reading7_html.replace("</div>", f"{md7_note_html}{weak7_note_html}</div>")
    
        # ── Reading based on 8th-house lord (longevity/obstructions/hidden) ─────
    h8_sign = (lagna_sign + 7) % 12
    h8_lord_pid = _SIGN_LORD[h8_sign]
    h8_lord_name = PLANET_NAMES.get(h8_lord_pid, str(h8_lord_pid))
    h8l_house_idx = p2h.get(h8_lord_pid)
    if h8l_house_idx is None:
        h8l_house_idx = (_planet_sign(h8_lord_pid) - lagna_sign) % 12
    h8l_house_no = h8l_house_idx + 1

    SIGN_TXT8 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    # helpers for conjunction/association checks
    def _planets_in_house(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    NAT_MALEFICS = {
        const._SUN, const._MARS, const._SATURN,
        getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)
    }
    BENEFIC_SET = {const._JUPITER, const._VENUS, const._MERCURY, const._MOON}

    in_same_house8 = set(_planets_in_house(h8l_house_idx)) - {h8_lord_pid}
    has_benefic_assoc8 = any(p in BENEFIC_SET for p in in_same_house8)
    has_malefic_assoc8 = any(p in NAT_MALEFICS for p in in_same_house8)

    # Weakness flag for 8th-lord (used both for note and for the 8H condition)
    sb8_val = _extract_shadbala_val(sb_res, h8_lord_pid)
    sb8_weak = False
    if sb8_val is not None and h8_lord_pid in SHAD_THRESH:
        sb8_weak = sb8_val < SHAD_THRESH[h8_lord_pid]
    weak8 = (h8_lord_pid in avs["bala"]) or (h8_lord_pid in avs["mrita"]) or (h8_lord_pid in avs["sushupti"]) or sb8_weak

    reading8_lines: list[str] = []
    header8 = f"8th-house lord {h8_lord_name} is in the {SIGN_TXT8[h8l_house_idx]} house."

    if h8l_house_no == 1:
        reading8_lines += [
            "Reduced physical comforts; irreverence toward the sacred/tradition; accident-prone; engages in forbidden or risky acts.",
        ]
    elif h8l_house_no == 2:
        reading8_lines += [
            "Weak initiative; limited wealth; loss of savings; short-life indications; thievish streak; many enemies; risk of punishment by authorities.",
        ]
    elif h8l_house_no == 3:
        reading8_lines += [
            "Lethargic and weak; poor comfort from siblings; quarrels with friends/brothers; fickle effort.",
        ]
    elif h8l_house_no == 4:
        reading8_lines += [
            "Deceives associates; deprived of help from mother/home/property; friction with father.",
        ]
    elif h8l_house_no == 5:
        reading8_lines += [
            "Limited progeny; wealth and longevity possible; dull or poor judgment; troubles after birth of a child.",
        ]
    elif h8l_house_no == 6:
        reading8_lines += [
            "Childhood ailments; ultimately overcomes enemies; anxieties/fears related to water and reptiles.",
        ]
        # Classical sub-conditions: nature of 8L when placed in 6H
        COD_8L_IN_6H = {
            const._SUN: "opposed to the ruler/state",
            const._MOON: "prone to lingering ailments",
            const._MARS: "quick-tempered and rash",
            const._MERCURY: "cowardly tendencies",
            const._JUPITER: "diseased/afflicted limbs",
            const._VENUS: "eye disease",
            const._SATURN: "diseases of the mouth/oral cavity",
        }
        _spec = COD_8L_IN_6H.get(h8_lord_pid)
        if _spec:
            reading8_lines.append(f"In this placement, classical texts add: {_spec}.")
    elif h8l_house_no == 7:
        reading8_lines += [
            "Two marriages/alliances likely; abdominal disease; immoral conduct indicated.",
        ]
        if has_malefic_assoc8:
            reading8_lines.append("With malefic association: losses in business and suffering caused by spouse.")
    elif h8l_house_no == 8:
        reading8_lines += [
            "Longevity and basic vitality protected; crafty/deceitful; fame through hidden or complex matters.",
        ]
        if weak8:
            reading8_lines.append("However, with a weak 8th-lord: lifespan trends toward medium rather than long.")
    elif h8l_house_no == 9:
        reading8_lines += [
            "Atheistic or irreverent; covets others’ spouse and wealth; cruel acts; spouse’s conduct is problematic; oral-cavity ailments possible.",
        ]
    elif h8l_house_no == 10:
        reading8_lines += [
            "Poor support from father; disinclined to sustained effort; serves under superiors without autonomy.",
        ]
    elif h8l_house_no == 11:
        reading8_lines += [
            "Difficult early years; prosperity improves in later life.",
        ]
        if has_malefic_assoc8:
            reading8_lines.append("With malefic association here: poverty/constraints persist.")
        if has_benefic_assoc8:
            reading8_lines.append("With benefic association here: longevity is enhanced and gains stabilise.")
    elif h8l_house_no == 12:
        reading8_lines += [
            "Spends on immoral pursuits; cruel or harsh behavior; chronic ailments; thievish tendencies.",
        ]

    reading8_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 8th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header8}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading8_lines)
        + "</div>"
    )

    # Mahadasha note for 8th-house lord
    md8 = _md_period_for(h8_lord_pid)
    md8_note_html = ""
    if md8:
        _s8, _e8 = md8
        md8_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h8_lord_name}:</strong> "
            f"{_s8:%Y-%m-%d} – {_e8:%Y-%m-%d}</p>"
        )

    # Weakness note for 8th-house lord (Avasthas & Śaḍbala)
    weak8_note_html = ""
    if weak8:
        weak8_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h8_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note inside this block
    reading8_html = reading8_html.replace("</div>", f"{md8_note_html}{weak8_note_html}</div>")
    
        # ── Reading based on 9th-house lord (fortune/dharma) ─────────────────────
    h9_sign = (lagna_sign + 8) % 12
    h9_lord_pid = _SIGN_LORD[h9_sign]
    h9_lord_name = PLANET_NAMES.get(h9_lord_pid, str(h9_lord_pid))
    h9l_house_idx = p2h.get(h9_lord_pid)
    if h9l_house_idx is None:
        h9l_house_idx = (_planet_sign(h9_lord_pid) - lagna_sign) % 12
    h9l_house_no = h9l_house_idx + 1

    SIGN_TXT9 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading9_lines: list[str] = []
    header9 = f"9th-house lord {h9_lord_name} is in the {SIGN_TXT9[h9l_house_idx]} house."

    if h9l_house_no == 1:
        reading9_lines += [
            "Learned, attractive, honoured by authorities; very fortunate.",
            "Small appetite; devoted to teachers and deities.",
        ]
    elif h9l_house_no == 2:
        reading9_lines += [
            "Sensuous; endowed with spouse and children; wealthy and learned; well-liked.",
            "Prone to ailments of the mouth/oral cavity.",
        ]
    elif h9l_house_no == 3:
        reading9_lines += [
            "Very good-looking; wealthy and virtuous.",
            "Support from siblings/relatives; spouse of pleasing appearance.",
        ]
    elif h9l_house_no == 4:
        reading9_lines += [
            "Devoted to mother; famous; owns house, land and vehicles.",
        ]
    elif h9l_house_no == 5:
        reading9_lines += [
            "Devoted to preceptors; religiously inclined and learned.",
            "Fortunate children; generally virtuous conduct.",
        ]
    elif h9l_house_no == 6:
        reading9_lines += [
            "Harassed by enemies; little comfort from maternal uncle.",
            "Despite adversity, remains engaged in religious pursuits; health is delicate.",
        ]
    elif h9l_house_no == 7:
        reading9_lines += [
            "Truthful, beautiful and devoted spouse; overall virtuousness indicated.",
        ]
    elif h9l_house_no == 8:
        reading9_lines += [
            "Unfortunate streak; little comfort from elder brother.",
            "Harms living beings; irreligious or transgressive tendencies.",
        ]
    elif h9l_house_no == 9:
        reading9_lines += [
            "Highly fortunate; attractive and virtuous; supported by brothers.",
            "Strong religious inclination.",
        ]
    elif h9l_house_no == 10:
        reading9_lines += [
            "Virtuous and renowned; elevated status with authorities.",
            "Actively religious; devoted to parents.",
        ]
    elif h9l_house_no == 11:
        reading9_lines += [
            "Pious and upright; steady inflow of money; long-lived.",
            "Religiously active; wealthy and famous.",
        ]
    elif h9l_house_no == 12:
        reading9_lines += [
            "Misfortune; spends wealth on religious deeds/charities.",
            "Honoured in foreign lands; scholarly and good-looking.",
        ]

    reading9_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 9th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header9}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading9_lines)
        + "</div>"
    )

    # Mahadasha note for 9th-house lord
    md9 = _md_period_for(h9_lord_pid)
    md9_note_html = ""
    if md9:
        _s9, _e9 = md9
        md9_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h9_lord_name}:</strong> "
            f"{_s9:%Y-%m-%d} – {_e9:%Y-%m-%d}</p>"
        )

    # Weakness note for 9th-house lord (Avasthas & Śaḍbala)
    weak9_note_html = ""
    sb9_val = _extract_shadbala_val(sb_res, h9_lord_pid)
    sb9_weak = False
    if sb9_val is not None and h9_lord_pid in SHAD_THRESH:
        sb9_weak = sb9_val < SHAD_THRESH[h9_lord_pid]
    weak9 = (h9_lord_pid in avs["bala"]) or (h9_lord_pid in avs["mrita"]) or (h9_lord_pid in avs["sushupti"]) or sb9_weak
    if weak9:
        weak9_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h9_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    reading9_html = reading9_html.replace("</div>", f"{md9_note_html}{weak9_note_html}</div>")
    
        # ── Reading based on 10th-house lord (career/status/karma) ─────────────
    h10_sign = (lagna_sign + 9) % 12
    h10_lord_pid = _SIGN_LORD[h10_sign]
    h10_lord_name = PLANET_NAMES.get(h10_lord_pid, str(h10_lord_pid))
    h10l_house_idx = p2h.get(h10_lord_pid)
    if h10l_house_idx is None:
        h10l_house_idx = (_planet_sign(h10_lord_pid) - lagna_sign) % 12
    h10l_house_no = h10l_house_idx + 1

    SIGN_TXT10 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading10_lines: list[str] = []
    header10 = f"10th-house lord {h10_lord_name} is in the {SIGN_TXT10[h10l_house_idx]} house."

    if h10l_house_no == 1:
        reading10_lines += [
            "Learned and virtuous; sickly in childhood but healthier later.",
            "Wealth rises progressively; devoted to father; friction with mother.",
        ]
    elif h10l_house_no == 2:
        reading10_lines += [
            "Virtuous and wealthy; honoured by authorities; charitable.",
            "Opposed to mother; acquisitive/avaricious streak.",
        ]
    elif h10l_house_no == 3:
        reading10_lines += [
            "Valorous and righteous; eloquent speaker; supported by siblings and staff.",
            "May oppose close relations when principles are at stake.",
        ]
    elif h10l_house_no == 4:
        reading10_lines += [
            "Prosperous and virtuous; lands/vehicles/comforts indicated.",
            "Devoted to both parents.",
        ]
    elif h10l_house_no == 5:
        reading10_lines += [
            "Wealth, children and learning supported; healthy and engaged in pious works.",
            "Favoured by the ruler; taste for music and the arts.",
        ]
    elif h10l_house_no == 6:
        reading10_lines += [
            "Harassed by rivals; skilful but under-rewarded; little comfort from father.",
            "Quarrelsome temperament; health generally serviceable.",
        ]
    elif h10l_house_no == 7:
        reading10_lines += [
            "Good spouse; virtuous and thoughtful; acts in accordance with dharma.",
        ]
    elif h10l_house_no == 8:
        reading10_lines += [
            "Long-lived but critical of others; reluctant to initiate ventures; harsh or unethical leanings.",
        ]
    elif h10l_house_no == 9:
        reading10_lines += [
            "Wealth and worthy progeny; royal favour or status equal to a ruler; noble friends.",
        ]
    elif h10l_house_no == 10:
        reading10_lines += [
            "Truthful and highly capable; enjoys comforts; excellent reputation.",
            "Kindness toward mother; professional stature is strong.",
        ]
    elif h10l_house_no == 11:
        reading10_lines += [
            "Riches, sons and virtues accrue; truthful and content; longevity indicated.",
            "Well cared for by the mother.",
        ]
    elif h10l_house_no == 12:
        reading10_lines += [
            "Clever yet anxious; intimidated by opponents; expenses through the state/authority.",
        ]
        # Special condition from the source: if 10L is a natural malefic in 12H → foreign work/wandering
        NAT_MALEFICS_10 = {
            const._SUN, const._MARS, const._SATURN,
            getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)
        }
        if h10_lord_pid in NAT_MALEFICS_10:
            reading10_lines.append("As a natural malefic 10th-lord in the 12th: wanders or works in a foreign land.")

    reading10_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 10th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header10}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading10_lines)
        + "</div>"
    )

    # Mahadasha note for 10th-house lord
    md10 = _md_period_for(h10_lord_pid)
    md10_note_html = ""
    if md10:
        _s10, _e10 = md10
        md10_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h10_lord_name}:</strong> "
            f"{_s10:%Y-%m-%d} – {_e10:%Y-%m-%d}</p>"
        )

    # Weakness note for 10th-house lord (Avasthas & Śaḍbala)
    weak10_note_html = ""
    sb10_val = _extract_shadbala_val(sb_res, h10_lord_pid)
    sb10_weak = False
    if sb10_val is not None and h10_lord_pid in SHAD_THRESH:
        sb10_weak = sb10_val < SHAD_THRESH[h10_lord_pid]
    weak10 = (h10_lord_pid in avs["bala"]) or (h10_lord_pid in avs["mrita"]) or (h10_lord_pid in avs["sushupti"]) or sb10_weak
    if weak10:
        weak10_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h10_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    reading10_html = reading10_html.replace("</div>", f"{md10_note_html}{weak10_note_html}</div>")
    
        # ── Reading based on 11th-house lord (income/gains/networks) ────────────
    h11_sign = (lagna_sign + 10) % 12
    h11_lord_pid = _SIGN_LORD[h11_sign]
    h11_lord_name = PLANET_NAMES.get(h11_lord_pid, str(h11_lord_pid))
    h11l_house_idx = p2h.get(h11_lord_pid)
    if h11l_house_idx is None:
        h11l_house_idx = (_planet_sign(h11_lord_pid) - lagna_sign) % 12
    h11l_house_no = h11l_house_idx + 1

    SIGN_TXT11 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    # natural malefic set (local to avoid cross-block deps)
    NAT_MALEFICS_11 = {
        const._SUN, const._MARS, const._SATURN,
        getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)
    }
    is_h11_malefic_nat = h11_lord_pid in NAT_MALEFICS_11

    reading11_lines: list[str] = []
    header11 = f"11th-house lord {h11_lord_name} is in the {SIGN_TXT11[h11l_house_idx]} house."

    if h11l_house_no == 1:
        reading11_lines += [
            "Wealthy with a sattvic bent; poetic/expressive; treats people fairly.",
            "Steady inflow of money; strong and brave; (some texts add) risk of shorter lifespan.",
        ]
    elif h11l_house_no == 2:
        reading11_lines += [
            "Very wealthy; enjoys comforts and spiritual leanings.",
            "Religious/charitable; prone to sickness and shorter life indications.",
        ]
    elif h11l_house_no == 3:
        reading11_lines += [
            "Highly efficient; many siblings/allies; overcomes enemies.",
            "Susceptible to abdominal complaints.",
        ]
    elif h11l_house_no == 4:
        reading11_lines += [
            "Wealth via mother; houses/lands; pilgrimages indicated.",
            "Long-lived; devoted to father; acts appropriately at the right time.",
        ]
    elif h11l_house_no == 5:
        reading11_lines += [
            "Learned; engaged in religious pursuits; lives comfortably.",
            "Virtuous children; harmonious relations with father.",
        ]
    elif h11l_house_no == 6:
        reading11_lines += [
            "Sickly; harsh; troubled by foes; residence or ties in foreign lands; powerful enemies.",
        ]
        if is_h11_malefic_nat:
            reading11_lines.append("With a natural malefic as 11th-lord in the 6th: classical indication of death in a foreign land at the hands of a thief.")
    elif h11l_house_no == 7:
        reading11_lines += [
            "Virtuous yet sensual; generous; often yields to spouse’s lead.",
            "Gains through women; long-lived; elevated status.",
        ]
    elif h11l_house_no == 8:
        reading11_lines += [
            "Professional/social failures; long-lived yet sickly; spouse may predecease.",
        ]
    elif h11l_house_no == 9:
        reading11_lines += [
            "Favoured by rulers; wealthy and truthful; very learned; devoted to religion.",
        ]
    elif h11l_house_no == 10:
        reading11_lines += [
            "Honoured by authority; self-controlled, truthful and virtuous.",
            "Follows own dharma; long-lived; devoted to mother; strained relation with father.",
        ]
    elif h11l_house_no == 11:
        reading11_lines += [
            "Gains from most undertakings; fame through learning and possessions.",
            "Long-lived; many sons and grandsons; pleasant appearance.",
        ]
    elif h11l_house_no == 12:
        reading11_lines += [
            "Associates with foreigners/outsiders; sensual; comforts via multiple women.",
            "Spends on religious works yet engages in misdeeds; chronic ailments indicated.",
        ]

    reading11_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 11th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header11}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading11_lines)
        + "</div>"
    )

    # Mahadasha note (shown above weakness note)
    md11 = _md_period_for(h11_lord_pid)
    md11_note_html = ""
    if md11:
        _s11, _e11 = md11
        md11_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h11_lord_name}:</strong> "
            f"{_s11:%Y-%m-%d} – {_e11:%Y-%m-%d}</p>"
        )

    # Weakness note for 11th-house lord (Avasthas & Śaḍbala)
    weak11_note_html = ""
    sb11_val = _extract_shadbala_val(sb_res, h11_lord_pid)
    sb11_weak = False
    if sb11_val is not None and h11_lord_pid in SHAD_THRESH:
        sb11_weak = sb11_val < SHAD_THRESH[h11_lord_pid]
    weak11 = (h11_lord_pid in avs["bala"]) or (h11_lord_pid in avs["mrita"]) or (h11_lord_pid in avs["sushupti"]) or sb11_weak
    if weak11:
        weak11_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h11_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    reading11_html = reading11_html.replace("</div>", f"{md11_note_html}{weak11_note_html}</div>")
    
        # ── Reading based on 12th-house lord (loss/foreign/expenses/isolation) ───
    h12_sign = (lagna_sign + 11) % 12
    h12_lord_pid = _SIGN_LORD[h12_sign]
    h12_lord_name = PLANET_NAMES.get(h12_lord_pid, str(h12_lord_pid))
    h12l_house_idx = p2h.get(h12_lord_pid)
    if h12l_house_idx is None:
        h12l_house_idx = (_planet_sign(h12_lord_pid) - lagna_sign) % 12
    h12l_house_no = h12l_house_idx + 1

    SIGN_TXT12 = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading12_lines: list[str] = []
    header12 = f"12th-house lord {h12_lord_name} is in the {SIGN_TXT12[h12l_house_idx]} house."

    if h12l_house_no == 1:
        reading12_lines += [
            "Spend-thrift tendencies; weak physique; poverty risks; not sharp-minded.",
            "Foreign residence likely; pleasant appearance; unmarried/impotency indications.",
            "Prone to Kapha-related illnesses."
        ]
    elif h12l_house_no == 2:
        reading12_lines += [
            "Religiously inclined; sweet-tongued; spends on good deeds.",
            "Generally comfortable, yet fears from thieves, fire and authority."
        ]
    elif h12l_house_no == 3:
        reading12_lines += [
            "Estranged from brothers or lives away from them; left to fend for oneself.",
            "Hostile stance toward others; thrifty/parsimonious bent."
        ]
    elif h12l_house_no == 4:
        reading12_lines += [
            "Devoid of lands, home/vehicles or maternal comforts; sickly.",
            "Opposition from own sons; general misery at home."
        ]
    elif h12l_house_no == 5:
        reading12_lines += [
            "Spends for the sake of children; deprived of children and learning.",
            "Pilgrimage or spiritual travel indicated."
        ]
    elif h12l_house_no == 6:
        reading12_lines += [
            "Short-tempered; miserable; sinful tendencies; hostile to own people.",
            "Addiction to other men’s/women’s company; eye disease indicated."
        ]
        # Special classical clause: Venus as 12L in 6H → blindness
        if h12_lord_pid == const._VENUS:
            reading12_lines.append("Classical warning: Venus as 12th-lord in the 6th — blindness risk.")
    elif h12l_house_no == 7:
        reading12_lines += [
            "Expenditure through spouse; deprived of marital comforts.",
            "Weakness or dullness; wicked conduct; suffering due to one’s own spouse."
        ]
    elif h12l_house_no == 8:
        reading12_lines += [
            "Pleasant speech; medium life span; some good qualities; capacity to acquire wealth."
        ]
    elif h12l_house_no == 9:
        reading12_lines += [
            "Self-serving; friction with friends and preceptors; pilgrimage/spiritual travel indicated."
        ]
    elif h12l_house_no == 10:
        reading12_lines += [
            "Little comfort from father; loss of money via the state/ruler.",
            "Avoids other’s spouses; accumulates wealth ultimately for the children."
        ]
    elif h12l_house_no == 11:
        reading12_lines += [
            "Rich and reputed; yet suffers losses even amidst wealth-yogas.",
            "Long-lived and famous; truthful disposition."
        ]
    elif h12l_house_no == 12:
        reading12_lines += [
            "Spend-thrift; quick to anger; sickly and short-lived tendencies.",
            "Cares for cattle/livestock; becomes well-known."
        ]

    reading12_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 12th-house lord</h3>"
        f"<p class='text-center mb-1'><em>{header12}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading12_lines)
        + "</div>"
    )

    # Mahadasha note for 12th-house lord
    md12 = _md_period_for(h12_lord_pid)
    md12_note_html = ""
    if md12:
        _s12, _e12 = md12
        md12_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h12_lord_name}:</strong> "
            f"{_s12:%Y-%m-%d} – {_e12:%Y-%m-%d}</p>"
        )

    # Weakness note for 12th-house lord (Avasthas & Śaḍbala)
    weak12_note_html = ""
    sb12_val = _extract_shadbala_val(sb_res, h12_lord_pid)
    sb12_weak = False
    if sb12_val is not None and h12_lord_pid in SHAD_THRESH:
        sb12_weak = sb12_val < SHAD_THRESH[h12_lord_pid]
    weak12 = (h12_lord_pid in avs["bala"]) or (h12_lord_pid in avs["mrita"]) or (h12_lord_pid in avs["sushupti"]) or sb12_weak
    if weak12:
        weak12_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h12_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    reading12_html = reading12_html.replace("</div>", f"{md12_note_html}{weak12_note_html}</div>")
    
        # ── Reading based on the Sun (graha-specific) ───────────────────────────
    sun_pid = const._SUN
    sun_name = PLANET_NAMES.get(sun_pid, "Sun")

    # Sun's house from Lagna (prefer p2h; fall back to sign-diff)
    sun_house_idx = p2h.get(sun_pid)
    if sun_house_idx is None:
        sun_house_idx = (_planet_sign(sun_pid) - lagna_sign) % 12
    sun_house_no = sun_house_idx + 1

    reading_sun_lines: list[str] = []
    header_sun = f"Sun is in the {SIGN_TXT[sun_house_idx]} house."

    # House-wise meanings
    if sun_house_no == 1:
        reading_sun_lines += [
            "Scant body hair; indolent; harsh/unyielding temper; easily angered; tall; proud; valiant; unforgiving; eye dryness or weak vision.",
        ]
        # Special Lagna conditions for Sun in 1H
        if lagna_sign == 0:   # Aries (exaltation)
            reading_sun_lines.append("In exalted Aries rising: poor vision is explicitly indicated.")
        if lagna_sign == 3:   # Cancer
            reading_sun_lines.append("In Cancer rising: cataract tendencies are classically noted.")
        if lagna_sign == 4:   # Leo (own sign)
            reading_sun_lines.append("In Leo rising: strong constitution but night-blindness is noted.")
        if lagna_sign == 6:   # Libra (debilitation)
            reading_sun_lines.append("In debilitated Libra rising: risk of blindness, poverty and poor progeny comfort.")
        if lagna_sign == 11:  # Pisces
            reading_sun_lines.append("In Pisces rising: served and attended by women is classically stated.")
    elif sun_house_no == 2:
        reading_sun_lines += [
            "Losses through authority/state; facial/teeth ailments; speech impediment; yet capacity for great wealth.",
        ]
    elif sun_house_no == 3:
        reading_sun_lines += [
            "Valorous, wealthy, liberal, strong; may lack comfort from siblings; learned; defeats opponents.",
        ]
    elif sun_house_no == 4:
        reading_sun_lines += [
            "Deprived of home comforts; weak ties to relatives; loss of land/house; prone to cardiac issues.",
        ]
    elif sun_house_no == 5:
        reading_sun_lines += [
            "Trouble with progeny (even childlessness); shortened longevity indications; poverty/worry; wise but a wanderer.",
            "Classically adverse for the first-born, especially a son.",
        ]
    elif sun_house_no == 6:
        reading_sun_lines += [
            "Opulent, powerful, very rich and famous; victorious; judicial/royal favour; strong digestion and appetite.",
        ]
    elif sun_house_no == 7:
        reading_sun_lines += [
            "Poverty; humiliation; unpleasant looks; ill-health; antagonism with women; transgressive behaviour.",
        ]
    elif sun_house_no == 8:
        reading_sun_lines += [
            "Loss of wealth/comforts; few children; shortened life; estrangement from kin; eye disease.",
        ]
    elif sun_house_no == 9:
        reading_sun_lines += [
            "Wealth, friends, sons and happiness; devotion to deities and Brahmins.",
            "But adverse for father due to significator-in-house effect (Sun signifies father; 9th is father).",
        ]
    elif sun_house_no == 10:
        reading_sun_lines += [
            "Renowned, wise, powerful; very wealthy; sons and relatives prosper; completes undertakings; unconquerable; status akin to a king.",
        ]
    elif sun_house_no == 11:
        reading_sun_lines += [
            "Wealthy, powerful, efficient; enjoys varied comforts and gains.",
        ]
    elif sun_house_no == 12:
        reading_sun_lines += [
            "Physical ailments; eye disease; deviation from rightful vocation; wandering; inimical to father.",
        ]

    reading_sun_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on the Sun</h3>"
        f"<p class='text-center mb-1'><em>{header_sun}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_sun_lines)
        + "</div>"
    )

    # Mahadasha timing note (Sun)
    md_sun = _md_period_for(sun_pid)
    md_sun_note_html = ""
    if md_sun:
        _sS, _eS = md_sun
        md_sun_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {sun_name}:</strong> "
            f"{_sS:%Y-%m-%d} – {_eS:%Y-%m-%d}</p>"
        )

    # Weakness check specific to Sun (Baladi/Jagradadi avasthas + Śaḍbala threshold)
    weak_sun_note_html = ""
    sb_sun_val = _extract_shadbala_val(sb_res, sun_pid)
    sb_sun_weak = False
    if sb_sun_val is not None and sun_pid in SHAD_THRESH:
        sb_sun_weak = sb_sun_val < SHAD_THRESH[sun_pid]

    sun_weak = (sun_pid in avs["bala"]) or (sun_pid in avs["mrita"]) or (sun_pid in avs["sushupti"]) or sb_sun_weak
    if sun_weak:
        weak_sun_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Sun is weak</p>"
        )

    # Attach MD line and the Sun-only weakness note inside this block
    reading_sun_html = reading_sun_html.replace("</div>", f"{md_sun_note_html}{weak_sun_note_html}</div>")
    
        # ── Reading based on the Moon (graha-specific) ─────────────────────────
    moon_pid = const._MOON
    moon_name = PLANET_NAMES.get(moon_pid, "Moon")

    # Moon's house from Lagna
    moon_house_idx = p2h.get(moon_pid)
    if moon_house_idx is None:
        moon_house_idx = (_planet_sign(moon_pid) - lagna_sign) % 12
    moon_house_no = moon_house_idx + 1

    # simple malefic influence checks local to this block
    NAT_MALEFICS_MOON = {
        const._SUN, const._MARS, const._SATURN,
        getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2),
    }

    def _planets_in_house_local(h_idx: int):
        return [p for p, h in p2h.items() if h == h_idx]

    def _malefic_aspects_house_local(target_idx: int) -> bool:
        """Classical aspects: everyone 7th; Mars 4/8; Jupiter 5/9; Saturn 3/10; Rahu/Ketu 5/9."""
        RAHU = getattr(const, "_RAHU", -1)
        KETU = getattr(const, "_KETU", -2)
        special = {
            const._MARS: {3, 6, 7},     # 4th,7th,8th (as 0-based steps)
            const._JUPITER: {4, 6, 8},  # 5th,7th,9th
            const._SATURN: {2, 6, 9},   # 3rd,7th,10th
            RAHU: {4, 6, 8},            # treat nodes like Jupiter
            KETU: {4, 6, 8},
        }
        for p in NAT_MALEFICS_MOON:
            p_house = p2h.get(p)
            if p_house is None:
                continue
            step = (target_idx - p_house) % 12
            if step == 6 or step in special.get(p, set()):
                return True
        return False

    # first-house specific: check malefic influence on Moon itself
    in_same_house_moon = set(_planets_in_house_local(moon_house_idx)) - {moon_pid}
    moon_malefic_influence = any(p in NAT_MALEFICS_MOON for p in in_same_house_moon) or _malefic_aspects_house_local(moon_house_idx)

    # Full Moon heuristic: Sun–Moon separation ≈ 180°
    sun_lon = _get_lon(const._SUN)
    moon_lon = _get_lon(const._MOON)
    sep = abs((moon_lon - sun_lon + 180) % 360 - 180)  # shortest arc
    is_fullish = sep >= 170  # tolerant band for “full”

    reading_moon_lines: list[str] = []
    header_moon = f"Moon is in the {SIGN_TXT[moon_house_idx]} house."

    if moon_house_no == 1:
        # General harsh set doesn’t apply for Lagna Aries/Taurus/Cancer
        if lagna_sign not in (0, 1, 3):
            reading_moon_lines += [
                "Unstable mind; risks of derangement or sensory issues (deaf/mute); harsh temperament; darker/harsh looks.",
            ]
            if moon_malefic_influence:
                reading_moon_lines.append("With malefic influence on Moon in Lagna: classic texts warn of shortened longevity.")
        # Special sign-specific clauses
        if lagna_sign == 0:   # Mesha/Aries
            reading_moon_lines.append("With Aries rising: many children are indicated.")
        if lagna_sign == 1:   # Vrisha/Taurus (exaltation)
            reading_moon_lines.append("With exalted Taurus rising: wealthy, famous and pleasant-looking.")
        if lagna_sign == 3:   # Karka/Cancer (own sign)
            reading_moon_lines.append("With Cancer rising (own sign): wealthy, famous and good-looking.")
        if is_fullish:
            reading_moon_lines.append("Full Moon in Lagna: fearless, wealthy and long-lived.")
    elif moon_house_no == 2:
        reading_moon_lines += [
            "Sweet speech; wealth and comforts; fond of women; large family; sparing with words.",
        ]
    elif moon_house_no == 3:
        reading_moon_lines += [
            "Virtuous and brave; enjoys support from siblings; educated.",
        ]
    elif moon_house_no == 4:
        reading_moon_lines += [
            "Generally happy; somewhat detached; learned and sensuous; fond of water sports/travel.",
        ]
    elif moon_house_no == 5:
        reading_moon_lines += [
            "Children, wealth and learning are supported; however, timidity is noted.",
        ]
    elif moon_house_no == 6:
        reading_moon_lines += [
            "Shorter longevity indications; delicate, easily angered; troubles from opponents; abdominal complaints.",
        ]
    elif moon_house_no == 7:
        reading_moon_lines += [
            "Good looks; strong sexual appetite; beautiful spouse; tendency to wander.",
        ]
    elif moon_house_no == 8:
        reading_moon_lines += [
            "Wise yet fickle; disease-prone; shortened longevity indicated.",
        ]
    elif moon_house_no == 9:
        reading_moon_lines += [
            "Dutiful; comforts, wealth, learning and children; admired by women.",
        ]
    elif moon_house_no == 10:
        reading_moon_lines += [
            "Wealthy, pious and efficient; powerful and liberal; completes undertakings thoroughly.",
        ]
    elif moon_house_no == 11:
        reading_moon_lines += [
            "Wealthy and famous; brave and thoughtful; blessed with sons; long-lived.",
        ]
    elif moon_house_no == 12:
        reading_moon_lines += [
            "Indolence and humiliation; misery and moral fall; eye disease; foreign residence likely.",
        ]

    reading_moon_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on the Moon</h3>"
        f"<p class='text-center mb-1'><em>{header_moon}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_moon_lines)
        + "</div>"
    )

    # Mahadasha timing note (Moon)
    md_moon = _md_period_for(moon_pid)
    md_moon_note_html = ""
    if md_moon:
        _sM, _eM = md_moon
        md_moon_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {moon_name}:</strong> "
            f"{_sM:%Y-%m-%d} – {_eM:%Y-%m-%d}</p>"
        )

    # Weakness check specific to Moon (Avasthas & Śaḍbala threshold)
    weak_moon_note_html = ""
    sb_moon_val = _extract_shadbala_val(sb_res, moon_pid)
    sb_moon_weak = False
    if sb_moon_val is not None and moon_pid in SHAD_THRESH:
        sb_moon_weak = sb_moon_val < SHAD_THRESH[moon_pid]

    moon_weak = (moon_pid in avs["bala"]) or (moon_pid in avs["mrita"]) or (moon_pid in avs["sushupti"]) or sb_moon_weak
    if moon_weak:
        weak_moon_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Moon is weak</p>"
        )

    # Attach MD line and Moon-only weakness note
    reading_moon_html = reading_moon_html.replace("</div>", f"{md_moon_note_html}{weak_moon_note_html}</div>")
    
        # ── Reading based on Mars (Kuja) ─────────────────────────────────────────
    mars_house_idx = p2h.get(const._MARS)
    if mars_house_idx is None:
        mars_house_idx = (_planet_sign(const._MARS) - lagna_sign) % 12
    mars_house_no = mars_house_idx + 1

    SIGN_TXT_MARS = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    mars_lines: list[str] = []
    header_mars = f"Mars is in the {SIGN_TXT_MARS[mars_house_idx]} house."

    if mars_house_no == 1:
        mars_lines += [
            "Harsh and dare-devilish; restless and injury-prone; health is delicate; lifespan can feel shortened.",
        ]
    elif mars_house_no == 2:
        mars_lines += [
            "Struggles with learning and stable wealth; oral/teeth issues; drifts from good company; roaming nature.",
        ]
    elif mars_house_no == 3:
        mars_lines += [
            "Very valorous and strong; hard to defeat; upstanding conduct; stressful indications for younger siblings.",
        ]
    elif mars_house_no == 4:
        mars_lines += [
            "Deprivation of home, land, funds, mother’s support and close friends; courage remains high.",
        ]
    elif mars_house_no == 5:
        mars_lines += [
            "Unsettled and unrighteous streak; risk of fewer comforts from children, wealth and allies; mental peace suffers.",
        ]
    elif mars_house_no == 6:
        mars_lines += [
            "High drive, appetite and digestive power; crushes opponents; leadership and command are highlighted.",
        ]
    elif mars_house_no == 7:
        mars_lines += [
            "Harsh temperament; health issues; risk to spouse or separation; slim frame; poverty-quarrels mix possible.",
        ]
    elif mars_house_no == 8:
        mars_lines += [
            "Suffering and poor health; longevity strain; violations/taboos attract; physical injuries possible.",
        ]
    elif mars_house_no == 9:
        mars_lines += [
            "Rebellious to dharma; treachery or harm indicators; yet can gain patronage from authority; stressful for parents.",
        ]
    elif mars_house_no == 10:
        mars_lines += [
            "Formidable and liberal; courageous; status near-royalty; fame and high regard in public life.",
        ]
    elif mars_house_no == 11:
        mars_lines += [
            "Wealth accumulation with strong desires; brave and sexually driven; achieves cherished gains.",
        ]
    elif mars_house_no == 12:
        mars_lines += [
            "Harsh and repulsive conduct; risk to marriage; danger of confinement, misery and eye complaints.",
        ]

    reading_mars_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Mars</h3>"
        f"<p class='text-center mb-1'><em>{header_mars}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in mars_lines)
        + "</div>"
    )

    # Mahadasha note for Mars
    md_mars = _md_period_for(const._MARS)
    md_mars_note_html = ""
    if md_mars:
        _sm, _em = md_mars
        md_mars_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Mars:</strong> "
            f"{_sm:%Y-%m-%d} – {_em:%Y-%m-%d}</p>"
        )

    # Weakness check for Mars (Baladi/Jagradadi avasthas + Śaḍbala)
    weak_mars_note_html = ""
    sb_mars_val = _extract_shadbala_val(sb_res, const._MARS)
    sb_mars_weak = False
    if sb_mars_val is not None and const._MARS in SHAD_THRESH:
        sb_mars_weak = sb_mars_val < SHAD_THRESH[const._MARS]
    mars_is_weak = (
        (const._MARS in avs["bala"]) or
        (const._MARS in avs["mrita"]) or
        (const._MARS in avs["sushupti"]) or
        sb_mars_weak
    )
    if mars_is_weak:
        weak_mars_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Mars is weak</p>"
        )

    # Attach MD line and the weakness note inside this block
    reading_mars_html = reading_mars_html.replace("</div>", f"{md_mars_note_html}{weak_mars_note_html}</div>")
    
        # ── Reading based on Mercury (intellect/skills/commerce) ────────────────
    merc_pid = const._MERCURY
    merc_name = PLANET_NAMES.get(merc_pid, "Mercury")

    merc_house_idx = p2h.get(merc_pid)
    if merc_house_idx is None:
        merc_house_idx = (_planet_sign(merc_pid) - lagna_sign) % 12
    merc_house_no = merc_house_idx + 1

    SIGN_TXT_MER = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    header_mercury = f"Mercury is in the {SIGN_TXT_MER[merc_house_idx]} house."
    reading_mercury_lines: list[str] = []

    if merc_house_no == 1:
        reading_mercury_lines += [
            "Healthy constitution; articulate and pleasant speech.",
            "Sharp intellect—mathematical/logical ability; scriptural or scholarly bend; long life.",
        ]
    elif merc_house_no == 2:
        reading_mercury_lines += [
            "Polished speech; educated; wealthy; enjoys fine meals and comforts.",
        ]
    elif merc_house_no == 3:
        reading_mercury_lines += [
            "Restless/variable disposition; hands-on hard worker; can be crafty or deceptive.",
            "Occult/magical interests possible; generally supported by siblings.",
        ]
    elif merc_house_no == 4:
        reading_mercury_lines += [
            "Very learned; wealth, vehicles, home and comforts are supported.",
            "Good friendships and social network around the home base.",
        ]
    elif merc_house_no == 5:
        reading_mercury_lines += [
            "Recognition through learning/skills; mantra or technical proficiency.",
            "Many children indicated; courageous and generally content.",
        ]
    elif merc_house_no == 6:
        reading_mercury_lines += [
            "Argumentative and quick-tempered; enjoys debate and wins against opponents.",
            "Can be indolent and prone to ailments; not very supportive to close relations.",
        ]
    elif merc_house_no == 7:
        reading_mercury_lines += [
            "Knowledgeable, wise and reputable; spouse tends to be resourceful/wealthy.",
        ]
    elif merc_house_no == 8:
        reading_mercury_lines += [
            "Name/fame despite obstacles; long-lived indications; judicial or arbitration abilities.",
        ]
    elif merc_house_no == 9:
        reading_mercury_lines += [
            "Prosperous and learned; eloquent; clever; virtuous and law-abiding.",
        ]
    elif merc_house_no == 10:
        reading_mercury_lines += [
            "Accomplished and efficient; righteous in professional conduct; well-known for skills.",
        ]
    elif merc_house_no == 11:
        reading_mercury_lines += [
            "Long life; truthful and intellectual; wealth, recognition and sensual enjoyments.",
        ]
    elif merc_house_no == 12:
        reading_mercury_lines += [
            "Indolent and withdrawn or austere; can appear unappealing; yet learned and sweet-spoken.",
        ]

    reading_mercury_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Mercury</h3>"
        f"<p class='text-center mb-1'><em>{header_mercury}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_mercury_lines)
        + "</div>"
    )

    # Mahadasha window for Mercury
    md_mer = _md_period_for(merc_pid)
    md_mer_note_html = ""
    if md_mer:
        _sm, _em = md_mer
        md_mer_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Mercury:</strong> "
            f"{_sm:%Y-%m-%d} – {_em:%Y-%m-%d}</p>"
        )

    # Weakness note for Mercury (Avasthas & Śaḍbala)
    weak_mer_note_html = ""
    sb_merc = _extract_shadbala_val(sb_res, merc_pid)
    sb_merc_weak = False
    if (sb_merc is not None) and (merc_pid in SHAD_THRESH):
        sb_merc_weak = sb_merc < SHAD_THRESH[merc_pid]

    merc_weak = (merc_pid in avs["bala"]) or (merc_pid in avs["mrita"]) or (merc_pid in avs["sushupti"]) or sb_merc_weak
    if merc_weak:
        weak_mer_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Mercury is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    reading_mercury_html = reading_mercury_html.replace("</div>", f"{md_mer_note_html}{weak_mer_note_html}</div>")
    
        # ── Reading based on Jupiter (Guru) ─────────────────────────────────────
    j_pid = const._JUPITER
    j_name = PLANET_NAMES.get(j_pid, "Jupiter")

    j_house_idx = p2h.get(j_pid)
    if j_house_idx is None:
        j_house_idx = (_planet_sign(j_pid) - asc_sign) % 12
    j_house_no = j_house_idx + 1

    SIGN_TXTJ = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading_j_lines: list[str] = []
    header_j = f"Jupiter is in the {SIGN_TXTJ[j_house_idx]} house."

    if j_house_no == 1:
        reading_j_lines += [
            "Learned, fearless and long-lived; balanced outlook; handsome; wealth indicated.",
        ]
    elif j_house_no == 2:
        reading_j_lines += [
            "Wealthy; eloquent; pleasant looks; enjoys good food; helpful and liberal.",
        ]
    elif j_house_no == 3:
        reading_j_lines += [
            "Subdued by sibling and spouse; covetous and troubled; still overcomes opponents; sluggish digestion; supportive for siblings.",
        ]
    elif j_house_no == 4:
        reading_j_lines += [
            "Comforts, wealth, vehicles and wise counsel; surrounded by near ones; defeats foes; content and good-looking.",
        ]
    elif j_house_no == 5:
        reading_j_lines += [
            "Learned, famous, wealthy and virtuous; advisory/ministerial capacity; strain through children is indicated.",
        ]
    elif j_house_no == 6:
        reading_j_lines += [
            "Indolent yet defeats enemies; weak digestion; hen-pecked tendencies; very famous; physically weak and lustful.",
        ]
    elif j_house_no == 7:
        reading_j_lines += [
            "Learned and renowned; surpasses the father; blessed with a good spouse and children.",
        ]
    elif j_house_no == 8:
        reading_j_lines += [
            "Miserable and servile tone; livelihood through service; unclean habits; adulterous streak; long life is indicated.",
        ]
    elif j_house_no == 9:
        reading_j_lines += [
            "Devout and learned; wealthy and famed; blessed with sons; leadership or ministerial role.",
        ]
    elif j_house_no == 10:
        reading_j_lines += [
            "Brings undertakings to completion; endowed with wisdom, wealth and virtue.",
        ]
    elif j_house_no == 11:
        reading_j_lines += [
            "Wealthy, steadfast and long-lived; fewer sons indicated.",
        ]
    elif j_house_no == 12:
        reading_j_lines += [
            "Indolent, irresolute and morally compromised; servile; lack of progeny indicated.",
        ]

    reading_jupiter_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Jupiter</h3>"
        f"<p class='text-center mb-1'><em>{header_j}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_j_lines)
        + "</div>"
    )

    # Mahadasha note for Jupiter
    md_j = _md_period_for(j_pid)
    md_j_note_html = ""
    if md_j:
        _sj, _ej = md_j
        md_j_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Jupiter:</strong> "
            f"{_sj:%Y-%m-%d} – {_ej:%Y-%m-%d}</p>"
        )

    # Weakness note for Jupiter (Avasthas & Shadbala)
    weak_j_note_html = ""
    sb_j_val = _extract_shadbala_val(sb_res, j_pid)
    sb_j_weak = False
    if sb_j_val is not None and j_pid in SHAD_THRESH:
        sb_j_weak = sb_j_val < SHAD_THRESH[j_pid]

    weak_j = (j_pid in avs["bala"]) or (j_pid in avs["mrita"]) or (j_pid in avs["sushupti"]) or sb_j_weak
    if weak_j:
        weak_j_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the Jupiter is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    reading_jupiter_html = reading_jupiter_html.replace("</div>", f"{md_j_note_html}{weak_j_note_html}</div>")
    
        # ── Reading based on Venus (Śukra) ──────────────────────────────────────
    v_pid = const._VENUS
    v_name = PLANET_NAMES.get(v_pid, "Venus")

    v_house_idx = p2h.get(v_pid)
    if v_house_idx is None:
        v_house_idx = (_planet_sign(v_pid) - lagna_sign) % 12
    v_house_no = v_house_idx + 1

    SIGN_TXTV = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    header_v = f"Venus is in the {SIGN_TXTV[v_house_idx]} house."
    v_lines: list[str] = []

    if v_house_no == 1:
        v_lines += [
            "Attractive presence; romantic; educated; generally contented and long-lived.",
        ]
    elif v_house_no == 2:
        v_lines += [
            "Wealth and grace; refined speech; poetic/creative talent.",
        ]
    elif v_house_no == 3:
        v_lines += [
            "Covetous yet financially able; tends to be influenced/managed by spouse; avoids strenuous ventures.",
        ]
    elif v_house_no == 4:
        v_lines += [
            "Good home, ornaments, clothing and vehicles; pleasing looks; boastful streak; often yields to spouse.",
        ]
    elif v_house_no == 5:
        v_lines += [
            "Wealth, sensuality and status; comforts, children and friends are supported; pleasing appearance.",
        ]
    elif v_house_no == 6:
        v_lines += [
            "Few open enemies but poverty/misery themes; many romantic ties; little joy from spouse; reputation suffers.",
        ]
    elif v_house_no == 7:
        v_lines += [
            "Quarrelsome and very passionate; charming looks; association with alluring but low characters.",
        ]
    elif v_house_no == 8:
        v_lines += [
            "Longevity with opulence; many comforts; stature akin to royalty; a contented tone overall.",
        ]
    elif v_house_no == 9:
        v_lines += [
            "Learned and wealthy; spouse, children, friends and comforts indicated; religious inclination.",
        ]
    elif v_house_no == 10:
        v_lines += [
            "High status, influence and wealth; public image strengthened; benefits and help through women.",
        ]
    elif v_house_no == 11:
        v_lines += [
            "Affluence; liaison with women not one’s own; relief from pains and miseries.",
        ]
    elif v_house_no == 12:
        v_lines += [
            "Indolence and fall from standards; skilled in love; debauchery noted.",
        ]

    reading_venus_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Venus</h3>"
        f"<p class='text-center mb-1'><em>{header_v}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in v_lines)
        + "</div>"
    )

    # Mahadasha note for Venus
    md_v = _md_period_for(v_pid)
    md_v_note_html = ""
    if md_v:
        _sv, _ev = md_v
        md_v_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Venus:</strong> "
            f"{_sv:%Y-%m-%d} – {_ev:%Y-%m-%d}</p>"
        )

    # Weakness note for Venus (Avasthas & Śaḍbala)
    weak_v_note_html = ""
    sb_v_val = _extract_shadbala_val(sb_res, v_pid)
    sb_v_weak = False
    if sb_v_val is not None and v_pid in SHAD_THRESH:
        sb_v_weak = sb_v_val < SHAD_THRESH[v_pid]

    v_is_weak = (v_pid in avs["bala"]) or (v_pid in avs["mrita"]) or (v_pid in avs["sushupti"]) or sb_v_weak
    if v_is_weak:
        weak_v_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Venus is weak</p>"
        )

    # Attach MD line + weakness note within this block
    reading_venus_html = reading_venus_html.replace("</div>", f"{md_v_note_html}{weak_v_note_html}</div>")
    
        # ── Reading based on Saturn (Śani) ──────────────────────────────────────
    s_pid = const._SATURN
    s_name = PLANET_NAMES.get(s_pid, "Saturn")

    s_house_idx = p2h.get(s_pid)
    if s_house_idx is None:
        s_house_idx = (_planet_sign(s_pid) - lagna_sign) % 12
    s_house_no = s_house_idx + 1

    # House names for narration
    SIGN_TXTS = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    header_s = f"Saturn is in the {SIGN_TXTS[s_house_idx]} house."
    s_lines: list[str] = []

    if s_house_no == 1:
        # Special condition: very good results if Lagna itself is Libra (exaltation),
        # Capricorn/Aquarius (own), or Jupiter’s signs Sagittarius/Pisces.
        very_good_lagnas = {_EXALTS[const._SATURN],  # Libra (6)
                            9, 10,                   # Capricorn, Aquarius
                            8, 11}                   # Sagittarius, Pisces
        if lagna_sign in very_good_lagnas:
            s_lines += [
                "Despite Saturn in the ascendant, this specific Lagna makes the placement powerfully auspicious.",
                "Status and authority (king-like/headman), long life, virtue and scholarship are indicated.",
            ]
        else:
            s_lines += [
                "Difficult ascendant placement: misery, lethargy, lust, poor health and looks; risk of bodily defects and bad odour.",
            ]
    elif s_house_no == 2:
        s_lines += [
            "Early life shows want and harsh speech; disease of the mouth likely.",
            "Later years: leaves birthplace and amasses wealth, possessions and comforts.",
        ]
    elif s_house_no == 3:
        s_lines += [
            "Morally rough/slothful streak, yet physically strong, liberal, wise and wealthy.",
        ]
    elif s_house_no == 4:
        s_lines += [
            "Stress around mother/home; distance from close kin; nevertheless wise and wealthy.",
            "Childhood/early sickness is indicated.",
        ]
    elif s_house_no == 5:
        s_lines += [
            "Mental unrest/instability; unhappiness; denial around children, comforts and wisdom.",
            "Still capable of overcoming opponents.",
        ]
    elif s_house_no == 6:
        s_lines += [
            "Wealth with strong appetite and libido; pleasing looks; yet harassment by enemies persists.",
        ]
    elif s_house_no == 7:
        s_lines += [
            "Chronic ailments and poverty themes; spouse may be sickly; sense of uncleanness or aversion from others.",
        ]
    elif s_house_no == 8:
        s_lines += [
            "Starts heroic and forceful but loses power and money later; perianal disease risk.",
            "Note: Saturn in the 8th is classically supportive for health and longevity overall.",
        ]
    elif s_house_no == 9:
        s_lines += [
            "Irreligious streak; misfortune and poverty; adverse to father; can be hurtful to others.",
        ]
    elif s_house_no == 10:
        s_lines += [
            "Learned, wealthy, powerful—judicial/leadership roles possible; proud and heroic bearing.",
        ]
    elif s_house_no == 11:
        s_lines += [
            "Stable reputation, good health and great wealth; sensuality; longevity indicated.",
        ]
    elif s_house_no == 12:
        s_lines += [
            "Eye troubles and wasteful spending; shameless conduct and suffering; still shows leadership in harsh contexts.",
        ]

    reading_saturn_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Saturn</h3>"
        f"<p class='text-center mb-1'><em>{header_s}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in s_lines)
        + "</div>"
    )

    # Mahadasha note for Saturn
    md_s = _md_period_for(s_pid)
    md_s_note_html = ""
    if md_s:
        _ss, _es = md_s
        md_s_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Saturn:</strong> "
            f"{_ss:%Y-%m-%d} – {_es:%Y-%m-%d}</p>"
        )

    # Weakness note for Saturn (Avasthas & Śaḍbala)
    weak_s_note_html = ""
    sb_s_val = _extract_shadbala_val(sb_res, s_pid)
    sb_s_weak = False
    if sb_s_val is not None and s_pid in SHAD_THRESH:
        sb_s_weak = sb_s_val < SHAD_THRESH[s_pid]

    s_is_weak = (s_pid in avs["bala"]) or (s_pid in avs["mrita"]) or (s_pid in avs["sushupti"]) or sb_s_weak
    if s_is_weak:
        weak_s_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Saturn is weak</p>"
        )

    # Attach MD line + weakness note within this block
    reading_saturn_html = reading_saturn_html.replace("</div>", f"{md_s_note_html}{weak_s_note_html}</div>")
    
        # ── Reading based on Rahu (Rāhu) ───────────────────────────────────────
    r_pid = getattr(const, "_RAHU", 7)
    r_name = PLANET_NAMES.get(r_pid, "Rahu")

    r_house_idx = p2h.get(r_pid)
    if r_house_idx is None:
        r_house_idx = (_planet_sign(r_pid) - lagna_sign) % 12
    r_house_no = r_house_idx + 1

    # sign (for special conditions)
    try:
        r_sign = int(natal_pp[r_pid + 1][1][0])
    except Exception:
        try:
            _rlon = float(natal_pp[r_pid + 1][1][1]) % 360.0
            r_sign = int(_rlon // 30)
        except Exception:
            r_sign = (lagna_sign + r_house_idx) % 12

    # Helper: benefic aspect on the Ascendant (house index 0)
    def _benefic_aspects_asc() -> bool:
        """Check if a natural benefic (Moon, Venus, Jupiter, Mercury) aspects Lagna.
        Uses classical aspects: all planets -> 7th; Jupiter adds 5th & 9th."""
        target_idx = 0  # Lagna house index
        for p in (const._MOON, const._VENUS, const._JUPITER, const._MERCURY):
            p_idx = p2h.get(p)
            if p_idx is None:
                continue
            delta = (target_idx - p_idx) % 12
            # 7th aspect always
            if delta == 6:
                return True
            # Jupiter's special aspects (5th, 9th)
            if p == const._JUPITER and delta in (4, 8):
                return True
        return False

    SIGN_TXTR = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    header_r = f"Rahu is in the {SIGN_TXTR[r_house_idx]} house."
    r_lines: list[str] = []

    if r_house_no == 1:
        # Baseline statement
        r_lines += [
            "Harsh and unyielding streak; wealth comes with turbulence; shorter longevity themes; bold yet cruel; little compassion; rough hair/nails/appearance; ailments in the upper body.",
        ]
        # Mesha/Karka/Siṁha special
        if lagna_sign in {0, 3, 4}:
            r_lines.append("Because Lagna is Aries, Cancer, or Leo: pleasures and affluence are specifically indicated despite the harsh tone.")
        # Benefic aspect on Lagna
        if _benefic_aspects_asc():
            r_lines.append("Benefic aspect on the ascendant adds comforts and enjoyments, softening Rahu’s edge.")
    elif r_house_no == 2:
        r_lines += [
            "Quarrels, poverty themes and theft-like behaviours; relies on patronage/authority for income.",
            "Unclear or double-edged speech; mouth/teeth issues; trades linked to skins/fish/odd commodities.",
        ]
    elif r_house_no == 3:
        r_lines += [
            "Wealthy, valiant, proud and long-lived; tension with brothers; comforts of spouse/friends/pleasures indicated.",
        ]
        # Rahu exaltation (Vr̥ṣabha/Taurus)
        if r_sign == 1:
            r_lines.append("Rahu in Taurus (its exaltation per tradition): gains include vehicles and attendants.")
    elif r_house_no == 4:
        r_lines += [
            "Destitution and folly; reduced lifespan; loss of wealth and home comforts; conflict with spouse around domestic matters.",
        ]
    elif r_house_no == 5:
        r_lines += [
            "Irascible; risks to children; compassionate yet phobic; abdominal disorders indicated.",
        ]
    elif r_house_no == 6:
        r_lines += [
            "Harried by foes yet also their destroyer; wealth, children and many comforts show up; adultery themes; perianal disease; longer life indicated.",
        ]
    elif r_house_no == 7:
        r_lines += [
            "Loss through women; adulterous ties; separation/bereft of spouse; wicked yet brave; chronic ailments.",
        ]
    elif r_house_no == 8:
        r_lines += [
            "Shorter longevity patterns; vāta disorders; few children; misery with fearlessness; perianal disease; lethargy.",
        ]
    elif r_house_no == 9:
        r_lines += [
            "Leader/headman type; opposes father; harsh/cruel expression; harried by opponents.",
        ]
    elif r_house_no == 10:
        r_lines += [
            "Fearless, helpful and famous; sensual and prone to unlawful ventures; can be learned, wealthy and advisory (minister-like); detached, wandering.",
        ]
    elif r_house_no == 11:
        r_lines += [
            "Wealth and longevity; fewer children; combative spirit with self-control; handsome and laconic; scriptural bent; foreign residence; ear disorders.",
        ]
    elif r_house_no == 12:
        r_lines += [
            "Loss of comforts, money and virtue; immorality and secret sin; fickleness and chronic ailments; water-borne disease; foreign residence.",
        ]

    reading_rahu_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Rahu</h3>"
        f"<p class='text-center mb-1'><em>{header_r}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in r_lines)
        + "</div>"
    )

    # Mahadasha note for Rahu
    md_r = _md_period_for(r_pid)
    md_r_note_html = ""
    if md_r:
        _sr, _er = md_r
        md_r_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of Rahu:</strong> "
            f"{_sr:%Y-%m-%d} – {_er:%Y-%m-%d}</p>"
        )

    # Weakness note for Rahu (Avasthas & Śaḍbala if available)
    weak_r_note_html = ""
    sb_r_val = _extract_shadbala_val(sb_res, r_pid)
    sb_r_weak = False
    if r_pid in SHAD_THRESH and sb_r_val is not None:
        sb_r_weak = sb_r_val < SHAD_THRESH[r_pid]

    r_is_weak = (r_pid in avs["bala"]) or (r_pid in avs["mrita"]) or (r_pid in avs["sushupti"]) or sb_r_weak
    if r_is_weak:
        weak_r_note_html = (
            "<p class='text-center mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Rahu is weak</p>"
        )

    # Attach MD line + weakness note within this block
    reading_rahu_html = reading_rahu_html.replace("</div>", f"{md_r_note_html}{weak_r_note_html}</div>")
    
        # ── Reading based on Ketu (moksha-karaka / detachment) ───────────────────
    KETU = getattr(const, "_KETU", -2)
    ketu_name = "Ketu"
    # House of Ketu (prefer p2h; fallback via sign difference)
    k_house_idx = p2h.get(KETU)
    if k_house_idx is None:
        # derive from rasi sign
        try:
            k_sign_idx = int(natal_pp[KETU + 1][1][0])
        except Exception:
            try:
                _klon = float(natal_pp[KETU + 1][1][1]) % 360.0
                k_sign_idx = int(_klon // 30)
            except Exception:
                k_sign_idx = asc_sign  # safest fallback
        k_house_idx = (k_sign_idx - asc_sign) % 12
    k_house_no = k_house_idx + 1

    # Also need the current sign of Ketu for conditional notes
    def _planet_sign_safe(pid: int) -> int:
        try:
            return int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                _lon = float(natal_pp[pid + 1][1][1]) % 360.0
                return int(_lon // 30)
            except Exception:
                return asc_sign
    k_sign = _planet_sign_safe(KETU)

    # Helpers for the conditions you specified
    BENEFIC_SET = {const._JUPITER, const._VENUS, const._MERCURY, const._MOON}
    def _benefic_aspects_house_local(target_idx: int) -> bool:
        """Use the benefic aspect helper already defined earlier if present, else minimal fallback."""
        try:
            # if the earlier helper exists, use it
            return _benefic_aspects_house(target_idx)  # type: ignore[name-defined]
        except Exception:
            # minimal 7th-aspect fallback
            for p in BENEFIC_SET:
                p_h = p2h.get(p)
                if p_h is None:
                    continue
                if (target_idx - p_h) % 12 == 6:
                    return True
            return False

    # Signs of Saturn for the special Lagna condition
    SATURN_SIGNS = {10, 11}  # Capricorn, Aquarius (0=Aries)
    # Signs of natural benefics (for “in sign of a benefic” check)
    def _sign_is_of_benefic(sign_idx: int) -> bool:
        return _SIGN_LORD[sign_idx] in BENEFIC_SET

    SIGN_TXT_K = [
        "first", "second", "third", "fourth", "fifth", "sixth",
        "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    ]

    reading_ketu_lines: list[str] = []
    header_ketu = f"{ketu_name} is in the {SIGN_TXT_K[k_house_idx]} house."

    if k_house_no == 1:
        reading_ketu_lines += [
            "Harsh temperament with courage; little compassion; appearance and nails/hair may be unattractive.",
            "Upper-body ailments are possible; wealth can come despite a rough edge.",
        ]
        # (i) Benefic aspect on Lagna
        if _benefic_aspects_house_local(0):
            reading_ketu_lines.append("With benefic aspect on the ascendant: comforts and enjoyments are indicated.")
        # (ii) In Lagna in a Saturn sign
        if asc_sign in SATURN_SIGNS:
            reading_ketu_lines.append("Placed in a Saturnic ascendant: wealth and children are supported.")
    elif k_house_no == 2:
        reading_ketu_lines += [
            "Quarrelsome and sharp-tongued; risk of unclear speech or mouth/teeth issues.",
            "Poverty or dependence on patrons; gains may come through authorities or unusual trades (skins, fish, etc.).",
        ]
        # Benefic sign condition
        if _sign_is_of_benefic(k_sign):
            reading_ketu_lines.append("Because it is in a sign of a natural benefic: physical comforts are improved.")
    elif k_house_no == 3:
        reading_ketu_lines += [
            "Wealthy, valiant and proud; long life; conflict with siblings is possible.",
            "If strongly placed, vehicles and servants are supported.",
        ]
    elif k_house_no == 4:
        reading_ketu_lines += [
            "Loss of mother’s support and homely comforts; financial strain and frequent relocations.",
            "Opposition to spouse; inclination to spread malicious talk.",
        ]
    elif k_house_no == 5:
        reading_ketu_lines += [
            "Phobias and abdominal issues; fear of water; weak for learning and progeny.",
        ]
    elif k_house_no == 6:
        reading_ketu_lines += [
            "Destroys enemies; good health and generosity; erudition with a sharp edge.",
            "May face humiliation via maternal uncle; gains through quadrupeds/livestock.",
        ]
    elif k_house_no == 7:
        reading_ketu_lines += [
            "Little marital comfort; wandering and poor judgment in partners; losses through women; intestinal or seminal disorders.",
            "Humiliation and fear from water are possible.",
        ]
        # Exaltation note (Vrischika/Scorpio)
        if k_sign == 7:  # Scorpio
            reading_ketu_lines.append("In Scorpio in the 7th: multiple material benefits are classically indicated.")
    elif k_house_no == 8:
        reading_ketu_lines += [
            "Perianal disease; separation from close ones; danger from weapons/accidents.",
            "Avarice and immorality can surface; health is delicate.",
        ]
        # Wealth-gain signs in the 8th (Mesha, Vrisha, Mithuna, Kanya, Vrischika)
        if k_sign in {0, 1, 2, 5, 7}:
            reading_ketu_lines.append("In this sign in the 8th: gains of wealth are indicated despite the harsh significations.")
    elif k_house_no == 9:
        reading_ketu_lines += [
            "Short-tempered yet eloquent; desires progeny but clashes with father and lacks siblings’ support.",
            "Fortune improves via help from foreigners/non-believers.",
        ]
    elif k_house_no == 10:
        reading_ketu_lines += [
            "Powerful and renowned; destroys opponents; inwardly spiritual/gnostic.",
            "Little comfort from father; wandering tendency; looks may be austere or unappealing.",
        ]
        # Extra classical note (strong placements like Mesha, Vrisha, Kanya, Vrischika are combative) already implicit.
    elif k_house_no == 11:
        reading_ketu_lines += [
            "Valiant, powerful and virtuous; learned and good-looking.",
            "Subtle fears; children may be troublesome; significant gains through effort.",
        ]
    elif k_house_no == 12:
        reading_ketu_lines += [
            "Secretive misconduct; ailments of legs/feet/anal region and eye.",
            "Victorious in conflict yet spends on charities; fickle in resolve.",
        ]

    reading_ketu_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Ketu</h3>"
        f"<p class='text-center mb-1'><em>{header_ketu}</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {line}</p>" for line in reading_ketu_lines)
        + "</div>"
    )

    # Mahadasha line for Ketu
    mdK = _md_period_for(KETU)
    mdK_note_html = ""
    if mdK:
        _sK, _eK = mdK
        mdK_note_html = (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {ketu_name}:</strong> "
            f"{_sK:%Y-%m-%d} – {_eK:%Y-%m-%d}</p>"
        )

    # Weakness note for Ketu (Avasthas & Shadbala) – local threshold per your spec
    weakK_note_html = ""
    try:
        sbK_val = _extract_shadbala_val(sb_res, KETU)
    except Exception:
        sbK_val = None
    # Recommended virupa threshold for Ketu
    KETU_THRESH = 300
    sbK_weak = (sbK_val is not None) and (sbK_val < KETU_THRESH)
    weakK = (KETU in avs["bala"]) or (KETU in avs["mrita"]) or (KETU in avs["sushupti"]) or sbK_weak
    if weakK:
        weakK_note_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {ketu_name} is weak</p>"
        )

    # Attach MD line and weakness note directly to Ketu block
    reading_ketu_html = reading_ketu_html.replace("</div>", f"{mdK_note_html}{weakK_note_html}</div>")
    
        # ── Readings: planets relative to the Moon (Chandra-lagna positions) ─────
    # Builds one mini-block per graha (Sun, Mars, Mercury, Jupiter, Venus, Saturn, Rahu)

    def _house_idx_or_sign(pid: int) -> int:
        """Return 0-based whole-sign house index for a planet; fall back to sign delta."""
        h = p2h.get(pid)
        if h is not None:
            return int(h)
        # fallback: use sign delta vs ascendant
        return (_planet_sign(pid) - lagna_sign) % 12

    def _rel_from_moon(pid: int) -> int:
        """0-based house *from the Moon* (Moon's house = 0)."""
        p_h = _house_idx_or_sign(pid)
        m_h = _house_idx_or_sign(const._MOON)
        return (p_h - m_h) % 12

    def _ord_txt(i0: int) -> str:
        ORD = ["first","second","third","fourth","fifth","sixth",
               "seventh","eighth","ninth","tenth","eleventh","twelfth"]
        return ORD[i0]

    def _weak_note_for(pid: int, pname: str) -> str:
        sb_val = _extract_shadbala_val(sb_res, pid)
        sb_weak = (pid in SHAD_THRESH) and (sb_val is not None) and (sb_val < SHAD_THRESH[pid])
        weak = (pid in avs["bala"]) or (pid in avs["mrita"]) or (pid in avs["sushupti"]) or sb_weak
        return (f"<p class='text-center mt-2'><strong>Note:</strong> "
                f"The above predictions may not manifest very strongly, since the {pname} is weak</p>") if weak else ""

    def _md_note_for(pid: int, pname: str) -> str:
        md = _md_period_for(pid)
        if not md:
            return ""
        s, e = md
        return (f"<p class='text-center mt-2'><strong>"
                f"The above effects would be more prominent in the mahadasha of {pname}:</strong> "
                f"{s:%Y-%m-%d} – {e:%Y-%m-%d}</p>")

    # helper: Jupiter aspect to a given house (0-based) from lagna
    def _jupiter_aspects_house(target_idx: int) -> bool:
        jh = _house_idx_or_sign(const._JUPITER)
        delta = (target_idx - jh) % 12
        return delta in {4, 6, 8}  # Jupiter’s 5th, 7th, 9th aspects

    reading_moon_rel_html = ""

    def _one_rel_block(pid: int, pname: str) -> str:
        i = _rel_from_moon(pid)        # 0-based
        n = i + 1                       # 1-based
        header = f"{pname} is in the {_ord_txt(i)} house from the Moon."
        lines: list[str] = []

        # ——— SUN from Moon ———
        if pid == const._SUN:
            if   n == 1:  lines += ["Long travels, indulgence in pleasures, and a taste for conflict."]
            elif n == 2:  lines += ["Servants/support staff; dignified bearing; favoured by authorities."]
            elif n == 3:  lines += ["Craves riches (especially gold); chaste; commands/controls people."]
            elif n == 4:  lines += ["Harms the mother or her wellbeing."]
            elif n == 5:  lines += ["Trouble via daughters; many sons indicated."]
            elif n == 6:  lines += ["Conquers enemies; works for warriors/authority (kṣatriya contexts)."]
            elif n == 7:  lines += ["Beautiful spouse; good conduct; honoured by rulers; ascetic leanings."]
            elif n == 8:  lines += ["Stirs strife; prone to ailments."]
            elif n == 9:  lines += ["Religious bent; truthful; suffers due to relatives."]
            elif n == 10: lines += ["Exceptionally wealthy; praised by the affluent."]
            elif n == 11: lines += ["Royal dignity; multi-skilled; famous; head of family."]
            elif n == 12: lines += ["One-eyed indication (classical)."]

        # ——— MARS from Moon ———
        elif pid == const._MARS:
            if   n == 1:  lines += ["Reddish eyes/complexion; bleeding wounds."]
            elif n == 2:  lines += ["Owns land; a son inclined to agriculture."]
            elif n == 3:  lines += ["About four brothers; good-natured; generally comfortable."]
            elif n == 4:  lines += ["Loss of comforts/wealth and risk of losing wife."]
            elif n == 5:  lines += ["Deprived of sons."]
            elif n == 6:  lines += ["Irreligious acts, illness and enmity."]
            elif n == 7:  lines += ["Spouse ill-natured and irritable."]
            elif n == 8:  lines += ["Sinful, violent; denies truthfulness."]
            elif n == 9:  lines += ["Wealth accrues; son in old age."]
            elif n == 10: lines += ["Conveyances, comforts and money."]
            elif n == 11: lines += ["Dignity at court; handsome presence."]
            elif n == 12: lines += ["Hurts all around him, including mother."]

        # ——— MERCURY from Moon ———
        elif pid == const._MERCURY:
            if   n == 1:  lines += ["Lacks ease and physical grace; harsh speech; restless wanderer."]
            elif n == 2:  lines += ["Wealth, house and kin; risk of cold-borne ailments."]
            elif n == 3:  lines += ["Property and wealth; gains via great persons or rulers."]
            elif n == 4:  lines += ["Ever comfortable; gains through maternal relations."]
            elif n == 5:  lines += ["Sharp intellect and learning; pleasing looks; sensual; harsh of tongue."]
            elif n == 6:  lines += ["Miserly, timid, conflict-averse; hairy body and large eyes."]
            elif n == 7:  lines += ["Dominated by women; miser yet wealthy; long life."]
            elif n == 8:  lines += ["Cold constitution; noted among rulers; feared by foes."]
            elif n == 9:  lines += ["Opposes own religion; absorbed in others’ religions; callous opposition to many."]
            elif n == 10:
                lines += ["Rāja-yoga indications (status/authority)."]
                # extra condition from the text – only emit if true
                if _house_idx_or_sign(const._MOON) == 9:  # Moon actually in 10th from lagna
                    lines += ["Because the Moon is in the 10th, status in the family rises (leader of the clan)."]
            elif n == 11: lines += ["Gains at every step; (classical) very early marriage."]
            elif n == 12: lines += ["Ever miserly; son is unsuccessful."]

        # ——— JUPITER from Moon ———
        elif pid == const._JUPITER:
            if   n == 1:  lines += ["Long-lived, healthy, powerful and consistently wealthy."]
            elif n == 2:  lines += ["Respected by rulers; swift; valorous; virtuous; long life (≈100 years)."]
            elif n == 3:  lines += ["Liked by women; father gains wealth in the native’s 17th year."]
            elif n == 4:  lines += ["Lacks comforts; maternal troubles; serves in others’ homes."]
            elif n == 5:  lines += ["Good eyesight; valorous; wealthy; dominating; sons indicated."]
            elif n == 6:  lines += ["Indifferent; homeless; long life but lives by low deeds/alms."]
            elif n == 7:  lines += ["Long-lived; sweet-tongued; healthy yet impotent; jaundice-prone; family leader."]
            elif n == 8:  lines += ["Frequent ailments and discomforts."]
            elif n == 9:  lines += ["Wealthy and virtuous; serves guru and gods."]
            elif n == 10: lines += ["Renounces wife and sons to become an ascetic."]
            elif n == 11: lines += ["Blessed sons, vehicles and king-like dignity."]
            elif n == 12:
                lines += ["Opposes his own people."]
                # extra condition (only when Jupiter aspects 6th house from lagna)
                if _jupiter_aspects_house(5):
                    lines += ["Still, Jupiter’s aspect to the 6th house promises comfort."]
        
        # ——— VENUS from Moon ———
        elif pid == const._VENUS:
            if   n == 1:  lines += ["Risk of death by water; paralysis; violent end."]
            elif n == 2:  lines += ["Wealthy; scholarly; king-like valour."]
            elif n == 3:  lines += ["Religious; wise; earnings via foreigners (mlecchhas)."]
            elif n == 4:  lines += ["Phlegmatic; weak body; loses money in old age."]
            elif n == 5:  lines += ["Many daughters; rich; little fame."]
            elif n == 6:  lines += ["Prodigal; loses in conflict."]
            elif n == 7:  lines += ["Lacks self-effort; foolish and suspicious nature."]
            elif n == 8:  lines += ["Famed fighter; generous; wealthy; obtains various comforts."]
            elif n == 9:  lines += ["Many brothers, sisters and friends."]
            elif n == 10: lines += ["Supports both parents; long life."]
            elif n == 11: lines += ["Long-lived; largely free of illness and opponents."]
            elif n == 12: lines += ["Associates with others’ wives; lewd and foolish."]

        # ——— SATURN from Moon ———
        elif pid == const._SATURN:
            if   n == 1:  lines += ["Adverse to health, friends and relatives."]
            elif n == 2:  lines += ["Bad for mother; survives on goat’s milk (classical)."]
            elif n == 3:  lines += ["Several daughters who die early (classical)."]
            elif n == 4:  lines += ["Shows purposeful effort; destroys foes."]
            elif n == 5:  lines += ["Wife is dark-complexioned and sweet-tongued."]
            elif n == 6:  lines += ["Short-lived indications; many troubles."]
            elif n == 7:  lines += ["Religious, generous; multiple marriages possible."]
            elif n == 8:  lines += ["Bad for father; alms/charity said to reduce ill effects."]
            elif n == 9:  lines += ["Loss of wealth during Saturn’s mahadasha (classical note)."]
            elif n == 10: lines += ["King-like status; miserly yet wealthy."]
            elif n == 11: lines += ["Poor health; irreligious."]
            elif n == 12: lines += ["Poor, beggarly and irreligious."]

        # ——— RAHU from Moon ———
        elif pid == getattr(const, "_RAHU", -1):
            if   n in (1, 10, 9): lines += ["Kingly rise; in old age retains only wealth."]
            elif n in (6, 12):    lines += ["King or minister; wealthy."]
            elif n in (4, 7):     lines += ["Adverse for parents; chronically unhappy."]
            elif n in (2, 11):    lines += ["Fame and wealth but little real comfort."]
            elif n == 5:          lines += ["Risk of death by drowning; few comforts."]
            else:                  lines += ["General Rahu effects are mixed and erratic here."]

        # Build HTML block
        block = (
            f"<div class='mt-4'>"
            f"<h3 class='h6 text-center'>Reading based on {pname} from the Moon</h3>"
            f"<p class='text-center mb-1'><em>{header}</em></p>"
            + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines)
            + _md_note_for(pid, pname)
            + _weak_note_for(pid, pname)
            + "</div>"
        )
        return block

    # Assemble all requested grahas in order
    GRAHAS_REL = [const._SUN, const._MARS, const._MERCURY,
                  const._JUPITER, const._VENUS, const._SATURN,
                  getattr(const, "_RAHU", -1)]

    for g in GRAHAS_REL:
        pname = PLANET_NAMES.get(g, str(g))
        reading_moon_rel_html += _one_rel_block(g, pname)

    html_out = f"""
<div class=\"container\"> 
  <h2 class=\"h5 mb-3 text-center\">Navagraha Summary</h2>
  <style>
    /* center-align entire table and all headers/cells */
    .table {{ margin-left: auto; margin-right: auto; }}
    .table th, .table td {{ text-align: center !important; vertical-align: middle; }}
  </style>
  <div class=\"table-responsive\"> 
    {table_html}
  </div>
  {reading_html}
  {reading2_html}
  {reading3_html}
  {reading4_html}
  {reading5_html}
  {reading6_html}
  {reading7_html}
  {reading8_html}
  {reading9_html}
  {reading10_html}
  {reading11_html}
  {reading12_html}
  {reading_sun_html}
  {reading_moon_html}
  {reading_mars_html}
  {reading_mercury_html}
  {reading_jupiter_html}
  {reading_venus_html}
  {reading_saturn_html}
  {reading_rahu_html}
  {reading_ketu_html}
  {reading_moon_rel_html}
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
