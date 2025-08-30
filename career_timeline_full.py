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
    reading_html = reading_html.replace("</div>", f"{weak_note_html}</div>")

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
    reading2_html = reading2_html.replace("</div>", f"{weak2_note_html}</div>")

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
