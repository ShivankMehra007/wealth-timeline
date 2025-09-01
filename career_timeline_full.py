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
        
    # ── Readings based on Rāśi (sign) locations of grahas ───────────────────
    # Helper: weakness flag & note (reuses SHAD_THRESH, avs, sb_res from above)
    def _is_weak(pid: int) -> bool:
        sb_val = _extract_shadbala_val(sb_res, pid)
        sb_weak = (pid in SHAD_THRESH) and (sb_val is not None) and (sb_val < SHAD_THRESH[pid])
        return (pid in avs["bala"]) or (pid in avs["mrita"]) or (pid in avs["sushupti"]) or sb_weak

    def _weak_note_line(name: str, pid: int) -> str:
        return (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {name} is weak</p>"
            if _is_weak(pid) else ""
        )

    def _md_line(pid: int, name: str) -> str:
        md = _md_period_for(pid)
        if not md:
            return ""
        s, e = md
        return (
            f"<p class='text-center mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {name}:</strong> "
            f"{s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
        )

    # convenience: house index of a planet (for aspect/association checks)
    def _house_idx_of(pid: int) -> int:
        h = p2h.get(pid)
        if h is not None:
            return h
        # fall back from sign
        return (_planet_sign(pid) - lagna_sign) % 12

    def _benefic_touches_pid(pid: int) -> bool:
        """Any natural benefic conjoins or aspects the planet’s house."""
        hidx = _house_idx_of(pid)
        # conjunction
        if any(p in BENEFICS_NATURAL for p, h in p2h.items() if h == hidx and p != pid):
            return True
        # classical 7th (+ specials handled inside helper)
        return _benefic_aspects_house(hidx)

    # ◼ SUN – sign based reading
    sun_sign = _planet_sign(const._SUN)
    sun_house_idx = _house_idx_of(const._SUN)
    sun_lines = []
    if sun_sign == 0:  # Mesha (Aries)
        sun_lines += [
            "Bold and combative; a striver with strong bones; fame by writing; restless, quick-tempered; blood/Pitta issues; wealth fluctuates; earnings linked to weapons or force.",
        ]
        # “If exalted, adverse influences are less marked”
        if _EXALTS.get(const._SUN) == sun_sign:
            sun_lines.append("Because the Sun is exalted here, the harsher notes tend to be muted.")
    elif sun_sign == 1:  # Taurus
        sun_lines += [
            "Tolerant, shrewd in dealings; may earn via scents, clothing or even shady acts; avoids female company; musical; prone to mouth/eye issues.",
        ]
    elif sun_sign == 2:  # Gemini
        sun_lines += [
            "Attractive, learned and wealthy; sweet-spoken; often skilled in astrology; quick learner; gains status; ‘two mothers’ symbolism appears.",
        ]
    elif sun_sign == 3:  # Cancer
        sun_lines += [
            "Can feel poor or burdened; friction with father/relatives; heavy labour; yet articulate and religious-leaning; Kapha-Pitta ailments possible.",
        ]
        if _benefic_touches_pid(const._SUN):
            sun_lines.append("Benefic support here gives a distinct royal bearing.")
    elif sun_sign == 4:  # Leo
        sun_lines += [
            "Firm, vigorous and learned; destroys enemies; outdoorsy; enjoys meat; wealthy, consistent, yet ear troubles are possible.",
        ]
    elif sun_sign == 5:  # Virgo
        sun_lines += [
            "Refined, creative and mathematically inclined; shy; multilingual; respectful; able to earn well despite physical delicacy.",
        ]
    elif sun_sign == 6:  # Libra
        sun_lines += [
            "Quarrelsome and unstable in status; can be humiliated by authority; pauperising tendencies; pulled toward others’ partners; dabbling in liquor-/metal-work; rash ‘foolhardy’ courage.",
        ]
    elif sun_sign == 7:  # Scorpio
        sun_lines += [
            "Argumentative and quick to fight; weapon-skilled; daring yet harsh; clashes with parents; risk from poison/fire; can still follow proper religious discipline.",
        ]
    elif sun_sign == 8:  # Sagittarius
        sun_lines += [
            "Respected by authority; scholarly, devout and strong; adept with weapons; medical knowledge; worthy of reverence.",
        ]
    elif sun_sign == 9:  # Capricorn
        sun_lines += [
            "Covetous wanderer; poor comforts; opposes his own; clever yet indulges in unworthy acts; enjoys others’ wealth.",
        ]
    elif sun_sign == 10:  # Aquarius
        sun_lines += [
            "Sparse comforts/children; strong-limbed; base indulgences; rigid views; unstable friendships; prone to cardiac strain.",
        ]
    elif sun_sign == 11:  # Pisces
        sun_lines += [
            "Well-liked and learned; crushes enemies; earnings via water-products or land/irrigation; many brothers; hidden ailment possible.",
        ]
    sun_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Sun</h3>"
        f"<p class='text-center mb-1'><em>Sun is in {SIGN_NAMES[sun_sign]}.</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in sun_lines)
        + _md_line(const._SUN, "Sun") + _weak_note_line("Sun", const._SUN)
        + "</div>"
    )

    # ◼ MOON – sign based reading
    moon_sign = _planet_sign(const._MOON)
    moon_deg_in_sign = (_get_lon(const._MOON) % 30.0)
    moon_lines = []
    if moon_sign == 0:
        moon_lines += [
            "Quick to anger yet wealthy; may lack siblings; sons indicated; courageous wanderer; lustful; pleases women; honoured by leaders; avoids deep waters; knee weakness; round pretty eyes; scar on head; scant body hair.",
        ]
    elif moon_sign == 1:
        moon_lines += [
            "Charitable and sensuous; honoured and pleasure-loving; very brave and strong; many daughters; forgiving and steady in friendship; yet finances/family/progeny can suffer.",
        ]
        # condition: first half vs second half of Taurus
        if moon_deg_in_sign < 15:
            moon_lines.append("With Moon in the first half of Taurus: adverse for the mother.")
        else:
            moon_lines.append("With Moon in the second half of Taurus: adverse for the father.")
    elif moon_sign == 2:
        moon_lines += [
            "Poetic, skilful lover; handsome and very intelligent; jovial; scripture-learned; can read hidden thoughts; sweet-tongued.",
        ]
    elif moon_sign == 3:
        moon_lines += [
            "Wealth fluctuates; astrological bent; fast walker; owns homes/land; fortunate with friends; sensuous; fond of water sports/orchards.",
        ]
    elif moon_sign == 4:
        moon_lines += [
            "Mountain/forest affinity; broad features; energetic; women-averse; hunger/thirst and abdominal/dental issues; enjoys meat; charitable and aggressive; few sons; dutiful to parents.",
        ]
    elif moon_sign == 5:
        moon_lines += [
            "Attractive and very learned; teacher-type; religious; sweet and truthful; composed and helpful; many daughters, few sons; fond of arts; enjoys others’ wealth; foreign residence likely.",
        ]
    elif moon_sign == 6:
        moon_lines += [
            "Prominent nose/eyes, slim; hen-pecked; devout and ethical; skilled trader; avoids coveting; fortune fluctuates; some limb defect/illness; helpful to relatives yet abandoned by them.",
        ]
    elif moon_sign == 7:
        moon_lines += [
            "Early sickness; strong body later; covetous and atheistic strain; pretty eyes; wealthy; drawn to others’ wives; cruel-hearted; cut off from relatives; losses via rulers; prominent abdomen/forehead; secret sins.",
        ]
    elif moon_sign == 8:
        moon_lines += [
            "Sāttvic nature; wealthy and haughty; multi-talented; inherits property; charitable and strong; eloquent; devout yet opposes own kin; yields only to love and kindness.",
        ]
    elif moon_sign == 9:
        moon_lines += [
            "Musical and learned; subdued by women; charitable and forgiving; pleases spouse; religious; wandering and lazy; hates cold; fine eyes/skin; tall and handsome.",
        ]
    elif moon_sign == 10:
        moon_lines += [
            "Clever yet indolent; attached to others’ wives; sinful; sculptor; liked by friends; ill-natured and poor; enjoys others’ wealth.",
        ]
    elif moon_sign == 11:
        moon_lines += [
            "Highly talented; earns via sea-products; devoted to family; sculptor; defeats opponents; easily yields to women; kind and charitable; beautiful, well-proportioned body.",
        ]
    moon_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Moon</h3>"
        f"<p class='text-center mb-1'><em>Moon is in {SIGN_NAMES[moon_sign]}.</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in moon_lines)
        + _md_line(const._MOON, "Moon") + _weak_note_line("Moon", const._MOON)
        + "</div>"
    )

    # ◼ MARS – sign based reading
    mars_sign = _planet_sign(const._MARS)
    mars_lines = []
    if mars_sign == 0:
        mars_lines += [
            "Truth-teller, bold and battle-ready; fame and wealth; good speech; liked by all; gains in cattle/agriculture; quick-tempered; many relationships.",
        ]
    elif mars_sign == 1:
        mars_lines += [
            "Many enemies and few comforts; foul-tongued; sinful; can sing; tendency to spoil virtuous women.",
        ]
    elif mars_sign == 2:
        mars_lines += [
            "Large family; handsome; multi-disciplinary; poet/sculptor; religious bent; foreign travel indicated.",
        ]
    elif mars_sign == 3:
        mars_lines += [
            "Eats/lives at others’ place; sickly and miserable; earns via land/water pursuits.",
        ]
    elif mars_sign == 4:
        mars_lines += [
            "Valorous yet poor; forest-going and strenuous work; intolerant; hunter’s streak; irreligious tendencies; risk to first marriage.",
        ]
    elif mars_sign == 5:
        mars_lines += [
            "Wealthy with big family; sweet-tongued and learned; spend-thrift; religious; fearful of enemies.",
        ]
    elif mars_sign == 6:
        mars_lines += [
            "Itinerant speaker; good looks; affectionate to spouse/preceptors/friends; risk to first marriage.",
        ]
    elif mars_sign == 7:
        mars_lines += [
            "Conqueror and gang-leader type; truthful; harms foes; risk of injury by poison/fire/weapon.",
        ]
    elif mars_sign == 8:
        mars_lines += [
            "High rank; weapon injuries weaken; bitter speech; hard labour; disregards elders/preceptors.",
        ]
    elif mars_sign == 9:
        mars_lines += [
            "Army leader / kingly; brave in battle; earns by own effort; stays in homeland.",
        ]
    elif mars_sign == 10:
        mars_lines += [
            "Sickly; clashes with own people; arrogant; lying and jealous; unfortunate; hairy body.",
        ]
    elif mars_sign == 11:
        mars_lines += [
            "Humiliated by own people; disrespectful to priests; sickly and wicked; abroad; enjoys praise.",
        ]
    mars_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Mars</h3>"
        f"<p class='text-center mb-1'><em>Mars is in {SIGN_NAMES[mars_sign]}.</em></p>"
        + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in mars_lines)
        + _md_line(const._MARS, "Mars") + _weak_note_line("Mars", const._MARS)
        + "</div>"
    )
    
        # ── Rashi-location readings (Mercury, Jupiter, Venus, Saturn, Rahu, Ketu) ──
    def _build_rashi_block(pid: int, name: str, sign_idx: int, mapping: dict[int, list[str]]):
        header = f"{name} is in {SIGN_NAMES[sign_idx]}."
        lines = list(mapping.get(sign_idx, []))

        # Special classical note: Jupiter in Aquarius ~ Cancer (per Varahamihira)
        if name == "Jupiter" and sign_idx == 10:
            lines.append("Classical note: some authorities treat Jupiter in Aquarius as giving results similar to Cancer.")

        # Compose base HTML
        html = (
            f"<div class='mt-4'><h3 class='h6 text-center'>Rashi reading — {name}</h3>"
            f"<p class='text-center mb-1'><em>{header}</em></p>"
            + "".join(f"<p class='text-center mb-1'>• {txt}</p>" for txt in lines)
            + "</div>"
        )

        # Mahadasha line
        md = _md_period_for(pid)
        md_note = ""
        if md:
            _s, _e = md
            md_note = (
                f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {name}:</strong> "
                f"{_s:%Y-%m-%d} – {_e:%Y-%m-%d}</p>"
            )

        # Weak-note (avasthas or sub-threshold shadbala)
        weak_note = ""
        sb_val = _extract_shadbala_val(sb_res, pid)
        under_thresh = (pid in SHAD_THRESH) and (sb_val is not None) and (sb_val < SHAD_THRESH[pid])
        is_weak = (pid in avs["bala"]) or (pid in avs["mrita"]) or (pid in avs["sushupti"]) or under_thresh
        if is_weak:
            weak_note = (
                f"<p class='text-center mt-2'><strong>Note:</strong> "
                f"The above predictions may not manifest very strongly, since the {name} is weak</p>"
            )

        return html.replace("</div>", f"{md_note}{weak_note}</div>")

    # Sign → bullet lines (reworded, faithful to source; not sugar-coated)
    mercury_sign_readings = {
        0: ["Sharp but contentious; cunning; restless; prone to lies; enjoys performance arts; highly sensual; spends and falls into debt or confinement."],
        1: ["Wealth-attracting; dependable; charitable; multi-skilled; witty and musical; sensual yet respected."],
        2: ["Well-dressed and wealthy; strong orator; proud; often cool toward sex; ‘two-mother’ upbringing motifs; versed in scripture; generally comfortable."],
        3: ["Scholarly; may live abroad; talkative to a fault; drawn to pretty partners; clashes with friends/relatives; artistic; gains via water-related work."],
        4: ["Roving and famous; learning suffers; poor memory; wealth/property strained; disliked by women; servile obligations weigh in."],
        5: ["Religious intellect; learned poet/speaker/writer; honored; fearless; argumentative yet forgiving."],
        6: ["Silvery tongue; skilled in many arts; devout; trader’s mind; spends readily; sensual indulgence present."],
        7: ["Industrious yet irreligious; shameless and greedy; consorts with questionable partners; deceitful; covets others’ assets."],
        8: ["Scriptural mastery; forgiving; renowned; teacher/preceptor type; brave and wealthy; persuasive writer; associates with worthy women."],
        9: ["Servile and unstable; foolish back-biter; shunned by kin; hyper-fickle and lust-driven; cowardly posturing."],
        10:["Harried by opponents; dereliction of duty; unclean; uncultured; defective speech; servility and timidity indicated."],
        11:["Good-natured and pious; capable; helpful; wins friends’ affection yet may remain materially limited; distant-lands themes recur."],
    }

    jupiter_sign_readings = {
        0: ["Pious yet argumentative; jeweled/ornamented; wealthy and famed; generous spender; opposed by many; scars from injuries; harsh streak appears."],
        1: ["Corpulent and healthy; devoted to gods/priests/cows; fortunate; devoted to spouse; land and cattle; wise and benevolent."],
        2: ["Ministerial; friends and sons support; attractive with fine eyes; eloquent; religious leanings."],
        3: ["Wealthy and learned; strong and truthful; adored; king-like stature."],
        4: ["Strong, learned, wealthy; pious commander/leader; aggressive edge; linked to forts/forests/mountains."],
        5: ["Learned, pious, efficient; enjoys scents/flowers; crushes opponents; widely versed."],
        6: ["Wise and soft-spoken; foreign earnings; scriptural erudition; attractive; trade-oriented."],
        7: ["Scriptural commentator; clever; keeps worthy company; sickly and toilsome; quick-tempered; dips into forbidden pursuits."],
        8: ["Religious teacher; very wealthy; charitable; high rank; pilgrimages and foreign circuits."],
        9: ["Servile and over-worked; pleasures denied; weak; irreligious and fearful; distant-lands motif."],
        10:["Sickly and greedy; loses money; poor judgment; liable to abdominal/dental disease; (some texts say Cancer-like results here)."],
        11:["Vedic scholar; adorable and famous; distinctly hairy body."],
    }

    venus_sign_readings = {
        0: ["Leads troops/teams; chases others’ partners; legal trouble via women; longs to go abroad; unreliable; provocative with authority; night-blindness risks."],
        1: ["Many women and children; agriculture/cattle; fond of scents/flowers; free of enemies; attractive."],
        2: ["Scripture-versed; very famous; beautiful body; writer/poet; friendly; income via song/dance; devout and sensual."],
        3: ["Good deeds and learning; strong and religious; obtains objects of desire; two marriages possible; sickness from liquor/women."],
        4: ["Money via women; fewer children; servile to women; destroys enemies; devoted to teachers/priests; generally comfortable and wealthy."],
        5: ["Very rich; persuasive with women; pilgrimages; learning present but material comforts lacking."],
        6: ["Earns by effort; loves garlands and fine clothes; foreign journeys; religiously inclined; wavers under pressure."],
        7: ["Quarrelsome and notorious; irreligious; excessive talk; shunned by brothers; violent skills; poverty; genitourinary disease."],
        8: ["Virtuous and liked; wealthy; high-ranking; large and heavyset; honored."],
        9: ["Over-sensual and older partners; spendthrift; lean and transgressive; heart disease/impotence risk; covets others’ wealth."],
        10:["Addicted to others’ spouses; irreligious; clashes with mentors/children; ugly, ill-clad and anxious."],
        11:["Very wealthy; subdues opponents; famous; charitable; royal favor; loves swimming; gentle speech; learned."],
    }

    saturn_sign_readings = {
        0: ["Weak constitution; worn by labor and excess; ill-tempered; deceitful; estranged from kin; unclean and disliked; sinful reputation."],
        1: ["Poor and servile; consorts with older women; wicked associations; yields to others’ spouses; versatile; violates social norms in mate choice."],
        2: ["Hounded by debt, prison, toil; deceitful; lust-inclined; lazy and wicked."],
        3: ["Frail in childhood; mother-loss themes; poor yet learned; famous; opposes relatives; health issues linger."],
        4: ["Skilled writer; quarrelsome; socially non-conforming; miserable and servile; bereft of wife and friends; taboo pursuits; quick-to-anger."],
        5: ["Wicked and unsteady; fails repeatedly; effeminate tendencies; chases easy-morals women; sculptor/artisan bent; paradoxically helpful; has wealth and progeny."],
        6: ["Regal bearing; sexually indulgent; eloquent; honored publicly; wanderer; linked with courtesans/dancers."],
        7: ["Burns by fire/weapon/poison; temper and conceit; grabs others’ assets; taboo acts; insincere; losses and illness."],
        8: ["Broad fame and contentment; steady income; many disciplines; good children; concise speech; honored widely."],
        9: ["Ruler-aligned; controls others’ women and wealth; learned; artisan; admired and famous; foreign travel; courageous."],
        10:["Very rich yet deceitful; drinks heavily; addicted to others’ wives; wicked and fickle; irreligious."],
        11:["Respected, helpful and wealthy; religious pursuits; mild and cool temperament; knowledge of gems."],
    }

    # Build the 4 graha rashi blocks
    rashi_mercury_html = _build_rashi_block(const._MERCURY, "Mercury", _planet_sign(const._MERCURY), mercury_sign_readings)
    rashi_jupiter_html = _build_rashi_block(const._JUPITER, "Jupiter", _planet_sign(const._JUPITER), jupiter_sign_readings)
    rashi_venus_html   = _build_rashi_block(const._VENUS,   "Venus",   _planet_sign(const._VENUS),   venus_sign_readings)
    rashi_saturn_html  = _build_rashi_block(const._SATURN,  "Saturn",  _planet_sign(const._SATURN),  saturn_sign_readings)

    # ── Nodes: conditional “favourable-sign” notes + benefic association/aspect ──
    def _assoc_or_aspected_by_jupiter_or_mercury(target_house_idx: int) -> bool:
        touched = False
        for p, deltas in ((const._JUPITER, {4, 6, 8}), (const._MERCURY, {6})):  # Jup: 5/7/9; Merc: 7th
            p_house = p2h.get(p)
            if p_house is None:
                continue
            if p_house == target_house_idx:
                touched = True
            else:
                delta = (target_house_idx - p_house) % 12
                if delta in deltas:
                    touched = True
        return touched

    # RAHU
    rashi_rahu_html = ""
    if hasattr(const, "_RAHU"):
        rahu_pid = const._RAHU
        rahu_sign = _planet_sign(rahu_pid)
        rahu_house = p2h.get(rahu_pid)
        favourable_rahu = {3, 5, 8, 7}   # Cancer, Virgo, Sagittarius, Scorpio
        exalt_rahu = 1                   # Taurus
        owns_rahu = {10}                 # Aquarius (some say Virgo too)
        mool_rahu = 2                    # Gemini

        lines_r = []
        header_r = f"Rahu is in {SIGN_NAMES[rahu_sign]}."
        # Favourable-sign condition
        if rahu_sign in favourable_rahu:
            lines_r.append(
                "In this sign, classics credit Rahu with benefits: growth in wealth, help from friends/authorities, comforts, religious leanings, new home/clothes, and honoured foreign travels."
            )
        # Specific sign dignities (only if actually met)
        if rahu_sign == exalt_rahu:
            lines_r.append("Exaltation sign for Rahu — power and prominence are amplified.")
        if rahu_sign in owns_rahu:
            lines_r.append("Own-sign placement for Rahu.")
        if rahu_sign == mool_rahu:
            lines_r.append("Moolatrikona sign for Rahu — functional strength in worldly dealings.")
        # Benefic association/aspect (Jupiter or Mercury) condition
        if rahu_house is not None and _assoc_or_aspected_by_jupiter_or_mercury(rahu_house):
            lines_r.append("With Jupiter/Mercury association or aspect: benefic outcomes are enhanced.")

        if lines_r:
            rashi_rahu_html = (
                f"<div class='mt-4'><h3 class='h6 text-center'>Rashi reading — Rahu</h3>"
                f"<p class='text-center mb-1'><em>{header_r}</em></p>"
                + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines_r)
                + "</div>"
            )
            # MD line
            mdR = _md_period_for(rahu_pid)
            if mdR:
                _sr, _er = mdR
                rashi_rahu_html = rashi_rahu_html.replace("</div>",
                    f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of Rahu:</strong> {_sr:%Y-%m-%d} – {_er:%Y-%m-%d}</p></div>"
                )
            # Weak note (only avasthas apply; shadbala thresholds are not classically defined for nodes)
            if (rahu_pid in avs["bala"]) or (rahu_pid in avs["mrita"]) or (rahu_pid in avs["sushupti"]):
                rashi_rahu_html = rashi_rahu_html.replace("</div>",
                    "<p class='text-center mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the Rahu is weak</p></div>"
                )

    # KETU
    rashi_ketu_html = ""
    if hasattr(const, "_KETU"):
        ketu_pid = const._KETU
        ketu_sign = _planet_sign(ketu_pid)
        ketu_house = p2h.get(ketu_pid)
        exalt_ketu = 7                   # Scorpio
        owns_ketu = {7, 11}             # Scorpio (or Pisces)
        mool_ketu = 8                   # Sagittarius

        lines_k = []
        header_k = f"Ketu is in {SIGN_NAMES[ketu_sign]}."
        # “In these signs Ketu gives favourable results”
        if (ketu_sign == exalt_ketu) or (ketu_sign in owns_ketu) or (ketu_sign == mool_ketu):
            lines_k.append("In this sign, classics say Ketu tends to give favourable outcomes.")
        # Benefic association/aspect (Jupiter or Mercury) condition
        if ketu_house is not None and _assoc_or_aspected_by_jupiter_or_mercury(ketu_house):
            lines_k.append("With Jupiter/Mercury association or aspect: benefic results are strengthened.")

        if lines_k:
            rashi_ketu_html = (
                f"<div class='mt-4'><h3 class='h6 text-center'>Rashi reading — Ketu</h3>"
                f"<p class='text-center mb-1'><em>{header_k}</em></p>"
                + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines_k)
                + "</div>"
            )
            # MD line
            mdK = _md_period_for(ketu_pid)
            if mdK:
                _sk, _ek = mdK
                rashi_ketu_html = rashi_ketu_html.replace("</div>",
                    f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of Ketu:</strong> {_sk:%Y-%m-%d} – {_ek:%Y-%m-%d}</p></div>"
                )
            # Weak note (only avasthas apply; shadbala thresholds are not classically defined for nodes)
            if (ketu_pid in avs["bala"]) or (ketu_pid in avs["mrita"]) or (ketu_pid in avs["sushupti"]):
                rashi_ketu_html = rashi_ketu_html.replace("</div>",
                    "<p class='text-center mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the Ketu is weak</p></div>"
                )
    
        # ── Aspects on the Sun (conditioned by Sun's sign) ─────────────────────
    # Uses classical aspects: all grahas cast 7th; Mars adds 4th & 8th; Jupiter 5th & 9th;
    # Saturn 3rd & 10th. (Rahu/Ketu not used here.) Mercury/Venus cannot practically
    # oppose the Sun in a rāśi chart, so they will rarely (effectively never) match.
    def _house_of(pid: int) -> int | None:
        hi = p2h.get(pid)
        if hi is not None:
            return hi
        # fallback from sign when PyJHora didn't fill p2h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    def _does_aspect_from(planet_pid: int, target_house_idx: int) -> bool:
        """Return True if planet casts an aspect on *target_house_idx* (0-based)."""
        src = _house_of(planet_pid)
        if src is None:
            return False
        delta = (target_house_idx - src) % 12
        aspects = {
            const._MOON:    {6},
            const._MARS:    {3, 6, 7},   # 4th, 7th, 8th
            const._MERCURY: {6},
            const._JUPITER: {4, 6, 8},   # 5th, 7th, 9th
            const._VENUS:   {6},
            const._SATURN:  {2, 6, 9},   # 3rd, 7th, 10th
        }
        return delta in aspects.get(planet_pid, set())

    # Sun's sign → group for the textual rules
    try:
        sun_sign = int(natal_pp[const._SUN + 1][1][0])
    except Exception:
        try:
            sun_lon = float(natal_pp[const._SUN + 1][1][1]) % 360.0
            sun_sign = int(sun_lon // 30)
        except Exception:
            sun_sign = 0

    grp = None
    grp_label = ""
    if sun_sign in (0, 7):        # Aries, Scorpio
        grp, grp_label = "mars",   "the signs of Mars (Aries/Scorpio)"
    elif sun_sign in (1, 6):      # Taurus, Libra
        grp, grp_label = "venus",  "the signs of Venus (Taurus/Libra)"
    elif sun_sign in (2, 5):      # Gemini, Virgo
        grp, grp_label = "mercury","the signs of Mercury (Gemini/Virgo)"
    elif sun_sign == 3:           # Cancer
        grp, grp_label = "moon",   "the sign of the Moon (Cancer)"
    elif sun_sign == 4:           # Leo
        grp, grp_label = "own",    "his own sign (Leo)"
    elif sun_sign in (8, 11):     # Sagittarius, Pisces
        grp, grp_label = "jupiter","the signs of Jupiter (Sagittarius/Pisces)"
    elif sun_sign in (9, 10):     # Capricorn, Aquarius
        grp, grp_label = "saturn", "the signs of Saturn (Capricorn/Aquarius)"
    else:
        grp, grp_label = "mars",   "the signs of Mars (Aries/Scorpio)"  # safe default

    # Conversational rewording of the classical dicta (per group × aspecting graha)
    P = {
        "mars": {
            const._MOON:    ["Charitable and soft-bodied yet attractive; has help/servants; drawn to sensual company."],
            const._MARS:    ["Very strong; hard-edged, red-eyed intensity; keeps composure in conflict."],
            const._MERCURY: ["Loses nerve and comforts; servile and weakened in resources and presence."],
            const._JUPITER: ["Wealth rises; advisory/judicial stature; generous; respected within the family."],
            const._VENUS:   ["Fixation on disreputable liaisons; opposed by many; few real friends; risk of skin issues and poverty."],
            const._SATURN:  ["Courage sags; sickly; dull/ungainly appearance."],
        },
        "venus": {
            const._MOON:    ["Multiple romantic ties; pulled toward courtesans; earnings link to water-related trades."],
            const._MARS:    ["Composed under fire; strong and bold; gains through own toil."],
            const._MERCURY: ["Talent for music/poetry/writing; pleasing looks."],
            const._JUPITER: ["Many allies and adversaries; minister-grade stature; wealthy and content."],
            const._VENUS:   ["Fine eyes; timid at heart; serves power; well-off."],
            const._SATURN:  ["Slothful; ailing and poor; keeps the company of older women."],
        },
        "mercury": {
            const._MOON:    ["Worn down by both friends and rivals; low spirits; hassles from foreign travel/residence."],
            const._MARS:    ["Enemy-shy and quarrelsome; beaten in contests; humiliation possible."],
            const._MERCURY: ["Regal bearing; famed; supported by friends; few enemies; watch the eyes."],
            const._JUPITER: ["Very learned and mantra-savvy; sharp yet loses inner calm; frequent foreign movement."],
            const._VENUS:   ["Comforted by spouse, children and wealth; good-looking and healthy."],
            const._SATURN:  ["Easily agitated; unwise tricksiness; many servants but muddled judgment."],
        },
        "moon": {
            const._MOON:    ["King-like confidence with a harsh edge; wealth via water-linked pursuits."],
            const._MARS:    ["Inflammation and perianal troubles; few friends; progeny comfort suffers."],
            const._MERCURY: ["Renown for learning and status; favoured by authorities; clever; largely free of foes."],
            const._JUPITER: ["Envoy/diplomat vibe; high office; very famous and multi-talented."],
            const._VENUS:   ["Income routes via women; does tangible good for others; brave and sweet-tongued."],
            const._SATURN:  ["Kapha-Vaata ailments; covets others’ wealth; skewed judgment; back-biting tendencies."],
        },
        "own": {
            const._MOON:    ["Shrewd and persuasive; liked by the powerful; Kapha-type issues may surface."],
            const._MARS:    ["Gallant and quick-witted; many lovers; feared by rivals."],
            const._MERCURY: ["Skilled writer; loves to travel; physical stamina can be middling."],
            const._JUPITER: ["Builds temples/orchards/tanks; strong and wise; enjoys solitude."],
            const._VENUS:   ["Harsh and shameless notes; friction with relatives; skin complaints possible."],
            const._SATURN:  ["Harasses close ones; derails others’ work; risk of impotence."],
        },
        "jupiter": {
            const._MOON:    ["Blessed with children, learning and fame; attractive; king-like contentment."],
            const._MARS:    ["War renown; assertive clarity; wealth and comforts accrue."],
            const._MERCURY: ["Poetic, multilingual, sweet-spoken; knows minerals/metals and the like."],
            const._JUPITER: ["Learned and affluent; moves in royal company."],
            const._VENUS:   ["Gets a virtuous, beautiful spouse; fine clothes and finery."],
            const._SATURN:  ["Unkempt; serves the fallen; covets others’ food; tends cattle."],
        },
        "saturn": {
            const._MOON:    ["Cunning and unsettled; loses wealth/comfort through women."],
            const._MARS:    ["Dogged by illness and adversaries; physical injuries are likely."],
            const._MERCURY: ["Bold yet eunuch-like nature; unclean habits; covets others’ assets."],
            const._JUPITER: ["Wise and well-known; becomes a refuge for many."],
            const._VENUS:   ["Livelihood via conch-shell-type trades; gains through women of ill repute."],
            const._SATURN:  ["Crushes foes; trusted by rulers; content at heart."],
        },
    }

    # Build per-graha sub-sections only if that graha actually aspects the Sun now
    sun_house_idx = _house_of(const._SUN)
    sub_sections = []
    for asp_pid in (const._MOON, const._MARS, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN):
        if not _does_aspect_from(asp_pid, sun_house_idx):
            continue
        lines = P.get(grp, {}).get(asp_pid, [])
        if not lines:
            continue

        # Mahadasha window of the aspecting graha
        md = _md_period_for(asp_pid)
        md_html = ""
        if md:
            s, e = md
            md_html = (
                f"<p class='text-center mt-2'><strong>"
                f"The above effects would be more prominent in the mahadasha of {PLANET_NAMES[asp_pid]}:</strong> "
                f"{s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check (balāvasthā, mṛtāvasthā, suṣupti; and Śaḍbala below threshold)
        sb_val_x = _extract_shadbala_val(sb_res, asp_pid)
        is_weak = (
            (asp_pid in avs["bala"]) or
            (asp_pid in avs["mrita"]) or
            (asp_pid in avs["sushupti"]) or
            (sb_val_x is not None and asp_pid in SHAD_THRESH and sb_val_x < SHAD_THRESH[asp_pid])
        )
        weak_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {PLANET_NAMES[asp_pid]} is weak</p>"
        ) if is_weak else ""

        block = (
            f"<p class='text-center mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects the Sun in {grp_label}:</em></p>"
            + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines)
            + md_html + weak_html
        )
        sub_sections.append(block)

    sun_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on the Sun (by sign of the Sun)</h3>"
        + "".join(sub_sections if sub_sections else [
            "<p class='text-center mb-1'><em>No qualifying planetary aspects to the Sun found for the current rules.</em></p>"
        ])
        + "</div>"
    )
    
        # ── Aspects on the Moon (conditioned by Moon's sign) ────────────────────
    # Classical aspects used: everyone casts 7th; Mars adds 4th & 8th;
    # Jupiter adds 5th & 9th; Saturn adds 3rd & 10th.
    def _house_of_moon_helper(pid: int) -> int | None:
        hi = p2h.get(pid)
        if hi is not None:
            return hi
        # fallback from sign when PyJHora didn't fill p2h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    def _does_aspect_to_moon_from(planet_pid: int, target_house_idx: int) -> bool:
        src = _house_of_moon_helper(planet_pid)
        if src is None:
            return False
        delta = (target_house_idx - src) % 12
        aspects = {
            const._SUN:     {6},          # 7th
            const._MOON:    {6},          # rarely useful (Moon→Moon), will be skipped below
            const._MARS:    {3, 6, 7},    # 4th, 7th, 8th
            const._MERCURY: {6},          # 7th
            const._JUPITER: {4, 6, 8},    # 5th, 7th, 9th
            const._VENUS:   {6},          # 7th
            const._SATURN:  {2, 6, 9},    # 3rd, 7th, 10th
        }
        return delta in aspects.get(planet_pid, set())

    # Moon sign index (0=Aries … 11=Pisces)
    try:
        moon_sign = int(natal_pp[const._MOON + 1][1][0])
    except Exception:
        try:
            moon_lon = float(natal_pp[const._MOON + 1][1][1]) % 360.0
            moon_sign = int(moon_lon // 30)
        except Exception:
            moon_sign = 0

    # Conversational re-wordings of the rules you supplied.
    # Key = sign index 0..11, value = {aspecting_pid: [bullet strings]}
    P_MOON = {
        0: {  # Mesha / Aries
            const._SUN:     ["Quick temper; poverty pressures; begging-like dependence can appear."],
            const._MARS:    ["Dental and eye troubles; risk of injuries; status can rise despite urinary issues."],
            const._MERCURY: ["Educated, articulate and known for speech/poetry—reputation follows."],
            const._JUPITER: ["King-like stature; wealth accrues."],
            const._VENUS:   ["Very agreeable, virtuous, persuasive speaker."],
            const._SATURN:  ["Sickliness; untruthful tendencies; theft/underhanded acts."],
        },
        1: {  # Vrisha / Taurus
            const._SUN:     ["Work tied to land/agriculture; hard labor; servile roles."],
            const._MARS:    ["Over-indulgent sexually; popular with women; good company but loss of property."],
            const._MERCURY: ["Learned, eloquent, highly skilled."],
            const._JUPITER: ["Virtuous, famous, admirable; good spouse and children."],
            const._VENUS:   ["Many comforts; king-level ease."],
            const._SATURN:  ["Wealth comes, yet nature turns harsh; strain on mother’s wellbeing."],
        },
        2: {  # Mithuna / Gemini
            const._SUN:     ["Clever and attractive yet poor; hardships persist."],
            const._MARS:    ["Very brave, learned; in arms trade; some bodily defect possible."],
            const._MERCURY: ["King’s confidant; defeats rivals."],
            const._JUPITER: ["Discerning teacher-type; learned."],
            const._VENUS:   ["Fearless, beautiful spouse; vehicles and ornaments."],
            const._SATURN:  ["Losses of wealth/spouse/vehicle/children; menial weaving-type work."],
        },
        3: {  # Karka / Cancer
            const._SUN:     ["Eye disease; custodian duties (fort/estate); poverty strain."],
            const._MARS:    ["Bold with status, but body weak."],
            const._MERCURY: ["Learned poet; advisory/ministerial role."],
            const._JUPITER: ["Learned, famed and valiant—ruler archetype."],
            const._VENUS:   ["Gems/ornaments; attractive; but liaisons with ill-repute women risk."],
            const._SATURN:  ["Wandering; hostility toward mother; trades in iron/arms."],
        },
        4: {  # Simha / Leo
            const._SUN:     ["Fine qualities, brave, near-royal standing; children delayed/denied."],
            const._MARS:    ["Regal authority; commands forces; sharp temper."],
            const._MERCURY: ["Devoted to spouse; learned; astrologer-leaning."],
            const._JUPITER: ["Wealthy, virtuous and famous."],
            const._VENUS:   ["Scholarship with frailty; devoted to spouse; royal ease."],
            const._SATURN:  ["Agriculture focus; loss of wealth/home comforts; sin-leaning; barber-type work."],
        },
        5: {  # Kanya / Virgo
            const._SUN:     ["Serves women; enjoys varied comforts."],
            const._MARS:    ["Sculptor/fabricator; fame, wealth; battle-ready."],
            const._MERCURY: ["Poet/astrologer; debate winner; king-like recognition."],
            const._JUPITER: ["Favoured by rulers; military leadership; keeps promises."],
            const._VENUS:   ["Many spouses; wealthy; learned and multi-talented."],
            const._SATURN:  ["Loss of wealth and wisdom; dependent on women; weak memory."],
        },
        6: {  # Tula / Libra
            const._SUN:     ["Wandering, sickness, poverty, humiliation; comforts lacking."],
            const._MARS:    ["Harsh temper; adultery risk; violent; eye disease."],
            const._MERCURY: ["Multi-talented; very wealthy; learned; eloquent."],
            const._JUPITER: ["Highly respected; trades in gold/precious stones."],
            const._VENUS:   ["Healthy, attractive, wealthy; learned; success in trade."],
            const._SATURN:  ["Harsh nature; wealthy but indulgent in sensuality."],
        },
        7: {  # Vrischika / Scorpio
            const._SUN:     ["Learned yet wandering; deprived of wealth/comforts; disliked socially."],
            const._MARS:    ["Famed; war-victor; royal bearing; voracious eater."],
            const._MERCURY: ["Abrasive speech; twins fathered; capable in craft."],
            const._JUPITER: ["Norm-abiding; pleasing appearance."],
            const._VENUS:   ["Wealthy and pleasant; spots others’ weaknesses; washerman-type associations."],
            const._SATURN:  ["Sickly; bodily defect; avaricious."],
        },
        8: {  # Dhanu / Sagittarius
            const._SUN:     ["Wealthy, famous, king-like."],
            const._MARS:    ["Army leader; wealthy, valorous, renowned."],
            const._MERCURY: ["Sculptor/astrologer; protector of kin."],
            const._JUPITER: ["Handsome, devout; ministerial high status; wealthy."],
            const._VENUS:   ["Good-looking; many comforts; loyal friends and spouse; gives refuge."],
            const._SATURN:  ["Fine speaker; strong; philosophical bent; proud; courtesan-attachments."],
        },
        9: {  # Makara / Capricorn
            const._SUN:     ["Poor wanderer with plain looks; helpful to others nonetheless."],
            const._MARS:    ["Famed, king-comparable; wealthy and fortunate."],
            const._MERCURY: ["King-like status; estranged from spouse/children."],
            const._JUPITER: ["Very valorous; ruler-type; many wives, children, friends."],
            const._VENUS:   ["Learned; enjoys others’ wealth and women."],
            const._SATURN:  ["Indolent and plain; rich; attraction to others’ spouses."],
        },
        10: {  # Kumbha / Aquarius
            const._SUN:     ["Unpleasant looks; immoral tones; farming focus."],
            const._MARS:    ["Honest but lazy; servile; harsh character."],
            const._MERCURY: ["Comforts flow; fine speaker; king-like favour."],
            const._JUPITER: ["Royal equivalence; status and possessions abound."],
            const._VENUS:   ["Attracted to others’ wives; little sensual comfort; sinful cowardice risk."],
            const._SATURN:  ["Drawn to others’ wives; irreligious—benefic aspects can flip to fame/prosperity."],
        },
        11: {  # Meena / Pisces
            const._SUN:     ["Highly sensuous, wealthy, leads forces; sin-leaning."],
            const._MARS:    ["Harsh deeds; humiliation; lacks comforts."],
            const._MERCURY: ["Very witty; wealthy and famed; consorts with others’ wives."],
            const._JUPITER: ["Attractive, very wealthy; many women; king-level."],
            const._VENUS:   ["Learned and pleasant; immersed in music/dance/singing."],
            const._SATURN:  ["Tormented by lust; drawn to low/ugly women; sinful track."],
        },
    }

    moon_house_idx = _house_of_moon_helper(const._MOON)
    sub_sections_moon = []
    # We exclude Moon→Moon self-aspect text; build only for Sun, Mars, Mercury, Jupiter, Venus, Saturn
    for asp_pid in (const._SUN, const._MARS, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN):
        if not _does_aspect_to_moon_from(asp_pid, moon_house_idx):
            continue
        lines = P_MOON.get(moon_sign, {}).get(asp_pid, [])
        if not lines:
            continue

        # Mahadasha window of the aspecting graha
        md = _md_period_for(asp_pid)
        md_html = ""
        if md:
            s, e = md
            md_html = (
                f"<p class='text-center mt-2'><strong>"
                f"The above effects would be more prominent in the mahadasha of {PLANET_NAMES[asp_pid]}:</strong> "
                f"{s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check for the aspecting graha
        sb_val_x = _extract_shadbala_val(sb_res, asp_pid)
        is_weak = (
            (asp_pid in avs['bala']) or
            (asp_pid in avs['mrita']) or
            (asp_pid in avs['sushupti']) or
            (sb_val_x is not None and asp_pid in SHAD_THRESH and sb_val_x < SHAD_THRESH[asp_pid])
        )
        weak_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {PLANET_NAMES[asp_pid]} is weak</p>"
        ) if is_weak else ""

        block = (
            f"<p class='text-center mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects the Moon in "
            f"{SIGN_NAMES[moon_sign]}:</em></p>"
            + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines)
            + md_html + weak_html
        )
        sub_sections_moon.append(block)

    moon_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on the Moon (by sign of the Moon)</h3>"
        + "".join(sub_sections_moon if sub_sections_moon else [
            "<p class='text-center mb-1'><em>No qualifying planetary aspects to the Moon found for the current rules.</em></p>"
        ])
        + "</div>"
    )

    # ← Also remember to include `moon_aspects_html` in your final HTML aggregation
    # e.g., sections.append(moon_aspects_html) or concatenation with your other blocks.
    
        # ── Aspects on Mars (conditioned by Mars' sign) ─────────────────────────
    # Classical aspects used: everyone casts 7th; Mars adds 4th & 8th;
    # Jupiter adds 5th & 9th; Saturn adds 3rd & 10th.
    def _house_of_planet(pid: int) -> int | None:
        hi = p2h.get(pid)
        if hi is not None:
            return hi
        # fallback from sign when PyJHora didn't fill p2h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    def _does_aspect_to_mars_from(planet_pid: int, target_house_idx: int) -> bool:
        src = _house_of_planet(planet_pid)
        if src is None:
            return False
        delta = (target_house_idx - src) % 12
        aspects = {
            const._SUN:     {6},          # 7th
            const._MOON:    {6},          # 7th
            const._MARS:    {3, 6, 7},    # 4th, 7th, 8th
            const._MERCURY: {6},          # 7th
            const._JUPITER: {4, 6, 8},    # 5th, 7th, 9th
            const._VENUS:   {6},          # 7th
            const._SATURN:  {2, 6, 9},    # 3rd, 7th, 10th
        }
        return delta in aspects.get(planet_pid, set())

    # Mars sign & house
    try:
        mars_sign = int(natal_pp[const._MARS + 1][1][0])
    except Exception:
        try:
            mars_lon = float(natal_pp[const._MARS + 1][1][1]) % 360.0
            mars_sign = int(mars_lon // 30)
        except Exception:
            mars_sign = 0
    mars_house_idx = _house_of_planet(const._MARS)

    # Map Mars' sign to its "host group" per your rules
    # own: Aries, Scorpio; Venus: Taurus, Libra; Mercury: Gemini, Virgo;
    # Moon: Cancer; Sun: Leo; Jupiter: Sagittarius, Pisces; Saturn: Capricorn, Aquarius
    if mars_sign in (0, 7):
        m_group = "own"
    elif mars_sign in (1, 6):
        m_group = "venus"
    elif mars_sign in (2, 5):
        m_group = "mercury"
    elif mars_sign == 3:
        m_group = "moon"
    elif mars_sign == 4:
        m_group = "sun"
    elif mars_sign in (8, 11):
        m_group = "jupiter"
    elif mars_sign in (9, 10):
        m_group = "saturn"
    else:
        m_group = "own"

    # Conversational re-wordings of your text, grouped by Mars-sign host and aspecting planet
    P_MARS = {
        "own": {  # Mars in Aries/Scorpio
            const._SUN:    ["Ministerial/judicial streak; persuasive speaker; money, spouse and sons indicated."],
            const._MOON:   ["Brave; attraction to others’ partners; injury risks; strain with mother."],
            const._MERCURY:["Sensual; drawn to women of easy morals; covets others’ wealth."],
            const._JUPITER:["Learned, sweet-tongued, devoted to father; wealth accrues."],
            const._VENUS:  ["Voracious appetite; suffers due to women."],
            const._SATURN: ["Attracted to others’ wives; shunned by own kin; weak constitution."],
        },
        "venus": {  # Mars in Taurus/Libra
            const._SUN:    ["Wanders in forests/hills; quick to anger; antipathy toward women."],
            const._MOON:   ["Opposed to mother; timid; drawn to multiple women."],
            const._MERCURY:["Learned, talkative, quarrelsome; pleasant looks."],
            const._JUPITER:["Fortunate; drawn to music and dance."],
            const._VENUS:  ["Worthy of praise; minister/commander potential; many comforts."],
            const._SATURN: ["Famous, amiable, wealthy and learned."],
        },
        "mercury": {  # Mars in Gemini/Virgo
            const._SUN:    ["Learned, wealthy and valorous; life around forts/forests/mountains."],
            const._MOON:   ["Leads women; agreeable, wise, wealthy; takes on royal/security roles."],
            const._MERCURY:["Talks a lot; loves poetry; maths talent; charming fibs."],
            const._JUPITER:["Envoy/sovereign vibes; highly skilful; leads men; may quit homeland."],
            const._VENUS:  ["Wealth; fine food and attire; devoted to spouse."],
            const._SATURN: ["Turns to agriculture; lazy yet brave; rough looks."],
        },
        "moon": {  # Mars in Cancer
            const._SUN:    ["Pitta aggravation; judge-like; dispenses punishment."],
            const._MOON:   ["Sickly; low character; plain looks."],
            const._MERCURY:["Unattractive, shameless; sinful; friendless."],
            const._JUPITER:["Fame, learning and high office."],
            const._VENUS:  ["Tormented through women; humiliation; loses money in unworthy pursuits."],
            const._SATURN: ["Sea-trade/maritime earnings; good looks; near-royal standing."],
        },
        "sun": {  # Mars in Leo
            const._SUN:    ["Woods/mountains wanderer; forceful; protects his own."],
            const._MOON:   ["Hardy body; harsh heart; stress around mother; skilful and bright."],
            const._MERCURY:["Sculptor/painter; poet; greedy yet exceptionally clever."],
            const._JUPITER:["Army leadership; royal favour; learned; fulfils others’ wishes."],
            const._VENUS:  ["Attractive; many liaisons; famous and youthful."],
            const._SATURN: ["Prematurely aged look; poverty worries; lives in others’ homes."],
        },
        "jupiter": {  # Mars in Sagittarius/Pisces
            const._SUN:    ["Adored by people; dwells in wild/fortified places; harsh edge."],
            const._MOON:   ["Quarrelsome scholar; opposes authority."],
            const._MERCURY:["Very clever; learned; sculptor; agreeable."],
            const._JUPITER:["Leaves homeland; without wife/comforts; perpetually battling foes."],
            const._VENUS:  ["Addicted to women; many comforts."],
            const._SATURN: ["Servile, ever wandering; poor looks; sinful tendencies."],
        },
        "saturn": {  # Mars in Capricorn/Aquarius
            const._SUN:    ["Aggressive and brave; wealth, spouse and progeny promised."],
            const._MOON:   ["Strained with mother; fickle friendships; displacement from residence."],
            const._MERCURY:["Very sweet-tongued yet poor/weak; deceitful and irreligious."],
            const._JUPITER:["Long-lived, handsome; enjoys royal favour; blessed with brothers."],
            const._VENUS:  ["Quarrelsome; hen-pecked; still enjoys abundant comforts."],
            const._SATURN: ["Very wealthy; aversion to women; many children; learned; king-like and battle-valorous."],
        },
    }

    sub_sections_mars = []
    for asp_pid in (const._SUN, const._MOON, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN):
        if not _does_aspect_to_mars_from(asp_pid, mars_house_idx):
            continue
        lines = P_MARS.get(m_group, {}).get(asp_pid, [])
        if not lines:
            continue

        # Mahadasha window of the *aspecting* planet
        md = _md_period_for(asp_pid)
        md_html = ""
        if md:
            s, e = md
            md_html = (
                f"<p class='text-center mt-2'><strong>"
                f"The above effects would be more prominent in the mahadasha of {PLANET_NAMES[asp_pid]}:</strong> "
                f"{s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check for the *aspecting* planet
        sb_val_x = _extract_shadbala_val(sb_res, asp_pid)
        is_weak = (
            (asp_pid in avs['bala']) or
            (asp_pid in avs['mrita']) or
            (asp_pid in avs['sushupti']) or
            (sb_val_x is not None and asp_pid in SHAD_THRESH and sb_val_x < SHAD_THRESH[asp_pid])
        )
        weak_html = (
            f"<p class='text-center mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {PLANET_NAMES[asp_pid]} is weak</p>"
        ) if is_weak else ""

        block = (
            f"<p class='text-center mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects Mars in "
            f"{SIGN_NAMES[mars_sign]}:</em></p>"
            + "".join(f"<p class='text-center mb-1'>• {t}</p>" for t in lines)
            + md_html + weak_html
        )
        sub_sections_mars.append(block)

    mars_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Mars (by sign of Mars)</h3>"
        + "".join(sub_sections_mars if sub_sections_mars else [
            "<p class='text-center mb-1'><em>No qualifying planetary aspects to Mars found for the current rules.</em></p>"
        ])
        + "</div>"
    )

    # ← Remember to include `mars_aspects_html` in your final HTML aggregation:
    # e.g., sections.append(mars_aspects_html)  or  final_html = final_html + mars_aspects_html
    
        # ── Aspects on Mercury in Different Signs (only when the aspect actually exists) ──
    # Helpers consistent with the Sun/Moon/Mars aspect sections
    def _house_of(pid: int) -> int:
        h = p2h.get(pid)
        if h is not None:
            return h
        # fallback from sign
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    # classical aspects (offsets from the aspector’s house, 0-based)
    ASPECTS = {
        const._SUN: {6}, const._MOON: {6}, const._MERCURY: {6}, const._VENUS: {6},
        const._MARS: {3, 6, 8}, const._JUPITER: {4, 6, 8}, const._SATURN: {2, 6, 9},
    }
    def _does_aspect(from_pid: int, target_house_idx: int) -> bool:
        src = _house_of(from_pid)
        delta = (target_house_idx - src) % 12
        return delta in ASPECTS.get(from_pid, {6})

    # Locate Mercury’s sign & house and identify the hosting-sign family
    merc_sign = _planet_sign(const._MERCURY)
    merc_house_idx = _house_of(const._MERCURY)
    # sign families
    _host = (
        "mars"    if merc_sign in (0, 7) else           # Aries, Scorpio
        "venus"   if merc_sign in (1, 6) else           # Taurus, Libra
        "own"     if merc_sign in (2, 5) else           # Gemini, Virgo
        "moon"    if merc_sign == 3 else                # Cancer
        "sun"     if merc_sign == 4 else                # Leo
        "jupiter" if merc_sign in (8, 11) else          # Sagittarius, Pisces
        "saturn"                                  # Capricorn, Aquarius
    )

    host_label = {
        "mars":    "a sign of Mars (Aries/Scorpio)",
        "venus":   "a sign of Venus (Taurus/Libra)",
        "own":     "its own sign (Gemini/Virgo)",
        "moon":    "the Moon’s sign (Cancer)",
        "sun":     "the Sun’s sign (Leo)",
        "jupiter": "a sign of Jupiter (Sagittarius/Pisces)",
        "saturn":  "a sign of Saturn (Capricorn/Aquarius)",
    }[_host]

    # Text payloads (paraphrased faithfully, not sugar-coated)
    P_MERCURY = {
        "mars": {  # Mercury in Aries/Scorpio
            const._SUN: [
                "Straight-speaking; bonds well with brothers; enjoys tangible comforts."
            ],
            const._MOON: [
                "Drawn to dance/music and sensuality; fond of women; morally wayward tendencies possible; access to staff and vehicles."
            ],
            const._MARS: [
                "Prone to falsehood and quarrels; still articulate, learned and very wealthy; may suffer from excessive thirst."
            ],
            const._JUPITER: [
                "Wealth and contentment; pleasing, soft disposition."
            ],
            const._VENUS: [
                "Suave and persuasive; courteous; trusted by others—especially women."
            ],
            const._SATURN: [
                "Harsh streak; courage with suffering—aggressive yet miserable."
            ],
        },
        "venus": {  # Mercury in Taurus/Libra
            const._SUN: [
                "Health is fragile; humiliations and servility; resources feel tight."
            ],
            const._MOON: [
                "Wealth and reputation; reliable; healthy; may serve the establishment."
            ],
            const._MARS: [
                "Knocks from rivals and illness; humbled by authority; pleasures dry up."
            ],
            const._JUPITER: [
                "Learned and trusted; a known face in the community; leadership potential."
            ],
            const._VENUS: [
                "Fortunate; fine clothes/ornaments; youthful attraction—young women are drawn to you."
            ],
            const._SATURN: [
                "Comforts get stripped; strained by spouse/children or friends."
            ],
        },
        "own": {  # Mercury in Gemini/Virgo
            const._SUN: [
                "Truth-inclined; pleasant appearance; favoured by authority."
            ],
            const._MOON: [
                "Scripture-loving and silver-tongued yet a relentless talker; quarrelsome edge."
            ],
            const._MARS: [
                "Well-liked and serviceable to power, but prone to back-biting."
            ],
            const._JUPITER: [
                "High governmental profile; brave; wealthy; presentable."
            ],
            const._VENUS: [
                "Scholar’s polish; works for the ruler; steadfast in friendships; entanglements with wayward women."
            ],
            const._SATURN: [
                "Kind-hearted finisher—sees work through; gains wealth."
            ],
        },
        "moon": {  # Mercury in Cancer
            const._SUN: [
                "Hands-on trader/craftsman vibe—garlands, building, polishing—as livelihood."
            ],
            const._MOON: [
                "Physical drains through women; weak constitution; comforts scarce."
            ],
            const._MARS: [
                "Education stays limited; talks a lot; good-looking; tells agreeable lies; thievish streak."
            ],
            const._JUPITER: [
                "Wise and humane; fortunate; learned; appreciated by the state."
            ],
            const._VENUS: [
                "Attractive like Kāma; sweet-tongued; tuned to dance and music."
            ],
            const._SATURN: [
                "Deceitful and ungrateful patterns; risk of imprisonment."
            ],
        },
        "sun": {  # Mercury in Leo
            const._SUN: [
                "Jealous, servile, harsh and fickle; shameless when pressed."
            ],
            const._MOON: [
                "Well-put-together and capable; loves poetry, dance and music; wealthy and well dressed."
            ],
            const._MARS: [
                "Harmful choices; unwise; bodily injuries likely."
            ],
            const._JUPITER: [
                "Tender constitution but razor-sharp intellect; impressive speaker; high rank."
            ],
            const._VENUS: [
                "Good looks with pleasure-seeking; wealthy."
            ],
            const._SATURN: [
                "Tall frame; afflicted; foul body odour—socially off-putting."
            ],
        },
        "jupiter": {  # Mercury in Sagittarius/Pisces
            const._SUN: [
                "Brave and cool-tempered; liable to kidney/stone/diabetes-type issues."
            ],
            const._MOON: [
                "Writerly; pleasant presence; well-liked; buoyed by friends."
            ],
            const._MARS: [
                "Writer’s skill with an underworld tint—‘leader among thieves’ archetype."
            ],
            const._JUPITER: [
                "Very learned; superb memory; pious; handsome; high station; treasurer-type trust."
            ],
            const._VENUS: [
                "Ministerial flair; youthful; brave; prone to theft."
            ],
            const._SATURN: [
                "Lives in forts/forests; voracious appetite; wicked and incompetent."
            ],
        },
        "saturn": {  # Mercury in Capricorn/Aquarius
            const._SUN: [
                "Big family; rough nature; grapples/wrestles well; voracious appetite; notoriety."
            ],
            const._MOON: [
                "Earnings tied to liquids/water trades; liquor commerce; cowardice."
            ],
            const._MARS: [
                "Low activity; shy but decent-natured; still accrues wealth."
            ],
            const._JUPITER: [
                "Very wealthy; prominent; leads within town or village."
            ],
            const._VENUS: [
                "Many children; looks lacking; highly sensual; wedded to a wayward spouse."
            ],
            const._SATURN: [
                "Sinner’s arc—poor, servile, miserable and destitute."
            ],
        },
    }

    # Build sub-sections only for planets that truly aspect Mercury now
    sub_sections_mercury = []
    for aspector_pid, lines in P_MERCURY[_host].items():
        if not _does_aspect(aspector_pid, merc_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Mercury in {SIGN_NAMES[merc_sign]}:"
        block = [f"<p class='text-center mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-center mb-1'>• {t}</p>" for t in lines]

        # per-aspecting-graha MD window
        mdx = _md_period_for(aspector_pid)
        if mdx:
            s, e = mdx
            block.append(
                f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # per-aspecting-graha weakness note (any of the four conditions)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        if is_weak_aspector:
            block.append(
                f"<p class='text-center'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {PLANET_NAMES.get(aspector_pid)} is weak</p>"
            )

        sub_sections_mercury.append("".join(block))

    if not sub_sections_mercury:
        sub_sections_mercury.append("<p class='text-center mb-1'>No classical aspect on Mercury is exact by canonical aspects right now.</p>")

    mercury_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Mercury (by sign of Mercury)</h3>"
        f"<p class='text-center mb-1'><em>Mercury is in {host_label}.</em></p>"
        + "".join(sub_sections_mercury)
        + "</div>"
    )
    
        # ── Aspects on Jupiter in Different Signs (only when an aspect truly exists) ──
    # Reuse/define local helpers (same pattern you used for Sun/Moon/Mars/Mercury)
    def _house_of(pid: int) -> int:
        h = p2h.get(pid)
        if h is not None:
            return h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    ASPECTS = {
        const._SUN: {6}, const._MOON: {6}, const._MERCURY: {6}, const._VENUS: {6},
        const._MARS: {3, 6, 8}, const._JUPITER: {4, 6, 8}, const._SATURN: {2, 6, 9},
    }
    def _does_aspect(from_pid: int, target_house_idx: int) -> bool:
        src = _house_of(from_pid)
        delta = (target_house_idx - src) % 12
        return delta in ASPECTS.get(from_pid, {6})

    # Jupiter’s sign & house
    jup_sign = _planet_sign(const._JUPITER)
    jup_house_idx = _house_of(const._JUPITER)

    # Host family for Jupiter’s current sign
    _host = (
        "mars"    if jup_sign in (0, 7) else           # Aries, Scorpio
        "venus"   if jup_sign in (1, 6) else           # Taurus, Libra
        "mercury" if jup_sign in (2, 5) else           # Gemini, Virgo
        "moon"    if jup_sign == 3 else                # Cancer
        "sun"     if jup_sign == 4 else                # Leo
        "own"     if jup_sign in (8, 11) else          # Sagittarius, Pisces
        "saturn"                                      # Capricorn, Aquarius
    )
    host_label = {
        "mars":    "a sign of Mars (Aries/Scorpio)",
        "venus":   "a sign of Venus (Taurus/Libra)",
        "mercury": "a sign of Mercury (Gemini/Virgo)",
        "moon":    "the Moon’s sign (Cancer)",
        "sun":     "the Sun’s sign (Leo)",
        "own":     "its own sign (Sagittarius/Pisces)",
        "saturn":  "a sign of Saturn (Capricorn/Aquarius)",
    }[_host]

    # Text payloads for each host-family (faithful paraphrases)
    P_JUPITER = {
        "mars": {  # Jupiter in Aries/Scorpio
            const._SUN: [
                "Deeply pious and truthful; famous; tends to have a hairy body."
            ],
            const._MOON: [
                "Soft-spoken and liked by spouse; religiously inclined; scholarly."
            ],
            const._MARS: [
                "Brave and forceful; crushes opponents’ pride; commands groups."
            ],
            const._MERCURY: [
                "Cheating tendencies; nitpicks others’ faults; outwardly polite; lies."
            ],
            const._VENUS: [
                "Cowardly streak; enjoys finery, women and sensual pleasures."
            ],
            const._SATURN: [
                "Unattractive; greedy; friendships are unstable."
            ],
        },
        "venus": {  # Jupiter in Taurus/Libra
            const._SUN: [
                "A wandering, learned type; serves authority; gains vehicles/cattle."
            ],
            const._MOON: [
                "Very wealthy; attractive; adored by women; indulgent."
            ],
            const._MARS: [
                "Favoured by rulers; liked by women/children; learned and wealthy."
            ],
            const._MERCURY: [
                "Learned, clever, likeable; virtuous and good-looking."
            ],
            const._VENUS: [
                "Wealthy and famous; clean habits; enjoys comforts."
            ],
            const._SATURN: [
                "Scholarly and wealthy; leads a town/village; unclean; shunned by women."
            ],
        },
        "mercury": {  # Jupiter in Gemini/Virgo
            const._SUN: [
                "Heads a village/town; large family; widely known."
            ],
            const._MOON: [
                "Virtuous, very famous and wealthy; favoured by mother; excellent qualities."
            ],
            const._MARS: [
                "Constant sensuality; victorious; wealthy and admirable; bears injury scars."
            ],
            const._MERCURY: [
                "Astrologer/savant craft; sculptor; articulate; blessed with spouse and children."
            ],
            const._VENUS: [
                "Wealth, spouse, progeny, lands and houses; yet addicted to wayward women."
            ],
            const._SATURN: [
                "Leads town/city; good looks; honoured by authority."
            ],
        },
        "moon": {  # Jupiter in Cancer
            const._SUN: [
                "Loss of wife’s wealth/children, then recovery of all; commands men."
            ],
            const._MOON: [
                "Controls treasury; wealthy; high status; many comforts."
            ],
            const._MARS: [
                "Marries a young girl; wealthy; scholarly; bears injury marks."
            ],
            const._MERCURY: [
                "Supports brothers; wealthy; quarrelsome yet trustworthy."
            ],
            const._VENUS: [
                "Many wives; highly famous; fortunate."
            ],
            const._SATURN: [
                "Leads village/town/army; very talkative; sensual comforts in old age."
            ],
        },
        "sun": {  # Jupiter in Leo
            const._SUN: [
                "Overspends; famous; kind-hearted; kingly bearing."
            ],
            const._MOON: [
                "Exceptionally fortunate; wealth through wife’s help."
            ],
            const._MARS: [
                "Loyal to preceptors/friends; does hard tasks; pious but harsh; a leader."
            ],
            const._MERCURY: [
                "Builder/scientific bent; strong oratory; ministerial and scholarly."
            ],
            const._VENUS: [
                "Fond of women; status via the ruler; robust."
            ],
            const._SATURN: [
                "Talks too much; comforts lacking; defeated in battle; status falls."
            ],
        },
        "own": {  # Jupiter in Sagittarius/Pisces
            const._SUN: [
                "Clashes with authority; shunned by friends/relatives."
            ],
            const._MOON: [
                "Many comforts; desired by women; pride from wealth/status."
            ],
            const._MARS: [
                "Wounded in battle; harsh and harmful; still helpful to others."
            ],
            const._MERCURY: [
                "Minister/king archetype; pleases all; wealth, sons and good fortune."
            ],
            const._VENUS: [
                "Wealthy, content, famous, learned and long-lived."
            ],
            const._SATURN: [
                "Unclean habits; cowardice; loss of standing."
            ],
        },
        "saturn": {  # Jupiter in Capricorn/Aquarius
            const._SUN: [
                "Learned and kingly; attractive; brave; numerous comforts."
            ],
            const._MOON: [
                "Keen mind; religious; proud yet respectful to parents; wealthy and learned."
            ],
            const._MARS: [
                "Brave; fights for the ruler; arrogant; courageous; honoured."
            ],
            const._MERCURY: [
                "Easily yields to women; group leader; rich; religious; driver/vehicle role; many friends."
            ],
            const._VENUS: [
                "Women are drawn; abundant pleasures and possessions."
            ],
            const._SATURN: [
                "High moral fibre; learned; famous; king-like; fond of comforts."
            ],
        },
    }

    # Build html for only those planets actually aspecting Jupiter now
    sub_sections_jupiter = []
    for aspector_pid, lines in P_JUPITER[_host].items():
        if not _does_aspect(aspector_pid, jup_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Jupiter in {SIGN_NAMES[jup_sign]}:"
        block = [f"<p class='text-center mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-center mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdx = _md_period_for(aspector_pid)
        if mdx:
            s, e = mdx
            block.append(
                f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        if is_weak_aspector:
            block.append(
                f"<p class='text-center'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {PLANET_NAMES.get(aspector_pid)} is weak</p>"
            )

        sub_sections_jupiter.append(''.join(block))

    if not sub_sections_jupiter:
        sub_sections_jupiter.append("<p class='text-center mb-1'>No canonical aspect on Jupiter is present right now.</p>")

    jupiter_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Jupiter (by sign of Jupiter)</h3>"
        f"<p class='text-center mb-1'><em>Jupiter is in {host_label}.</em></p>"
        + "".join(sub_sections_jupiter)
        + "</div>"
    )
    
        # ── Aspects on Venus in Different Signs (only when an aspect truly exists) ──
    # Reuse/define local helpers (same pattern used in other aspect sections)
    def _house_of(pid: int) -> int:
        h = p2h.get(pid)
        if h is not None:
            return h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    ASPECTS = {
        const._SUN: {6}, const._MOON: {6}, const._MERCURY: {6}, const._VENUS: {6},
        const._MARS: {3, 6, 8}, const._JUPITER: {4, 6, 8}, const._SATURN: {2, 6, 9},
    }
    def _does_aspect(from_pid: int, target_house_idx: int) -> bool:
        src = _house_of(from_pid)
        delta = (target_house_idx - src) % 12
        return delta in ASPECTS.get(from_pid, {6})

    # Venus’s sign & house
    v_sign = _planet_sign(const._VENUS)
    v_house_idx = _house_of(const._VENUS)

    # Host family for Venus’s current sign
    _host_v = (
        "mars"    if v_sign in (0, 7) else           # Aries, Scorpio
        "own"     if v_sign in (1, 6) else           # Taurus, Libra
        "mercury" if v_sign in (2, 5) else           # Gemini, Virgo
        "moon"    if v_sign == 3 else                # Cancer
        "sun"     if v_sign == 4 else                # Leo
        "jupiter" if v_sign in (8, 11) else          # Sagittarius, Pisces
        "saturn"                                      # Capricorn, Aquarius
    )
    host_label_v = {
        "mars":    "a sign of Mars (Aries/Scorpio)",
        "own":     "its own sign (Taurus/Libra)",
        "mercury": "a sign of Mercury (Gemini/Virgo)",
        "moon":    "the Moon’s sign (Cancer)",
        "sun":     "the Sun’s sign (Leo)",
        "jupiter": "a sign of Jupiter (Sagittarius/Pisces)",
        "saturn":  "a sign of Saturn (Capricorn/Aquarius)",
    }[_host_v]

    # Text payloads (faithful paraphrases) keyed by Venus’s sign family
    P_VENUS = {
        "mars": {  # Venus in Aries/Scorpio
            const._SUN: [
                "Favoured by rulers/authority; tormented by the wife; scholarly."
            ],
            const._MOON: [
                "Very fickle; risk of incarceration; driven by excessive sexual urge."
            ],
            const._MARS: [
                "Loss of money and status; servile situations."
            ],
            const._MERCURY: [
                "Hard-hearted and wicked; shunned by relatives; earns through illegitimate means."
            ],
            const._JUPITER: [
                "Good looks; charitable; tall; good spouse; pleasant manners."
            ],
            const._SATURN: [
                "Indolent, unattractive wanderer; thievish; keeps secret possessions."
            ],
        },
        "own": {  # Venus in Taurus/Libra
            const._SUN: [
                "Beautiful spouse; association with attractive women; wealth."
            ],
            const._MOON: [
                "Virtuous mother; blessed with sons, wealth, status and good looks; also consorts with women of easy morals."
            ],
            const._MARS: [
                "Deprived of home/comforts; sensuous; subdued/defeated in conflicts."
            ],
            const._MERCURY: [
                "Learned, well-mannered, sensuous; virtuous and famous."
            ],
            const._JUPITER: [
                "Obtains desired things—friends, women, children, vehicles and houses."
            ],
            const._SATURN: [
                "Poor, wicked and sickly; married to a difficult/immoral woman."
            ],
        },
        "mercury": {  # Venus in Gemini/Virgo
            const._SUN: [
                "Serves women; wise; affluent; enjoys comforts."
            ],
            const._MOON: [
                "Beautiful hair and eyes; youthful appearance; many comforts."
            ],
            const._MARS: [
                "Fortunate and sensuous; skilful in sex; wastes money on women."
            ],
            const._MERCURY: [
                "Learned, good-looking and wealthy; leads a group/community."
            ],
            const._JUPITER: [
                "Preceptor/teacher; artist/photographer profile; enjoys many comforts."
            ],
            const._SATURN: [
                "Humiliation and misery; shunned by people."
            ],
        },
        "moon": {  # Venus in Cancer
            const._SUN: [
                "Quick-tempered; wealthy spouse; troubled by opponents."
            ],
            const._MOON: [
                "First child a daughter, then sons; treats mother and step-mother equally."
            ],
            const._MARS: [
                "Master of several arts; wealthy; troubled by women; favourable toward relatives."
            ],
            const._MERCURY: [
                "Learned; spouse is learned; wealthy; a wanderer."
            ],
            const._JUPITER: [
                "Wealth, children, servants, vehicles and friends; favoured by authority."
            ],
            const._SATURN: [
                "Overpowered by women; poor and fallen; deprived of comforts."
            ],
        },
        "sun": {  # Venus in Leo
            const._SUN: [
                "Jealous; driven by desire; earnings come via women."
            ],
            const._MOON: [
                "Inconsistent; two mothers; famed yet suffers due to women."
            ],
            const._MARS: [
                "Favoured by rulers; famous; fond of women; addicted to others’ wives; wealthy."
            ],
            const._MERCURY: [
                "Hoarding/greedy; falsehood; excessive lust."
            ],
            const._JUPITER: [
                "High status; many women and children; rich."
            ],
            const._SATURN: [
                "King-like stature; good-looking; spouse may be a widow."
            ],
        },
        "jupiter": {  # Venus in Sagittarius/Pisces
            const._SUN: [
                "Short-tempered, learned, wealthy and strong; travels abroad."
            ],
            const._MOON: [
                "Famous, very strong; numerous physical pleasures."
            ],
            const._MARS: [
                "Aversion to women; varied comforts; natural leadership."
            ],
            const._MERCURY: [
                "Enjoys ornaments, good dress, food and vehicles."
            ],
            const._JUPITER: [
                "Many wives/children; very wealthy; abundant sensual pleasures."
            ],
            const._SATURN: [
                "Fortunate, rich, indulgent; good earner."
            ],
        },
        "saturn": {  # Venus in Capricorn/Aquarius
            const._SUN: [
                "Steady nature; famous; wealthy, powerful and truthful."
            ],
            const._MOON: [
                "Valorous, powerful and attractive; wealthy."
            ],
            const._MARS: [
                "Sickliness; exhausted by labour; penury."
            ],
            const._MERCURY: [
                "Learned; accumulates wealth; truthful; very scholarly."
            ],
            const._JUPITER: [
                "Youthful; loves music and scents; associates with worthy women; fond of finery."
            ],
            const._SATURN: [
                "Darker complexion; blessed with servants and comforts."
            ],
        },
    }

    # Build html for only those planets actually aspecting Venus now
    sub_sections_venus = []
    for aspector_pid, lines in P_VENUS[_host_v].items():
        if not _does_aspect(aspector_pid, v_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Venus in {SIGN_NAMES[v_sign]}:"
        block = [f"<p class='text-center mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-center mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdv = _md_period_for(aspector_pid)
        if mdv:
            s, e = mdv
            block.append(
                f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        if is_weak_aspector:
            block.append(
                f"<p class='text-center'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {PLANET_NAMES.get(aspector_pid)} is weak</p>"
            )

        sub_sections_venus.append(''.join(block))

    if not sub_sections_venus:
        sub_sections_venus.append("<p class='text-center mb-1'>No canonical aspect on Venus is present right now.</p>")

    venus_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Venus (by sign of Venus)</h3>"
        f"<p class='text-center mb-1'><em>Venus is in {host_label_v}.</em></p>"
        + "".join(sub_sections_venus)
        + "</div>"
    )
    
        # ── Aspects on Saturn in Different Signs (only when an aspect truly exists) ──
    # Reuse/define local helpers (same pattern used in other aspect sections)
    def _house_of(pid: int) -> int:
        h = p2h.get(pid)
        if h is not None:
            return h
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            try:
                lonx = float(natal_pp[pid + 1][1][1]) % 360.0
                sidx = int(lonx // 30)
            except Exception:
                sidx = 0
        return (sidx - asc_sign) % 12

    ASPECTS = {
        const._SUN: {6}, const._MOON: {6}, const._MERCURY: {6}, const._VENUS: {6},
        const._MARS: {3, 6, 8}, const._JUPITER: {4, 6, 8}, const._SATURN: {2, 6, 9},
    }
    def _does_aspect(from_pid: int, target_house_idx: int) -> bool:
        src = _house_of(from_pid)
        delta = (target_house_idx - src) % 12
        return delta in ASPECTS.get(from_pid, {6})

    # Saturn’s sign & house
    sat_sign = _planet_sign(const._SATURN)
    sat_house_idx = _house_of(const._SATURN)

    # Host family for Saturn’s current sign
    _host_s = (
        "mars"    if sat_sign in (0, 7) else           # Aries, Scorpio
        "venus"   if sat_sign in (1, 6) else           # Taurus, Libra
        "mercury" if sat_sign in (2, 5) else           # Gemini, Virgo
        "moon"    if sat_sign == 3 else                # Cancer
        "sun"     if sat_sign == 4 else                # Leo
        "jupiter" if sat_sign in (8, 11) else          # Sagittarius, Pisces
        "own"                                        # Capricorn, Aquarius
    )
    host_label_s = {
        "mars":    "a sign of Mars (Aries/Scorpio)",
        "venus":   "a sign of Venus (Taurus/Libra)",
        "mercury": "a sign of Mercury (Gemini/Virgo)",
        "moon":    "the Moon’s sign (Cancer)",
        "sun":     "the Sun’s sign (Leo)",
        "jupiter": "a sign of Jupiter (Sagittarius/Pisces)",
        "own":     "its own sign (Capricorn/Aquarius)",
    }[_host_s]

    # Text payloads (faithful paraphrases) keyed by Saturn’s sign family
    P_SATURN = {
        "mars": {  # Saturn in Aries/Scorpio
            const._SUN:     ["Turns to agriculture; wealthy; tends cattle."],
            const._MOON:    ["Keeps low company; fickle; wicked; drawn to coarse/ugly partners."],
            const._MARS:    ["Wretched; cruel to animals; leads thieves; indulges in meat, women and wine."],
            const._MERCURY: ["Quarrelsome; irreligious; voracious; a notorious thief."],
            const._JUPITER: ["Religious and fortunate; high status with rulers; minister-like; wealthy."],
            const._VENUS:   ["Ever-changing; ill-looking; addicted to others’ wives; destitute."],
        },
        "venus": {  # Saturn in Taurus/Libra
            const._SUN:     ["Lacks wealth; learned; weak-bodied; clear speech."],
            const._MOON:    ["High status with rulers; helped by women; fine clothes and ornaments."],
            const._MARS:    ["Skilled in warfare; kind-hearted; talkative; rich."],
            const._MERCURY: ["Very witty; eager to please women; favoured by the king."],
            const._JUPITER: ["Helpful to others; charitable; skilful."],
            const._VENUS:   ["Favoured by rulers; gains from gems; indulges in wine and women."],
        },
        "mercury": {  # Saturn in Gemini/Virgo
            const._SUN:     ["Bereft of wealth/pleasures/anger; religious and content."],
            const._MOON:    ["King-like; soft skin; loved and respected by women."],
            const._MARS:    ["Fighter/wrestler; wise; limb defect; well-known."],
            const._MERCURY: ["Wealthy; skilled in fighting and dance; talented singer/painter/sculptor."],
            const._JUPITER: ["Favoured by rulers; virtuous; liked by friends."],
            const._VENUS:   ["Fond of women; versed in Yoga-śāstra; adept at serving women."],
        },
        "moon": {  # Saturn in Cancer
            const._SUN:     ["Early loss of father; without money/spouse/comforts; sinful."],
            const._MOON:    ["Wealthy; harmful to mother and brothers."],
            const._MARS:    ["Lacks strength; favoured by rulers; anxious/worrisome."],
            const._MERCURY: ["Wanderer; deceitful; harsh; an orator."],
            const._JUPITER: ["Has friends, sons, lands, houses; wealthy."],
            const._VENUS:   ["Deprived of comforts despite good birth."],
        },
        "sun": {  # Saturn in Leo
            const._SUN:     ["Moneyless, comfortless and of poor qualities; lies; fond of drink; slim; miserable."],
            const._MOON:    ["Fame, wealth, women and gems; favoured by rulers."],
            const._MARS:    ["Wanderer; dwells in forts/mountains; cruel fighter."],
            const._MERCURY: ["Deceitful; indolent; poor; ugly."],
            const._JUPITER: ["Leads a village/town/group; wealthy; virtuous."],
            const._VENUS:   ["Good-looking; wealthy; troubled by women."],
        },
        "jupiter": {  # Saturn in Sagittarius/Pisces
            const._SUN:     ["Famous; fond of others’ children."],
            const._MOON:    ["Motherless; yet blessed with wife, sons and riches."],
            const._MARS:    ["Vaata ailments; foreign residence."],
            const._MERCURY: ["King-like; respectable; rich; good-looking."],
            const._JUPITER: ["Equal to a king; army commander; powerful."],
            const._VENUS:   ["Lives abroad; two mothers/fathers; pursues many things at once."],
        },
        "own": {  # Saturn in Capricorn/Aquarius
            const._SUN:     ["Sickly; spouse unattractive; wanderer; miserable; carries loads."],
            const._MOON:    ["Has wealth and wife; opposed to mother; sexually excessive."],
            const._MARS:    ["Courageous, famous and powerful; leader of multitudes; harsh."],
            const._MERCURY: ["Powerful; quick-tempered; famous; limited money."],
            const._JUPITER: ["Famous; virtuous; long-lived, healthy; handsome body."],
            const._VENUS:   ["Very wealthy; sensuous; addicted to others’ wives; norm-breaking."],
        },
    }

    # Build html for only those planets actually aspecting Saturn now
    sub_sections_sat = []
    for aspector_pid, lines in P_SATURN[_host_s].items():
        if not _does_aspect(aspector_pid, sat_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Saturn in {SIGN_NAMES[sat_sign]}:"
        block = [f"<p class='text-center mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-center mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdv = _md_period_for(aspector_pid)
        if mdv:
            s, e = mdv
            block.append(
                f"<p class='text-center mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
            )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        if is_weak_aspector:
            block.append(
                f"<p class='text-center'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {PLANET_NAMES.get(aspector_pid)} is weak</p>"
            )

        sub_sections_sat.append(''.join(block))

    if not sub_sections_sat:
        sub_sections_sat.append("<p class='text-center mb-1'>No canonical aspect on Saturn is present right now.</p>")

    saturn_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Saturn (by sign of Saturn)</h3>"
        f"<p class='text-center mb-1'><em>Saturn is in {host_label_s}.</em></p>"
        + "".join(sub_sections_sat)
        + "</div>"
    )
    
        # ── Yogas & Doṣas – auto-discovery, evidence & effects ─────────────────
    def _effect_hint(name: str) -> str:
        """Lightweight classic-effect hint when the library output lacks one."""
        n = (name or "").lower().replace("_", " ").strip()
        if "raja" in n or "rajayoga" in n:
            return "Classic rāja-yoga: rise in status, authority, patronage; support from power."
        if "dhan" in n or "dhana" in n or "wealth" in n:
            return "Wealth-yoga: accumulation of assets, earnings and financial leverage."
        if "gaja" in n or "kesari" in n:
            return "Gaja-Keśarī: reputation, patronage, counsel, protection from harm."
        if "chandra" in n and "mangal" in n:
            return "Chandra-Maṅgala: trading/commercial drive, liquidity, business acumen."
        if "pancha" in n or "mahapurusha" in n or "panch" in n:
            return "Mahāpuruṣa yoga: strong worldly prominence (planet-specific expression)."
        if "vipareet" in n or "viparita" in n:
            return "Viparīta rāja-yoga: gains via adversity, hidden support after setbacks."
        if "sakata" in n:
            return "Sakata-doṣa: boom-bust cycles, wavering fortunes linked to the Moon."
        if "kemadrum" in n:
            return "Kema-druma: isolation/instability of mind and resources; remedial support helps."
        if "daridra" in n:
            return "Daridra doṣa: poverty/shortfall indications unless strongly cancelled."
        if "kala" in n and "sarpa" in n:
            return "Kāla-Sarp(a): constrictive patterns, sudden rises/falls; cancellations can moderate."
        if "pitra" in n or "pitri" in n:
            return "Pitṛ-doṣa: ancestral/lineage obligations; delays till propitiated or offset."
        return "Favourable/Adverse yoga per classical rules; strength varies with dignity and aspect."

    def _stringify(obj) -> str:
        if obj is None:
            return ""
        if isinstance(obj, (str, int, float)):
            return str(obj)
        if isinstance(obj, (list, tuple, set)):
            parts = []
            for it in obj:
                parts.append(_stringify(it))
            return "; ".join([p for p in parts if p])
        if isinstance(obj, dict):
            kv = []
            for k, v in obj.items():
                if isinstance(v, (dict, list, tuple)) and not v:
                    continue
                kv.append(f"{k}: {_stringify(v)}")
            return "; ".join([p for p in kv if p])
        return str(obj)

    def _normalise_yoga_output(data) -> list[dict]:
        """Turn any reasonable library output into [{name, evidence, effects}] items."""
        out = []
        if not data:
            return out
        # Dict: name -> details
        if isinstance(data, dict):
            for k, v in data.items():
                name = _stringify(k)
                evidence = ""
                effects = ""
                if isinstance(v, dict):
                    # look for common keys
                    evidence = _stringify(v.get("evidence") or v.get("details") or v.get("why") or v.get("comment") or v.get("conditions"))
                    effects  = _stringify(v.get("effect") or v.get("effects") or v.get("result") or v.get("prediction"))
                    if not effects:
                        effects = _effect_hint(name)
                elif isinstance(v, (list, tuple, set)):
                    evidence = _stringify(v)
                    effects = _effect_hint(name)
                elif isinstance(v, (str, int, float, bool)):
                    if isinstance(v, bool):
                        if not v:
                            continue
                        evidence = "Detected by yoga checker."
                        effects = _effect_hint(name)
                    else:
                        # could be a textual verdict
                        txt = _stringify(v)
                        evidence = txt
                        effects  = _effect_hint(name)
                else:
                    evidence = _stringify(v)
                    effects = _effect_hint(name)
                out.append({"name": name, "evidence": evidence, "effects": effects})
            return out
        # List/tuple: maybe list of names or detailed tuples
        if isinstance(data, (list, tuple, set)):
            for item in data:
                if isinstance(item, (tuple, list)) and item:
                    name = _stringify(item[0])
                    details = _stringify(item[1:]) if len(item) > 1 else ""
                    out.append({"name": name, "evidence": details or "Detected.", "effects": _effect_hint(name)})
                else:
                    name = _stringify(item)
                    out.append({"name": name, "evidence": "Detected.", "effects": _effect_hint(name)})
            return out
        # Fallback boolean/string
        if isinstance(data, bool):
            if data:
                out.append({"name": "Yoga", "evidence": "Detected.", "effects": _effect_hint("yoga")})
            return out
        if isinstance(data, (str, int, float)):
            out.append({"name": str(data), "evidence": "Detected.", "effects": _effect_hint(str(data))})
            return out
        return out

    def _try_call(func, *args):
        try:
            return func(*args)
        except TypeError:
            # Try alternate signatures commonly seen in pyjhora forks
            for alt in (
                (natal_pp,),                           # (pp)
                (natal_pp, asc_sign),                  # (pp, asc)
                (natal_pp, p2h),                       # (pp, p2h)
                (natal_pp, asc_sign, p2h),             # (pp, asc, p2h)
            ):
                try:
                    return func(*alt)
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # 1) Collect yogas from jd_yoga & jd_raja by scanning for relevant getters
    yoga_items: list[dict] = []
    scanned_funcs = []

    def _gather_from_module(mod):
        names = [n for n in dir(mod)
                 if callable(getattr(mod, n))
                 and (("yoga" in n.lower() and n.lower().startswith(("get_", "list_", "detect_", "calc_", "compute_")))
                      or n.lower() in {"yogas", "getyogas", "rajayogas", "get_rajayogas", "get_raja_yogas"})]
        for n in names:
            func = getattr(mod, n, None)
            if not func:
                continue
            if func in scanned_funcs:
                continue
            scanned_funcs.append(func)
            res = _try_call(func, natal_pp)
            items = _normalise_yoga_output(res)
            yoga_items.extend(items)

    for _mod in (jd_yoga, jd_raja):
        _gather_from_module(_mod)

    # Deduplicate by lowercase name while preserving first evidence/effects
    seen = set()
    deduped = []
    for it in yoga_items:
        key = (it.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    yoga_items = deduped

    # 2) Doshas from current chart state (combustion, retrograde, graha-yuddha)
    dosha_items: list[dict] = []

    # combustion
    for pid in sorted(_combust_set):
        dosha_items.append({
            "name": f"Combustion – {PLANET_NAMES.get(pid, pid)}",
            "evidence": f"{PLANET_NAMES.get(pid, pid)} is reported combust (close to Sun) per library check.",
            "effects": "Weakens expression/significations of the planet; delays, volatility, lowered support."
        })
    # retrograde
    for pid in sorted(_retro_set):
        # skip Rahu/Ketu which are always retro by concept
        if pid in (getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)):
            continue
        dosha_items.append({
            "name": f"Retrograde – {PLANET_NAMES.get(pid, pid)}",
            "evidence": f"{PLANET_NAMES.get(pid, pid)} is retrograde at birth.",
            "effects": "Non-linear results, reversals/redo cycles, internalised themes; can be strong yet eccentric."
        })
    # graha-yuddha (planetary war) if available
    gy_pairs = []
    for func in (getattr(pdrik, "planets_in_graha_yudh", None),
                 getattr(jd_charts, "planets_in_graha_yudh", None)):
        if func:
            try:
                gy_pairs = func(natal_pp) or []
                break
            except Exception:
                pass
    for pair in gy_pairs:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            continue
        winner, loser = pair
        wname = PLANET_NAMES.get(winner, winner)
        lname = PLANET_NAMES.get(loser, loser)
        dosha_items.append({
            "name": f"Graha-Yuddha (Planetary War): {wname} defeats {lname}",
            "evidence": f"{wname} is closer to the ecliptic latitude/longitudinal dominance versus {lname} at birth (library reports a war pair).",
            "effects": f"The winner ({wname}) gains prominence; the loser ({lname}) suffers weakness/obstruction in its portfolios."
        })

    # 3) Build HTML
    if not yoga_items and not dosha_items:
        yoga_dosha_html = (
            "<div class='mt-4'>"
            "<h3 class='h6 text-center'>Yogas & Doṣas</h3>"
            "<p class='text-center mb-1'>No standard yogas or doṣas were detected by the current library checks.</p>"
            "</div>"
        )
    else:
        parts = ["<div class='mt-4'><h3 class='h6 text-center'>Yogas & Doṣas</h3>"]

        if yoga_items:
            parts.append("<h4 class='h6 text-center mt-2'>Applicable Yogas</h4>")
            for it in yoga_items:
                nm = _stringify(it.get("name"))
                ev = _stringify(it.get("evidence"))
                ef = _stringify(it.get("effects"))
                parts.append(f"<p class='text-center mb-1'><strong>{nm}</strong></p>")
                if ev:
                    parts.append(f"<p class='text-center mb-1'>Evidence: {ev}</p>")
                if ef:
                    parts.append(f"<p class='text-center mb-2'>Predicted effects: {ef}</p>")

        if dosha_items:
            parts.append("<h4 class='h6 text-center mt-2'>Applicable Doṣas</h4>")
            for it in dosha_items:
                nm = _stringify(it.get("name"))
                ev = _stringify(it.get("evidence"))
                ef = _stringify(it.get("effects"))
                parts.append(f"<p class='text-center mb-1'><strong>{nm}</strong></p>")
                if ev:
                    parts.append(f"<p class='text-center mb-1'>Evidence: {ev}</p>")
                if ef:
                    parts.append(f"<p class='text-center mb-2'>Predicted effects: {ef}</p>")

        parts.append("</div>")
        yoga_dosha_html = "".join(parts)

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
  {sun_rashi_html}
  {moon_rashi_html}
  {mars_rashi_html}
  {rashi_mercury_html}
  {rashi_jupiter_html}
  {rashi_venus_html}
  {rashi_saturn_html}
  {rashi_rahu_html}
  {rashi_ketu_html}
  {sun_aspects_html}
  {moon_aspects_html}
  {mars_aspects_html}
  {mercury_aspects_html}
  {jupiter_aspects_html}
  {venus_aspects_html}
  {saturn_aspects_html}
  {yoga_dosha_html}
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
