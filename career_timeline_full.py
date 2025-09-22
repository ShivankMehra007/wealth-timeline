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
            "Planet": PLANET_NAMES.get(pid, str(pid)),
            "Sign": sign_name,
            "House": house_no,
            "House Lord": house_lord,
            "Longitude": longitude_str,
            "Nakshatra": nak_name,
            "Pada": pada,
            "Motion": motion,
        })

        df = pd.DataFrame(rows, columns=["Planet","Sign","House","House Lord","Longitude","Nakshatra","Pada","Motion"])
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
    header = f"Your Lagna Lord, {lagna_lord_name}, is in the {SIGN_TXT[ll_house_idx]} house."

    if ll_house_no == 1:
        reading_lines += [
            "With your Lagna Lord placed in the 1st house, you naturally radiate vitality, courage, and a strong presence, making you the central force in your own life story. This placement blesses you with sound health, longevity, and the ability to bounce back from challenges, while also giving you leadership qualities and a magnetic personality that draws others to you. You are bold, quick to act, and versatile—sometimes even restless or fickle, but this same adaptability helps you thrive in changing circumstances. Gains from land or property are likely. In relationships, your strong self-focus can make you charismatic yet occasionally bring complexity in partnerships. When your Lagna and its lord are strong, all areas of life—career, prosperity, and status—benefit greatly, but you may need to guard against impulsiveness, occasional health fluctuations, or unstable decisions. Overall, this is one of the most empowering placements, showing that you are destined to shape your own path with courage and personal initiative.",
        ]
    elif ll_house_no == 2:
        reading_lines += [
            "With your Lagna Lord placed in the Second House, expect a tilt toward learning, dignity and self-respect, a sober value system with religious/spiritual leanings, and good longevity, alongside a knack for building tangible assets such and land or vehicles, plus a generally virtuous reputation. In modern life, this often reads as stable earnings, respectable family standing, and speech that opens doors. However, you need to watch out for phases of overspending, strained family agreements, or attachment to possessions—keeping your counsel steady and aligning money choices with your principles brings out the best of this placement.",
        ]
    elif ll_house_no == 3:
        reading_lines += [
            "With your Lagna lord placed in the 3rd house, you are someone who thrives on self-effort, courage, and initiative. This gives you a natural drive to push forward in life through your own actions, making you enterprising, hardworking, and often willing to take risks to achieve your goals. You come across as bold in communication, with potential for skills in writing, speaking, or fields that demand constant interaction, movement, or short travels. Relationships with siblings, relatives, and close associates are central to your journey—sometimes supportive, sometimes competitive—and they shape your confidence and growth. Health generally benefits when your Lagna lord is strong, though overexertion can bring strain to areas ruled by the 3rd house such as shoulders, arms, or neck, and impulsiveness should be guarded against. Overall, this placement makes you someone who creates your own path, where progress and recognition flow directly from your ability to take initiative, work consistently, and courageously face challenges.",
        ]
    elif ll_house_no == 4:
        reading_lines += [
            "With your Lagna lord placed in the 4th house, your chart suggests a life blessed with comforts and property connected to your mother and home, as well as the support of several brothers. You are naturally sensual yet uphold virtue, combining good looks with inner strength. Longevity is promised, and devotion toward both parents remains a defining feature of your character. Though your appetite for food is light, your appetite for family bonds and values is strong, making you someone who balances worldly enjoyment with virtue and familial duty.",
        ]
    elif ll_house_no == 5:
        reading_lines += [
            "Your ascendant lord being placed in the 5th house makes you someone who is naturally proud, strong-willed, and quick to anger, yet capable of earning respect and honour from rulers, bosses, or those in authority. Life may bring some mixed results with regard to children—ordinary comforts through them, and in some cases, concerns about the survival or well-being of the first-born. Despite such challenges, the placement promises longevity, and at heart you remain inclined towards virtuous deeds, noble actions, and a desire to live a meaningful life in alignment with higher principles.",
        ]
    elif ll_house_no == 6:
        if afflicted:
            reading_lines += [
                "When your lagna lord is placed in the 6th house and afflicted, it generally indicates struggles connected to health, vitality, and competition. The ascendant lord represents you — your body, personality, and life direction — and its placement in the 6th house, which governs diseases, debts, obstacles, and rivals, suggests that challenges in these areas will be central themes in your life. Affliction here can manifest as recurring health issues, susceptibility to stress, or a tendency to attract hidden enemies and rivals who try to create hurdles. Careful attention to health, disciplined lifestyle, and conscious handling of conflicts are important to balance this energy and avoid unnecessary troubles.",
            ]
        else:
            reading_lines += [
                "Your ascendant lord being placed in the 6th house indicates a life marked by resilience and quiet strength. You are naturally inclined toward good health and possess the ability to overcome adversities with determination. This placement makes you formidable against opponents—whether in competition, workplace politics, or life’s challenges—often turning rivals into stepping stones for your own progress. You tend to live with frugality and discipline, which paradoxically brings you lasting wealth and stability. Gains often come through consistent work, land, or practical efforts, rewarding you for diligence and persistence. This combination shows a person who thrives through hard work, service, and the ability to conquer obstacles with patience and inner power.",
            ]

    elif ll_house_no == 7:
        reading_lines += [
            "With your lagna lord placed in the 7th house, your chart highlights strong personal magnetism and a natural brilliance that makes you noticeable to others. This placement often blesses you with an attractive personality, charm in interactions, and the ability to draw people toward you with ease. Relationships hold special significance in your life, and you are likely to have a spouse who is not only good-looking but also good-natured and supportive. Partnerships—whether in marriage or work—become a major source of growth and balance, reflecting both your outward brilliance and your inner need to share life harmoniously with others.",
        ]
        if is_malefic_natural:
            reading_lines += [
                "Your ascendant lord is a natural malefic, which colors your life path with intensity and extremes. This placement can bring experiences of separation or detachment in relationships, sometimes leaving one bereft of spouse or close companionship. It may also indicate phases of hardship, poverty, or wandering, yet equally, it can raise you to positions of great power, authority, or even kingship. The same energy that brings struggles also fuels resilience, pushing you toward distant lands or foreign experiences, where transformation and growth await. This chart signature speaks of a destiny shaped by challenges that can either bind you to suffering or elevate you toward mastery, depending on how you channel its force.",
            ]
    elif ll_house_no == 8:
        reading_lines += [
            "When your lagna lord is placed in the 8th house, it gives you a life marked by both endurance and complexity. You are likely to enjoy longevity and have the capacity to accumulate wealth over time, though this may come with periods of sudden gains or losses. Health, however, can be a sensitive area, with recurring issues that demand care. There may be impulses toward risky behaviors such as gambling or speculative ventures, and may face temptation to cheat in relationships, so self-discipline is essential. A fiery temperament may also show itself in your interactions. Yet, this very placement opens the door to deep spiritual pursuits, transformation, and hidden wisdom—pointing to a life path where challenges ultimately guide you toward inner growth and higher understanding.",
        ]
        reading_lines += [
            ("Eye diseases/strain are likely." if not h8_is_benefic else "Good looks/appearance from benefic 8th‑lord influence."),
        ]
    elif ll_house_no == 9:
        reading_lines += [
            "With your lagna lord placed in the 9th house, your chart shows a powerful alignment that blesses you with fortune, wisdom, and a natural inclination toward higher learning and spirituality. This placement makes you respected and beloved by others, drawing people toward your noble and virtuous qualities. It often brings devotion to Viṣṇu or a structured form of worship, giving stability to your spiritual life. Materially, this also promises a supportive spouse, good children, and sufficient wealth, enabling you to live with dignity. Most importantly, it elevates your reputation, ensuring that you become very well-known or famous in your circles for your values and achievements.",
        ]
    elif ll_house_no == 10:
        reading_lines += [
            "When your lagna lord is placed in the 10th house, it brings a powerful alignment for career, recognition, and worldly success. You are likely to be highly learned and command respect in professional and social circles. Such a placement often draws the favor of authority figures, including rulers, employers, or people in power, who may honor or elevate you. It also indicates strong support and comforts through your father, along with blessings that manifest as guidance, protection, or inherited dignity. The 10th house being the house of karma (action) and status, your own effort and prowess become the source of fame, prosperity, and wealth. This placement suggests a life where your personal initiative and capabilities open doors to public recognition, steady advancement, and a sense of fulfillment in your chosen field.",
        ]
    elif ll_house_no == 11:
        reading_lines += [
            "When your lagna lord is placed in the 11th house, it indicates a life blessed with manifold gains and prosperity. You are endowed with admirable qualities that earn you recognition and fame, and you may find yourself surrounded by influential circles and networks that bring continuous opportunities. This placement also shows comfort and enjoyment of life’s pleasures, often with strong support from family and children who are long-lived and fortunate. It may suggest multiple relationships or marriages in some cases, but overall it strongly points to growth, fulfillment of desires, and living a life of ease and abundance.",
        ]
    elif ll_house_no == 12:
        reading_lines += [
            "Since your lagna lord is placed in the 12th house, the chart suggests that life may often feel marked by a sense of sacrifice or detachment. Bodily comforts might not always come easily, and there may be a tendency to get drawn into pursuits that don’t always bring lasting fulfillment or recognition. At the same time, this position strongly emphasizes foreign connections — whether through travel, work, or residence abroad — and can make you seek meaning beyond conventional boundaries. The 12th house also represents seclusion, spirituality, and hidden matters, so this placement may at times incline you toward private struggles or quiet inner growth, where the challenge is to transform apparent losses into deeper wisdom.",
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
        f"<p class='text-left mb-1'><em>{header}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_lines)
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
        if pid == const._SUN:
            return jd_strength.shad_bala(jd_birth, place)[6][0]
        elif pid == const._MOON:
            return jd_strength.shad_bala(jd_birth, place)[6][1]
        elif pid == const._MARS:
            return jd_strength.shad_bala(jd_birth, place)[6][2]
        elif pid == const._MERCURY:
            return jd_strength.shad_bala(jd_birth, place)[6][3]
        elif pid == const._JUPITER:
            return jd_strength.shad_bala(jd_birth, place)[6][4]
        elif pid == const._VENUS:
            return jd_strength.shad_bala(jd_birth, place)[6][5]
        else:
            return jd_strength.shad_bala(jd_birth, place)[6][6]

    sb_res = _get_shadbala_result()
    sb_val = _extract_shadbala_val(sb_res, lagna_lord_pid)
    sb_weak = False
    if lagna_lord_pid in SHAD_THRESH and sb_val is not None:
        sb_weak = sb_val < SHAD_THRESH[lagna_lord_pid]

    weak = (lagna_lord_pid in avs["bala"]) or (lagna_lord_pid in avs["mrita"]) or (lagna_lord_pid in avs["sushupti"]) or sb_weak
    if weak:
        weak_note_html = f"<p class='text-left mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {lagna_lord_name} is weak</p>"
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
        md1_note_html = f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {lagna_lord_name}:</strong> {_s:%Y-%m-%d} – {_e:%Y-%m-%d}</p>"
    weak_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val/1020)*100)}%</p>"
        )
    #reading_html = reading_html.replace("</div>", f"{md1_note_html}{weak_note_html}</div>")

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
            "With your 2nd house lord placed in the 1st house, it creates a strong connection between wealth, values, and self-identity. Such a placement often blesses you with material prosperity and comforts, along with a natural sense of thrift and financial prudence. You may be wealthy, but also cautious in handling money. At the same time, this position gives a sharpness or harsh edge to temperament, making you outspoken or direct in expression. You are generous toward others and inclined to help, but within your own family circle this same nature may cause friction or troubles. The chart suggests that you will enjoy good comforts, blessings in the form of children, and a life where your personal identity and financial growth are closely interlinked.",
        ]
    elif h2l_house_no == 2:
        reading2_lines += [
            "When the lord of your 2nd house is placed in the 2nd house itself, it makes finances and possessions a central theme in life. You are likely to enjoy wealth, comforts, and a sense of pride in what you accumulate, often having a good earning capacity and taste for luxuries. At the same time, the placement also carries karmic complexities: it can suggest multiple marriages or alliances, difficulties with progeny, and a tendency to oppose others or create friction in relationships. Thus, while material abundance and status are strongly favored, one must balance this with care in partnerships and sensitivity in personal dealings.",
        ]
    elif h2l_house_no == 3:
        reading2_lines += [
            "With the lord of your 2nd house placed in the 3rd house, it creates a blend of qualities that reflect both wisdom and courage. This shows that you are virtuous and principled at heart, with the ability to use knowledge and intelligence in practical ways. You possess valor, which means you are not afraid to take initiative or stand up for yourself and others. At the same time, this placement brings strong desires and inclinations toward worldly pleasures, making you somewhat sensuous and inclined toward material gains. This mix gives you a personality that is admirable for its wisdom and bravery, but also one that must be careful of its attachment to wealth and enjoyment.",
        ]
        if is_h2_malefic_nat:
            reading2_lines.append("As a natural malefic 2nd-lord in the 3rd: You may have differences with co-borns.")
        if is_h2_benefic:
            reading2_lines.append("As a natural benefic 2nd-lord in the 3rd: You may be opposed to the ruler.")
        if h2_lord_pid == const._MARS:
            reading2_lines.append("Mars as 2nd-lord in the 3rd: You may have thief-like tendencies.")
        if has_malefic_assoc:
            reading2_lines.append("2nd-lord joined malefics in the 3rd: You may have a tendency to speak ill of the devas and those who are virtuous.")
    elif h2l_house_no == 4:
        reading2_lines += [
            "With your 2nd house lord placed in the 4th house, your chart suggests a fortunate alignment that blesses you with material comforts and inner contentment. Wealth and financial stability are likely to flow steadily in your life, supported by your truthful and upright nature. This placement also points toward a long and sustained life, with a foundation built on integrity. The 4th house connection indicates that your prosperity may extend to property, vehicles, or assets connected to your home and family, creating a sense of security and belonging. Additionally, benefits or support from your father or paternal lineage are indicated, further strengthening your fortunes and giving you both material and emotional grounding.",
        ]
        if is_exalted_h2 or has_guru_shukra_assoc:
            reading2_lines.append("You are likely to enjoy status akin to a king.")
        if h2_lord_pid == const._MARS:
            reading2_lines.append("Mars as 2nd-lord in the 4th is a maraka. You need to be careful of dangers to life.")
    elif h2l_house_no == 5:
        reading2_lines += [
            "With the lord of your 2nd house placed in the 5th house, it creates a powerful connection between wealth, family resources, and creative expansion. This placement shows that you are likely to be wealthy, skilled, and efficient in your endeavors, earning fame for your competence. It also indicates blessings in progeny—having several children or deriving joy and prosperity through them. The 5th house being a trine adds fortune to your finances, making you capable of earning greatly through your talents, intelligence, or even speculative ventures. However, while material abundance and recognition come naturally, there can be some delicacy or vulnerability in health that requires care. In essence, your chart promises prosperity, reputation, and fulfillment through both wealth and children, with the reminder to nurture physical well-being alongside success.",
        ]
    elif h2l_house_no == 6:
        reading2_lines += [
            "With your 2nd house lord placed in the 6th house, your chart indicates a unique blend of wealth and challenge: you have the capacity to steadily accumulate wealth, yet this often comes through situations involving conflict, competition, or overcoming obstacles. Enemies, rivals, and even litigation may paradoxically open doors to financial gain or strengthen your position. This placement empowers you to destroy opposition, turn adversities into opportunities, and ultimately build prosperity by engaging directly with challenges that others might shy away from. It shows a life path where wealth, victory over difficulties, and the handling of disputes are closely interlinked.",
        ]
        if has_malefic_assoc:
            reading2_lines.append("However, your 2nd-house lord also has a malefic association. This could lead to loss of wealth, and disease of anal region and breast.")
    elif h2l_house_no == 7:
        reading2_lines += [
            "With your 2nd house lord placed in the 7th house, you are likely to have a strong sensuous nature and find that your spouse plays a significant role in supporting or contributing to your income, often pointing to a partner who brings financial gain or is actively involved in money-earning activities. At the same time, this placement can create a tendency for both you and your spouse to be drawn toward outside attractions, making the relationship vulnerable to temptations or adultery if not handled with awareness and trust.",
        ]
        if has_malefic_assoc:
            reading2_lines.append("You have a strong chance of becoming a physician.")
    elif h2l_house_no == 8:
        reading2_lines += [
            "With your 2nd house lord placed in the 8th house, the chart indicates that finances may often come through land or property, yet personal comforts—especially from the spouse—may feel reduced. Support from elder siblings may not flow as expected, and at times your actions could prove unintentionally harmful to others. This placement can also suggest phases where dependence on charity, favors, or external help becomes necessary, and in extreme cases, it may even incline the mind toward darker, self-destructive thoughts if other chart factors aggravate it.",
        ]
    elif h2l_house_no == 9:
        reading2_lines += [
            "With your 2nd house lord placed in the 9th house, your chart indicates the blessings of wealth and industriousness, suggesting that effort and dharmic alignment naturally draw prosperity toward you. Though early years may bring struggles with health or vitality, these challenges refine your resilience, and you eventually grow into a life of comfort and well-being. This placement also bestows eloquence—your words carry weight, and you are likely to develop into a fine orator, able to express wisdom in ways that inspire others. Ultimately, your financial security and voice of influence mature alongside your journey, making later life smoother and more rewarding.",
        ]
    elif h2l_house_no == 10:
        reading2_lines += [
            "With your 2nd house lord placed in the 10th house, you are likely to be a sensuous and self-respecting individual, blessed with learning and refinement. Relationships may be many, reflecting both charm and desire for companionship, though they may not always bring the comfort of progeny. Your sense of self-worth and material values naturally flow into your career, making you inclined toward professions connected with knowledge, aesthetics, or authority. Gains often come through associations with the ruler, government, or state institutions, giving you recognition and dignity in the professional sphere.",
        ]
    elif h2l_house_no == 11:
        reading2_lines += [
            "With your 2nd house lord positioned in the 11th house, your chart suggests a strong flow of wealth and resources into networks, social circles, and larger communities. This placement often makes you well-known and respectable, admired for your efficiency and reliability. You are likely to enjoy continuous benefits and gains throughout life, with money and opportunities flowing steadily. Prosperity does not remain limited to yourself—you naturally extend your support to others, fulfilling many people’s needs. This gives you both material abundance and the goodwill of society, making you someone who uplifts not just yourself but also your wider community.",
        ]
    elif h2l_house_no == 12:
        reading2_lines += [
            "With your 2nd house lord positioned in the 12th house, you are a person of courage and labor, willing to strive hard even when life places challenges before you. Yet, this placement suggests that material gains may often slip away or be lost, and the flow of wealth may feel uncertain. Comfort and support from your eldest child may not be as fulfilling or accessible as desired, adding a sense of distance in that bond. Still, the combination instills resilience and determination, teaching you to draw strength from effort rather than from easy comfort.",
        ]
        if is_h2_benefic:
            reading2_lines.append("Since your 2nd house-lord is a natural benefic and situated in the 12th, you have a strong chance of becoming a renowned trader.")

    reading2_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 2nd-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header2}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading2_lines)
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
        weak2_note_html = f"<p class='text-left mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {h2_lord_name} is weak</p>"
    # Inject note directly into the 2nd-lord reading block
    md2 = _md_period_for(h2_lord_pid)
    md2_note_html = ""
    if md2:
        _s2, _e2 = md2
        md2_note_html = f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {h2_lord_name}:</strong> {_s2:%Y-%m-%d} – {_e2:%Y-%m-%d}</p>"
    weak2_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb2_val/1020)*100)}%</p>"
        )
    #reading2_html = reading2_html.replace("</div>", f"{md2_note_html}{weak2_note_html}</div>")

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
            "With your 3rd house lord positioned in the 1st house, you come across as a courageous self-starter who is naturally valorous and often able to carve out wealth or success through bold personal initiative. This placement blesses you with sharp instincts and a certain street-smartness that allows you to navigate challenges with confidence, though it may sometimes indicate gaps in formal education. On the flip side, the same daring energy can lean toward reckless choices—such as risky affairs, tendencies toward cheating or forgery, or bending rules for personal gain—so it’s important to channel your courage into integrity and constructive pursuits.",
        ]
    elif h3l_house_no == 2:
        reading3_lines += [
            "With your 3rd house lord placed in the 2nd house, the chart shows a tendency to covet the wealth and spouse of others, with possible indulgence leading to obesity or over-attachment to material pleasures. There may be reluctance in initiating ventures, a lack of boldness, and challenges in asserting one’s valour. This placement can also point toward deprivation of certain comforts, opposition from one’s own people, and indications of a shortened life span if not balanced by benefic influences. The key lesson here is to cultivate restraint, channel energy into rightful pursuits, and consciously build harmony in personal and familial relations.",
        ]
    elif h3l_house_no == 3:
        reading3_lines += [
            "With your 3rd house lord placed in the 3rd house, you are naturally courageous, healthy, and valorous, blessed with strong willpower and the ability to assert yourself effectively. This placement often brings harmonious relations and support from siblings, along with a cooperative circle of family and friends who stand by you in times of need. You are inclined toward devotion, showing respect to teachers and reverence for divine forces, which strengthens your inner guidance. Such a position also favors prosperity and comforts in life, blessing you with wealth, sons, and social goodwill, making you a person who enjoys both material security and spiritual inclination.",
        ]
    elif h3l_house_no == 4:
        reading3_lines += [
            "With your 3rd house lord placed in the 4th house, you are naturally inclined toward a life of comfort and prosperity, with wealth and wisdom supporting your pursuits. This placement gives you confidence in self-expression, courage, and the ability to take initiative. Yet, the same alignment may create challenges in personal relationships—particularly showing a strained connection with your mother and some harshness or cruelty in the disposition of your spouse. In essence, while your chart favors growth, comfort, and intelligence, it also calls for conscious effort in nurturing family bonds and cultivating harmony in marriage.",
        ]
    elif h3l_house_no == 5:
        reading3_lines += [
            "With your 3rd house lord placed in the 5th house, your chart highlights a graceful flow of communication and effort into creativity, knowledge, and progeny. This alignment often blesses the native with virtuous conduct and a helpful nature, always inclined to uplift and support others. It also indicates blessings in matters of children, ensuring joy and continuity through progeny, while also granting longevity and a resilient constitution. Such a placement makes you a person remembered for wisdom, generosity, and noble deeds, with the ability to harmonize personal growth with service to others.",
        ]
        if malefic_touches_3lord:
            reading3_lines.append("However, given a malefic conjunction/aspect on the 3rd house lord, it indicates that your spouse will tend to be cruel.")
    elif h3l_house_no == 6:
        reading3_lines += [
            "With the lord of your 3rd house placed in the 6th house, your chart shows a karmic pattern where relationships with siblings, especially brothers, may at times be strained or marked by rivalry. Despite this, you carry strong potential for financial prosperity and material gains, indicating wealth accumulation through perseverance and overcoming struggles. Comfort or support from the maternal uncle may be limited, yet there may be an unusual attraction or attachment toward the maternal aunt. Health-wise, some tendencies toward recurring ailments, weakness, or eye troubles are possible, demanding care. This placement highlights a life path where challenges and enmities, though present, can serve as stepping-stones to significant prosperity and strength of character.",
        ]
    elif h3l_house_no == 7:
        reading3_lines += [
            "With your 3rd house lord placed in the 7th house, your life path reflects an early phase of challenges and unsettled experiences in childhood, yet it matures into greater comfort and stability as you grow older. You are inclined to follow authority figures and draw guidance from others, showing a tendency to respect and align with established structures rather than defy them. Relationships play a central role in your life’s journey, and your spouse is likely to be of good nature, supportive, and well-disposed, helping bring balance to the trials you faced in your early years.",
        ]
    elif h3l_house_no == 8:
        reading3_lines += [
            "With your 3rd house lord placed in the 8th house, the chart suggests that life lessons in communication, courage, and sibling relationships often intersect with themes of secrecy, transformation, and hidden struggles. This placement can at times bring a tendency toward cunning or thieving habits, or create a servile disposition where one may find themselves bound in subordination. The 8th house being a dusthāna (house of obstacles and sudden events) also indicates potential dangers of severe punishment or difficulties through authority figures and rulers. Additionally, your siblings may experience adverse outcomes or struggles, as the 3rd house governs their wellbeing. The overall tone is one of intensity and karmic challenges in areas of personal initiative and relationships with close kin.",
        ]
    elif h3l_house_no == 9:
        reading3_lines += [
            "With your 3rd house lord positioned in the 9th house, your chart suggests that fortune often flows to you through associations with women, while paternal support or comfort from your father may feel limited. Children play an important role in uplifting and aiding you on your path, and their presence can be a source of growth and strength. This placement also highlights your own leaning toward learning, knowledge, and wisdom, indicating a mind inclined to expand through study, travel, and higher pursuits.",
        ]
    elif h3l_house_no == 10:
        reading3_lines += [
            "With your 3rd house lord positioned in the 10th house, your life path is strongly driven by personal effort and initiative. You have the capacity to earn wealth through your own hard work and perseverance, and this placement often blesses you with material comforts and conveniences in life. It also ties your communication skills, courage, and networking (3rd house themes) directly to your career, status, and public image (10th house themes), giving you opportunities to rise in professional life and even receive honour or recognition from those in authority. However, this placement may also draw you into responsibilities or attachments that are morally questionable — such as associations with a person of questionable character — requiring discernment in your relationships and professional dealings. Overall, this alignment empowers you to carve out your destiny through effort and resilience, while reminding you to keep integrity as the guiding principle in your rise.",
        ]
    elif h3l_house_no == 11:
        reading3_lines += [
            "With your 3rd house lord positioned in the 11th house, your life path shows a mix of challenges and strengths. You may often feel vulnerable—sometimes weak, sickly, or prone to dependence on others—yet at the same time there lies within you a streak of courage that pushes you forward despite odds. Your gains in life tend to come primarily through your own efforts, struggles, and initiative rather than easy inheritance or support. There is a tendency to seek comfort and indulgence in physical pleasures, which can sometimes make you appear servile or overly compliant in relationships, but it also reflects a desire to experience life fully in its material and sensory dimensions. This placement paints a picture of someone whose courage shines brightest when tempered by hardship, and whose earnings and achievements carry the distinct stamp of self-effort and personal striving.",
        ]
    elif h3l_house_no == 12:
        reading3_lines += [
            "With your 3rd house lord placed in the 12th house, your life path shows a tendency toward hidden struggles and expenditures, sometimes directed toward questionable or indulgent pursuits. Relations with your father may feel strained, as his nature could come across as harsh or overly demanding. You may often find yourself at odds with relatives and friends, creating distance or friction in those connections. At the same time, gains through associations with women are strongly indicated, which can bring unexpected benefits or support. This placement also highlights a karmic pull toward distant lands, pointing to foreign travel, or even the possibility of residence abroad, where your growth and learning may unfold away from your place of origin.",
        ]

    reading3_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 3rd‑house lord</h3>"
        f"<p class='text-left mb-1'><em>{header3}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading3_lines)
        + "</div>"
    )

    # Mahadasha note for 3rd‑house lord
    md3 = _md_period_for(h3_lord_pid)
    md3_note_html = ""
    if md3:
        _s3, _e3 = md3
        md3_note_html = f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {h3_lord_name}:</strong> {_s3:%Y-%m-%d} – {_e3:%Y-%m-%d}</p>"

    # Weakness note for 3rd‑house lord (Avasthas & Shadbala)
    weak3_note_html = ""
    sb3_val = _extract_shadbala_val(sb_res, h3_lord_pid)
    sb3_weak = False
    if h3_lord_pid in SHAD_THRESH and sb3_val is not None:
        sb3_weak = sb3_val < SHAD_THRESH[h3_lord_pid]
    weak3 = (h3_lord_pid in avs["bala"]) or (h3_lord_pid in avs["mrita"]) or (h3_lord_pid in avs["sushupti"]) or sb3_weak
    if weak3:
        weak3_note_html = f"<p class='text-left mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the {h3_lord_name} is weak</p>"
    weak3_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb3_val/1020)*100)}%</p>"
        )
    #reading3_html = reading3_html.replace("</div>", f"{md3_note_html}{weak3_note_html}</div>")
    
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
            "With your 4th house lord placed in the 1st house, you are naturally blessed with strong maternal support and deep emotional grounding. This placement often makes you feel at home within yourself and gives comfort through domestic harmony, vehicles, and property-related matters. You are likely to be well-educated, virtuous, and inclined toward a good moral path. There are strong indications of gains in land, real estate, and conveniences that add to your sense of stability. Overall, this position reflects a life where inner contentment, support from mother, and material comforts combine to shape your personality and outward success.",
        ]
    elif h4l_house_no == 2:
        reading4_lines += [
            "With your 4th house lord placed in the 2nd house, the chart suggests a strong foundation of security and pride expressed through family and possessions. You are likely to own property and feel deeply rooted in your sense of belonging, which brings you courage and a certain dignity in how you carry yourself. A large family setup may play a significant role in your life, shaping both your responsibilities and your identity. This placement also gives you a magnetic personal charm, making you naturally attractive to others, though it may incline you toward indulgence in physical comforts and pleasures. Overall, this alignment ties your inner sense of home and stability with your material wealth and outward presence, blending family pride with sensual magnetism.",
        ]
    elif h4l_house_no == 3:
        reading4_lines += [
            "With your 4th house lord positioned in the 3rd house, your chart highlights a life of courage, talent, and generosity. You are inclined toward charitable deeds, and support often comes through helpers, attendants, or those who work under you. At the same time, your wealth and progress are largely self-made, flowing through your own personal efforts, skills, and initiatives rather than inheritance or easy gains. This placement also suggests that while you may rise on the strength of your will and determination, it can occasionally bring challenges or troubles for your parents, indicating a karmic balancing between your path of independence and your family ties.",
        ]
    elif h4l_house_no == 4:
        reading4_lines += [
            "With your 4th house lord placed in the 4th house, the chart promises a life anchored in steady comforts, domestic peace, and vast property or real estate. You carry a composed and clever temperament, with the ability to act in an advisory or ministerial capacity, well-informed and proud of your judgment. There is a natural attachment and loyalty to your spouse, which strengthens the foundation of home life. This placement also uplifts your father’s status and wealth, while simultaneously inclining you toward religious or spiritual pursuits, giving your life a sense of depth and higher purpose.",
        ]
    elif h4l_house_no == 5:
        reading4_lines += [
            "With your 4th house lord positioned in the 5th house, your chart reflects a natural love for comfort and beauty in life, along with a warm, approachable personality that makes you widely liked and socially admired. This placement strengthens your devotion to higher ideals and spirituality, often inclining you toward faith in God or righteous living. It also indicates that you are capable of creating your own sources of income through initiative and creativity, while enjoying the stability of a long life. Importantly, you may experience benefits, guidance, or support from your father, which adds to your overall prosperity and growth.",
        ]
    elif h4l_house_no == 6:
        reading4_lines += [
            "With your 4th house lord placed in the 6th house, it indicates challenges in enjoying the nurturing and protective comforts of home and mother. You may often feel deprived of maternal support or emotional grounding, which can make you restless and quick-tempered. This placement may also pull your mind into brooding tendencies and sometimes lead you toward morally questionable choices or adulterous inclinations. It suggests inner conflict between the desire for peace at home and the constant pull of disputes, debts, or adversities that the 6th house represents, making you prone to agitation and dissatisfaction in your domestic and emotional sphere.",
        ]
        # Conditional clause from the source:
        if is_h4_malefic_nat:
            reading4_lines.append("As a natural malefic 4th-lord in the 6th house, you may bring bad name to your father.")
        if is_h4_benefic_nat:
            reading4_lines.append("As a natural benefic 4th-lord in the 6th house, you are likely to accumulate wealth.")
    elif h4l_house_no == 7:
        reading4_lines += [
            "With your 4th house lord placed in the 7th house, you are naturally inclined towards acquiring knowledge in multiple fields, giving you versatility and depth in learning. However, this placement may also bring challenges with regard to ancestral or paternal property, suggesting a tendency to renounce, lose, or be distanced from such inheritance. While your mind is sharp and learned, you may sometimes struggle to project confidence or assertiveness in public assemblies or group settings, leading to a sense of restraint in openly expressing your ideas. Overall, this alignment creates a blend of intellectual richness with certain material sacrifices, and it calls upon you to balance personal confidence with your natural wisdom.",
        ]
    elif h4l_house_no == 8:
        reading4_lines += [
            "With your 4th house lord placed in the 8th house, the chart suggests that home comforts may feel lacking, and support from parents could be limited or strained. This placement often points to challenges around family stability and emotional security, sometimes reflecting a background that feels harsh or difficult. It may also indicate health vulnerabilities, risks to vitality including sexual weakness, and a tendency to struggle with inner contentment. The mind may be restless, carrying karmic burdens that can manifest as cruelty, sickness, or moral compromises. Such a position can suggest roots in humble or troubled circumstances, but it also points to a deep transformative journey—where hardship can push the native toward profound inner strength, resilience, and eventual spiritual growth if consciously worked upon.",
        ]
    elif h4l_house_no == 9:
        reading4_lines += [
            "With your 4th house lord placed in the 9th house, your chart suggests that you are likely to be beloved and well-provided for, enjoying material comforts and a sense of inner pride rooted in virtuous living. This placement often gives a strong moral compass and dignity in conduct. At the same time, it indicates limited support or presence of the father figure, either through physical distance or a sense of separation, though this may encourage you to cultivate independence and self-reliance. You are shown as a learned individual, with a natural inclination toward deeper knowledge and spiritual inquiry, often gravitating toward worship of Viṣṇu or higher divine principles. This combination ultimately points to a life where inner growth, faith, and wisdom take precedence over worldly attachments.",
        ]
    elif h4l_house_no == 10:
        reading4_lines += [
            "With your 4th house lord placed in the 10th house, you are someone whose sense of emotional security and roots naturally channel into your career and public life. This placement often makes you beloved and well-provided for, with an air of pride and virtue that shines through your professional conduct. You may find that while you receive little help from your father or remain physically distant from him, your life still unfolds with learning, refinement, and a strong moral compass. Spiritually, this combination inclines you toward devotion, especially in the path of Viṣṇu, where your work and dharma are not just means of livelihood but offerings aligned with divine order.",
        ]
    elif h4l_house_no == 11:
        reading4_lines += [
            "With your 4th house lord placed in the 11th house, you carry a natural generosity and a helpful spirit, often extending support to others and engaging in charitable deeds. This placement makes you capable, socially connected, and oriented toward community welfare. At the same time, it may expose you to some health vulnerabilities that require attention. Your devotion toward your father and respect for his role in your life remain strong, and you are inclined to perform virtuous and righteous works, finding fulfillment in noble actions that uplift both yourself and society.",
        ]
    elif h4l_house_no == 12:
        reading4_lines += [
            "With your 4th house lord placed in the 12th house, the foundation of home and inner security may feel fragile, often giving a sense of restlessness or even detachment from one’s homeland. This placement can sometimes point to difficulties in maintaining a stable domestic life, a tendency toward negligence or impractical habits, and a wandering or wayward conduct that makes it hard to feel rooted. There may be experiences of homelessness, frequent changes of residence, or living in places away from the comfort of your ancestral home. On the familial side, the chart shows a likelihood that the father may reside abroad or far from the native, creating a distance—geographical or emotional—that influences your inner life and sense of belonging.",
        ]

    reading4_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 4th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header4}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading4_lines)
        + "</div>"
    )

    # Mahadasha note for 4th-house lord
    md4 = _md_period_for(h4_lord_pid)
    md4_note_html = ""
    if md4:
        _s4, _e4 = md4
        md4_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h4_lord_name} is weak</p>"
        )
    weak4_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb4_val/1020)*100)}%</p>"
        )
    # Attach the MD line and the weakness note directly inside this block
    #reading4_html = reading4_html.replace("</div>", f"{md4_note_html}{weak4_note_html}</div>")
    
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
            "When the lord of your 5th house is placed in the 1st house, it gives you a natural sharpness of intellect and an eagerness to learn, which makes you shrewd and knowledgeable, and this contributes to your personal reputation and recognition in life. At the same time, this placement can create challenges connected with children or progeny, often bringing a sense of distance or lack of fulfillment from that area. It may also incline you toward spending or squandering resources that belong to others, making it important to handle shared finances carefully. Overall, this alignment shows a rise in personal standing through your intelligence and wisdom, but it also calls for awareness in how you deal with family comforts and wealth that isn’t your own.",
        ]
    elif h5l_house_no == 2:
        reading5_lines += [
            "With the 5th house lord placed in your 2nd house, your chart suggests a harmonious blend of creativity and prosperity. This placement indicates that you are likely to be blessed with many children or receive great joy and fortune through them. Financial stability and accumulation of wealth come naturally, and you may also gain fame or a good reputation within your community. Women, in particular, tend to be supportive or appreciative of your talents. There is a strong artistic streak in you—perhaps in music, performing arts, or other aesthetic expressions—that not only enhances your self-worth but could also be a source of income.",
        ]
    elif h5l_house_no == 3:
        reading5_lines += [
            "With your 5th house lord placed in the 3rd house, you’re likely to enjoy a good rapport with your siblings and may even be well-regarded by them. You have a persuasive charm and a clever way with words, though there can sometimes be a tendency toward behind-the-scenes gossip or backbiting. You come across as thrifty and self-focused, preferring to invest your time and resources wisely. Interestingly, your children may also play a supportive role in helping or bonding with your siblings, reflecting a unique intertwining of family dynamics rooted in communication and mutual assistance.",
        ]
    elif h5l_house_no == 4:
        reading5_lines += [
            "With your 5th house lord placed in the 4th house, your chart indicates that your intelligence, creativity, and past-life merits find expression through matters of home, emotional grounding, and maternal influence. You are likely to gain comfort and emotional fulfillment through your mother or domestic life, and may even follow an ancestral or maternal vocation. This placement bestows inner wisdom, strong memory, and a tendency toward roles that involve guidance, teaching, or advisory capacities. There's also a deep devotion to family roots and heritage, often manifesting as a desire to preserve traditions and offer counsel rooted in both experience and intuition.",
        ]
    elif h5l_house_no == 5:
        reading5_lines += [
            "With your 5th house lord placed in the 5th house, this forms a powerful and auspicious yoga that amplifies all significations of the 5th house—learning, creativity, children, intelligence, and personal merit. You are likely to be proud of your knowledge and talents, and may enjoy a natural flair for self-expression, education, and the arts. This placement often indicates virtuous tendencies, a refined intellect, and a strong capacity for gaining recognition through one’s skills or progeny. It also suggests a deep investment in your own growth and a karmic strength in areas related to education and legacy.",
        ]
        # Special condition from the source:
        if benefic_touches_5lord:
            reading5_lines.append("Your 5th house lord is under benefic influence which is favourable for obtaining progeny.")
        if malefic_touches_5lord:
            reading5_lines.append("Your 5th house lord is under malefic influence which creates risk of childlessness or poor comforts from progeny.")
    elif h5l_house_no == 6:
        reading5_lines += [
            "With your 5th house lord placed in the 6th house, your chart suggests potential challenges in areas related to children and creativity. There may be occasional conflicts with your children or issues related to their health. You might face a higher number of hidden enemies or competitors, and your own health could see fluctuations, especially due to stress or overexertion. This placement can also indicate difficulties in maintaining consistent financial stability, and at times, your status or recognition may not reflect your efforts. It is important to focus on self-care, manage financial planning carefully, and cultivate positive relationships with children and subordinates to counter these effects.",
        ]
    elif h5l_house_no == 7:
        reading5_lines += [
            "With your 5th house lord placed in the 7th house, you are likely to be a person of religious inclination and inner dignity, someone who extends help to others naturally. This placement often blesses you with noble associations—particularly a devout and virtuous spouse and sincere teachers who guide your path. You may also enjoy the joy of having sons, and your sense of service and ethics may shape your interactions and partnerships in life, fostering respectful and spiritually uplifting relationships.",
        ]
    elif h5l_house_no == 8:
        reading5_lines += [
            "With the 5th house lord placed in the 8th house in your chart, you may possess a short-tempered and blunt nature, often expressing yourself in a way that others may find harsh. This planetary placement tends to bring certain life challenges and hidden obstacles, especially concerning health and day-to-day struggles. There may be a tendency toward respiratory ailments, and care should be taken to maintain lung and throat health. It can also create hurdles in matters related to children or progeny, indicating some delays or difficulties in that area. This alignment often points to a karmic pattern of transformation through suffering, urging you to build resilience and inner strength over time.",
        ]
    elif h5l_house_no == 9:
        reading5_lines += [
            "With the lord of your 5th house placed in the 9th house, your chart suggests significant karmic rewards through service and perseverance. This placement indicates that your child is likely to attain high status and bring renown to the family. There is a strong indication of literary or artistic talent, possibly coupled with physical attractiveness and charm. You may find that such gifts are recognized and honoured by authoritative or institutional figures. This placement also hints at overcoming obstacles through higher knowledge, dharma, or guidance from a spiritual or paternal figure, ultimately elevating your and your family's standing.",
        ]
    elif h5l_house_no == 10:
        reading5_lines += [
            "With the 5th house lord placed in the 10th house, your chart indicates a rise to prominence that brings recognition akin to royalty. You are likely to gain fame and high social standing through diligent public service or virtuous deeds. This placement blesses you with material comforts and pleasures, suggesting a life of fulfillment and social respect. Professionally, it points to a strong work ethic and success in roles involving responsibility or societal impact. Moreover, this position subtly strengthens support toward the mother, either through emotional closeness or your ability to uplift her status through your achievements.",
        ]
    elif h5l_house_no == 11:
        reading5_lines += [
            "With your 5th house lord placed in the 11th house, your chart suggests a person of great learning and intellectual refinement, likely to accumulate significant wealth and achieve renown in their field. You may possess strong writing abilities and could be a skilled author or communicator. This placement often indicates a fruitful and active social network, with steadfast friendships and loyal companions who support your ambitions. You are likely to be courageous and resilient, facing challenges with bravery. The presence of royal comforts suggests you enjoy luxuries and status, and there is also a strong indication of being blessed with many sons or progeny, contributing to your legacy.",
        ]
    elif h5l_house_no == 12:
        reading5_lines += [
            "With your 5th house lord placed in the 12th house, your chart suggests a karmic pattern that may involve challenges related to children—either a delay in having children, denial of their comfort, or even childlessness in certain cases. At the same time, this placement strongly indicates a life connected with foreign lands—whether through long-distance travel, overseas residence, or spiritual retreats. This configuration often draws the native toward foreign service, charitable work, or healing professions abroad, though it may also bring hidden enemies and the need for spiritual detachment from material entanglements.",
        ]

    reading5_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 5th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header5}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading5_lines)
        + "</div>"
    )

    # Mahadasha note for 5th-house lord
    md5 = _md_period_for(h5_lord_pid)
    md5_note_html = ""
    if md5:
        _s5, _e5 = md5
        md5_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h5_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    weak5_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb5_val/1020)*100)}%</p>"
        )
    #reading5_html = reading5_html.replace("</div>", f"{md5_note_html}{weak5_note_html}</div>")
    
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
            "With your 6th house lord placed in the 1st house, you are likely to be someone who earns wealth through your own efforts and is known for your dependability, virtue, and courage. There may be recurring health concerns or a tendency toward bodily weakness, but these do not stop you from achieving prominence—your presence is noticed and respected. You may face opposition or tension with relatives or siblings, yet you have the strength to overcome adversaries and challenges. This placement gives you a proud demeanor, but it is backed by your solid reputation and personal victories.",
        ]
        if benefic_touches_6lord:
            reading6_lines.append("Your 6th house lord is under benefic influence, which indicates stability or improvement in health.")
    elif h6l_house_no == 2:
        reading6_lines += [
            "With your 6th house lord placed in the 2nd house, you are likely to gain name and respect within your family, known for your courage and persuasive speech. You may possess a strong sense of duty, though this can come with health challenges or responsibilities that weigh on you. This placement also favors financial gain and the ability to accumulate wealth through partnerships or spouse-related efforts. There’s a notable possibility of foreign residence or connections abroad that influence your material stability. Overall, your chart suggests a blend of familial prominence, eloquence, and financial acumen rooted in your relationships.",
        ]
    elif h6l_house_no == 3:
        reading6_lines += [
            "With your 6th house lord placed in the 3rd house, your relationships—especially partnerships—tend to intersect strongly with communication, courage, and siblings, but not always harmoniously. This placement can indicate tension or hostility with siblings, especially brothers, and a tendency toward impatience or a quick temper when dealing with close kin or co-workers. Your self-efforts may sometimes lack consistency or strength, leading to occasional setbacks or defeats in competitive situations. Additionally, there may be challenges in managing subordinates or helpers, with troublesome dynamics in those relationships surfacing from time to time. Overall, partnerships may demand more conscious effort in cultivating cooperation and emotional balance amidst familial or communicative tensions.",
        ]
    elif h6l_house_no == 4:
        reading6_lines += [
            "With your 6th house lord placed in the 4th house, your chart suggests a complex blend of personal and familial dynamics. You may experience emotional distance or limited comfort from your mother, and your temperament might tend toward brooding or inner hostility. There can be frequent mood swings and a fickle approach to relationships or decisions, yet despite these inner turmoils, you have the capacity to accumulate wealth. On the paternal side, the relationship may be strained, with potential for ongoing friction, and your father's health could be a recurring area of concern. Overall, the placement creates a push-pull between inner emotional roots and external partnerships.",
        ]
    elif h6l_house_no == 5:
        reading6_lines += [
            "With your 6th house lord placed in the 5th house, your chart suggests a dynamic interplay between partnerships and matters of love, creativity, and progeny. You may find that friendships and financial fortunes fluctuate, often affected by your emotional investments or relationships. There can be strain or karmic challenges involving children—either in conceiving, raising, or emotionally connecting with them. You come across as both kind and considerate, yet your actions may at times be perceived as self-centered, especially when driven by personal desire or romantic pursuits. A tendency to suffer due to issues related to children—whether through worry, detachment, or unmet expectations—might be a recurring theme, urging you to seek a balance between your own joy and the responsibilities you share with others.",
        ]
    elif h6l_house_no == 6:
        reading6_lines += [
            "With your 6th house lord placed in the 6th house, your relationships and partnerships—both personal and professional—tend to involve an element of conflict or challenge, possibly reflecting power struggles or debts (karmic or material) within partnerships. You may often find yourself more at ease or better supported by those outside your immediate community or background, while connections within your own circle can sometimes turn adversarial. Despite this, you maintain a generally good constitution and are likely to experience modest but steady financial gains, particularly through service, competition, or resolving disputes. Your approach to others is often humble, and your interactions, though tested, can lead to personal growth through resilience.",
        ]
    elif h6l_house_no == 7:
        reading6_lines += [
            "With the 6th house lord placed in your 7th house, your chart suggests a complex dynamic in relationships. You are likely to be courageous, virtuous, and potentially wealthy, but may experience challenges in marital harmony. There can be a sense of deprivation in terms of marital pleasures, and your spouse may display hostility or a short temper, leading to conflicts. Additionally, this placement can indicate possible concerns related to fertility. While your personal character and strength may thrive, partnerships may require conscious effort and understanding to sustain balance and harmony.",
        ]
    elif h6l_house_no == 8:
        reading6_lines += [
            "With the 6th house lord placed in the 8th house in your chart, you're likely to experience recurring health issues or a vulnerability to hidden ailments. This placement can indicate inner turbulence and a tendency to be at odds with virtuous or morally upright individuals, sometimes due to feelings of jealousy or rivalry. You may also find yourself coveting what belongs to others—be it their wealth, resources, or even intimate partners. This can lead to karmic entanglements and intense life lessons around desire, ethics, and control. There may also be tendencies toward habits or environments that are considered unclean or unhealthy, making self-discipline and spiritual purification especially important for your personal growth.",
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
            reading6_lines.append(f"According to classical texts, you may face survival threat from: {cod_txt}.")
    elif h6l_house_no == 9:
        reading6_lines += [
            "With your 6th house lord positioned in the 9th house, your life path reveals certain unique patterns and challenges. You may find yourself involved in professions related to wood, timber, or associated trades—careers that often involve physical materials and hands-on work. Financially, income may fluctuate, suggesting that consistency in earnings could be elusive unless managed wisely. There might be a tendency toward irreverence or nonconformity when it comes to traditional scriptures or belief systems, possibly indicating a questioning or rebellious attitude toward religious authority. Relations with brothers or male siblings might be strained or marked by conflict. On a more physical note, there may be indications of lameness or vulnerability to injuries affecting mobility, either literally or symbolically as obstacles in one's path.",
        ]
    elif h6l_house_no == 10:
        reading6_lines += [
            "With the 6th house lord placed in your 10th house, your chart suggests that you are likely to earn fame and recognition within your family and social circles, often seen as someone articulate and persuasive in speech. However, this placement also brings a sense of emotional distance from your father and possible opposition or tension with your mother. Interestingly, it points toward a life that finds ease and comfort in foreign lands—suggesting that you may flourish professionally or find peace while living or working abroad.",
        ]
    elif h6l_house_no == 11:
        reading6_lines += [
            "With the lord of your 6th house placed in the 11th house, your chart indicates a strong and courageous personality, someone who stands tall in the face of challenges and gains recognition and virtue through overcoming opposition. You may receive unexpected gains or support through adversaries or those you once considered enemies, suggesting a karmic pattern of turning rivalry into reward. There is also a risk of losses through theft or deceit, and potential dangers linked to hidden enemies—though these challenges ultimately serve as stepping stones to growth. Additionally, you may benefit materially or emotionally through associations with quadrupeds—such as cattle, pets, or animals in general—indicating a possible link to livelihood or wellbeing through such connections.",
        ]
    elif h6l_house_no == 12:
        reading6_lines += [
            "With your 6th house lord placed in the 12th house, your chart suggests karmic entanglements linked to debts, enmities, and past-life consequences. This placement often brings challenges with wise or scholarly individuals—there may be a tendency to oppose or misunderstand them. Resources may be wasted on unworthy or degrading activities, and actions may at times inadvertently cause harm to other living beings. Financial losses are possible, particularly due to large animals or quadrupeds. There is also an indication of a restless, wandering nature—one who may feel driven by fate or unseen forces, often surrendering to a fatalistic outlook on life.",
        ]

    reading6_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 6th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header6}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading6_lines)
        + "</div>"
    )

    # Mahadasha note for 6th-house lord
    md6 = _md_period_for(h6_lord_pid)
    md6_note_html = ""
    if md6:
        _s6, _e6 = md6
        md6_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h6_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    weak6_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb6_val/1020)*100)}%</p>"
        )
    #reading6_html = reading6_html.replace("</div>", f"{md6_note_html}{weak6_note_html}</div>")
    
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
            "With your 7th house lord positioned in the 1st house, you are likely to possess a naturally attractive and pleasure-seeking personality, often drawing others to you with charm and magnetism. This placement can indicate a strong attachment or dependency on your spouse or partner, often making your identity deeply tied to relationships. However, there may also be tendencies toward flirtation or adulterous inclinations. Your intellect is sharp, but may sometimes operate in ways that prioritize self-interest over scruples. Physically, this placement can predispose you to vāta-related imbalances—such as issues related to the nervous system, joints, or dryness in the body.",
        ]
    elif h7l_house_no == 2:
        reading7_lines += [
            "With your 7th house lord placed in the 2nd house, your relationships and partnerships strongly influence your resources and family life. This placement often indicates income and gains through spouse or business partners, yet the flow of wealth can be somewhat sluggish or delayed. Socially, it shows a tendency to attract multiple associations or opportunities for relationships, but paradoxically, you may practice restraint or abstinence despite these openings. Overall, this placement ties your marital or partnership karma directly to your financial stability and family bonds, bringing lessons of patience and measured action in both love and money.",
        ]
    elif h7l_house_no == 3:
        reading7_lines += [
            "Your 7th house lord is positioned in the 3rd house, indicating a blend of qualities that enhance your partnerships. This placement suggests you possess strong spiritual fortitude and an affectionate nature, which can foster deeper connections with others. However, it's important to be mindful of potential challenges, as there may be a risk of miscarriage for your wife. Balancing these influences will be key to nurturing both your relationships and family life.",
        ]
    elif h7l_house_no == 4:
        reading7_lines += [
            "Your 7th house lord is positioned in the 4th house, suggesting a personality that is both truthful and religious. However, there may be challenges in your marital relationships, as your spouse could be prone to infidelity. Additionally, this placement indicates potential dental issues. You may also find yourself entangled in matters concerning your father's adversaries, highlighting a connection between family dynamics and your partnerships.",
        ]
    elif h7l_house_no == 5:
        reading7_lines += [
            "With the 7th house lord positioned in the 5th house, you are likely to embody traits of wealth, pride, and virtue, leading to a contented life. This placement suggests that your spouse may benefit significantly from your relationship, as your son plays a pivotal role in taking care of her. Overall, this configuration indicates a harmonious balance between personal and familial responsibilities, enhancing your overall happiness.",
        ]
    elif h7l_house_no == 6:
        reading7_lines += [
            "Your 7th house lord positioned in the 6th house suggests challenges in your marital relationship. You may experience health issues related to your spouse, leading to mutual hostility and a quick temper between you. This placement indicates that you might suffer emotionally or physically due to your spouse's actions, contributing to an overall sense of misery within the partnership. It's essential to address these dynamics to foster a healthier relationship.Your 7th house lord positioned in the 6th house suggests challenges in your marital relationship. You may experience health issues related to your spouse, leading to mutual hostility and a quick temper between you. This placement indicates that you might suffer emotionally or physically due to your spouse's actions, contributing to an overall sense of misery within the partnership. It's essential to address these dynamics to foster a healthier relationship.",
        ]
    elif h7l_house_no == 7:
        reading7_lines += [
            "In your chart, the placement of your 7th house lord in the 7th house suggests that you are likely to have a good spouse who is learned and socially well-known. However, it's important to be aware that this placement may also indicate a susceptibility to Vāta-related health issues.",
        ]
    elif h7l_house_no == 8:
        reading7_lines += [
            "7th house lord in the 8th house suggests potential challenges in your marital life. Your spouse may face health issues or exhibit morally questionable behavior, which could lead to feelings of dissatisfaction or distress in the relationship. Furthermore, there is a possibility of separation or loss, indicating that the dynamics of your partnership may be strained. Be mindful of the potential for infidelity and the associated emotional turmoil that could arise from these circumstances.",
        ]
    elif h7l_house_no == 9:
        reading7_lines += [
            "7th house lord in 9th house indicates a strong and constant inclination toward women, suggesting that relationships play a significant role in your life. Your agreeable nature enhances your interactions, making you likable and approachable. Additionally, this configuration often brings fame or recognition, indicating that your partnerships may lead to opportunities for growth and visibility in your pursuits.",
        ]
    elif h7l_house_no == 10:
        reading7_lines += [
            "With your 7th house lord positioned in the 10th house, you exhibit a strong inclination towards religious beliefs and practices, which can bring both wealth and potential for progeny. However, this placement may also indicate challenges in your marital relationship, characterized by a spouse who may be disobedient or resistant to authority. Additionally, you may find a tendency towards sensuous indulgence, which could play a significant role in your personal life.",
        ]
    elif h7l_house_no == 11:
        reading7_lines += [
            "With your 7th house lord positioned in the 11th house, you can expect to earn through your spouse, indicating a strong financial partnership. Additionally, this placement suggests the likelihood of having more daughters in your family. Furthermore, your spouse is characterized by beauty and virtue, enhancing the overall harmony and attractiveness of your relationship.",
        ]
    elif h7l_house_no == 12:
        reading7_lines += [
            "With your 7th house lord positioned in the 12th house, you may experience challenges related to partnerships and relationships. This placement suggests potential financial difficulties or poverty, possibly linked to your spouse's influence. It indicates that you might engage in trade related to garments or textiles, which could be a source of income. However, it's important to be cautious, as this alignment may also lead to significant expenses through your spouse and potential feelings of being deceived or misled within your relationship.",
        ]

    reading7_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 7th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header7}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading7_lines)
        + "</div>"
    )

    # Mahadasha note for 7th-house lord
    md7 = _md_period_for(h7_lord_pid)
    md7_note_html = ""
    if md7:
        _s7, _e7 = md7
        md7_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h7_lord_name} is weak</p>"
        )

    # Attach the MD line and the weakness note directly inside this block
    weak7_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb7_val/1020)*100)}%</p>"
        )
    #reading7_html = reading7_html.replace("</div>", f"{md7_note_html}{weak7_note_html}</div>")
    
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
            "Your 8th house lord positioned in the 1st house suggests a potential reduction in physical comforts and an inclination toward irreverence regarding sacred traditions. This placement may make you accident-prone and more likely to engage in forbidden or risky behaviors, indicating a bold approach to relationships and personal expression, often challenging societal norms.",
        ]
    elif h8l_house_no == 2:
        reading8_lines += [
            "With the 8th house lord positioned in the 2nd house, you may experience a weak initiative in financial matters, which could lead to limited wealth accumulation. This placement suggests potential loss of savings and could indicate short-life possibilities. You might also face challenges such as a thievish streak and the presence of many enemies. Additionally, there is a risk of encountering punishment by authorities, emphasizing the need for caution in financial dealings and personal relationships.",
        ]
    elif h8l_house_no == 3:
        reading8_lines += [
            "With your 8th house lord positioned in the 3rd house, you may experience feelings of lethargy and weakness, which can affect your interactions in partnerships. Comfort from siblings might be lacking, leading to potential quarrels with friends or brothers. Additionally, your efforts in relationships may seem fickle or inconsistent, indicating a need for more stability and focus in your connections with others.",
        ]
    elif h8l_house_no == 4:
        reading8_lines += [
            "With your 8th house lord positioned in the 4th house, you may experience challenges in your relationships and partnerships due to potential deception among associates. This placement suggests a likelihood of feeling deprived of support from your mother, home, or property, leading to emotional disruptions. Additionally, you may encounter friction or conflict with your father, which could further complicate your domestic and relational dynamics.",
        ]
    elif h8l_house_no == 5:
        reading8_lines += [
            "With your 8th house lord positioned in the 5th house, it suggests potential challenges regarding progeny, indicating a limited number of children or difficulties in childbearing. However, this placement also offers opportunities for wealth accumulation and longevity in life. You may experience moments of dull or poor judgment, particularly in matters related to relationships or children. Additionally, there may be some troubles or complications following the birth of a child, which could require careful attention and management.",
        ]
    elif h8l_house_no == 6:
        reading8_lines += [
            "8th house lord in 6th house suggests that you may have experienced childhood ailments that could have affected your relationships. However, you possess the ability to ultimately overcome adversaries and challenges in your life. Additionally, there may be underlying anxieties or fears, particularly related to water and reptiles, that you will need to address as you navigate your partnerships and social interactions.",
        ]
        # Classical sub-conditions: nature of 8L when placed in 6H
        COD_8L_IN_6H = {
            const._SUN: "opposed to the ruler/state",
            const._MOON: "prone to lingering ailments",
            const._MARS: "quick-tempered and rash",
            const._MERCURY: "cowardly",
            const._JUPITER: "diseased/afflicted in your limbs",
            const._VENUS: "afflicted with eye disease",
            const._SATURN: "afflicted with diseases of the mouth/oral cavity",
        }
        _spec = COD_8L_IN_6H.get(h8_lord_pid)
        if _spec:
            reading8_lines.append(f"With this placement, you are likely to be {_spec}.")
    elif h8l_house_no == 7:
        reading8_lines += [
            "Your 8th house lord is positioned in the 7th house, suggesting potential for two marriages or significant alliances in your life. However, this placement may also indicate a predisposition to abdominal health issues and a tendency towards immoral conduct. It’s essential to be aware of these influences and navigate your relationships and health with mindfulness.",
        ]
        if has_malefic_assoc8:
            reading8_lines.append("However, the malefic association indicates losses in business and suffering caused by spouse.")
    elif h8l_house_no == 8:
        reading8_lines += [
            "When your 8th house lord is positioned in the 8th house, you can expect a unique blend of influences: your longevity and basic vitality are safeguarded, indicating a strong foundation for health and resilience. However, this placement also suggests a tendency towards craftiness or deceit, possibly in relationships or partnerships. Additionally, you may find yourself gaining fame or recognition through hidden or complex matters, highlighting your ability to navigate intricate situations with skill. Overall, this alignment speaks to a life marked by depth, transformation, and the potential for significant achievements in less conventional spheres.",
        ]
        if weak8:
            reading8_lines.append("However, since you have a weak 8th-lord, lifespan tends toward medium rather than long.")
    elif h8l_house_no == 9:
        reading8_lines += [
            "With your 8th house lord in the 9th house, you will tend to be atheistic or irreverent, and tend to covet others’ spouse and wealth. A tendency towards cruel acts is indicated, and spouse’s conduct is also likely to be problematic. High chance of oral-cavity ailments.",
        ]
    elif h8l_house_no == 10:
        reading8_lines += [
            "8th house lord in 10th house indicates a challenging dynamic regarding support from your father, manifesting as limited encouragement in your professional endeavors. You may find yourself lacking the motivation for sustained effort, leading to a tendency to serve under superiors without the autonomy you desire. This configuration suggests a potential struggle to assert your independence in your career, often feeling more like a subordinate than a leader.",
        ]
    elif h8l_house_no == 11:
        reading8_lines += [
            "With your 8th House Lord in the 11th House, challenges and difficulties may be experienced in your early life and the formative years. You will transition to improved prosperity and favorable outcomes as your life progresses. This placement suggests that while initial struggles may impact relationships and partnerships, there is a significant potential for growth and success in social networks and friendships as you mature.",
        ]
        if has_malefic_assoc8:
            reading8_lines.append("However, in your case, due to malefic association, poverty constraints are likely to persist.")
        if has_benefic_assoc8:
            reading8_lines.append("With benefic association, longevity is enhanced and gains stabilise.")
    elif h8l_house_no == 12:
        reading8_lines += [
            "With your 8th house lord situated in the 12th house, you may find yourself drawn to spending on pursuits that may be deemed immoral or indulgent. This placement can indicate tendencies towards cruel or harsh behavior in relationships, and it may also contribute to chronic ailments that affect your well-being. Additionally, there may be inclinations towards thievish behaviors, suggesting a need to be mindful of integrity in both personal and professional interactions.",
        ]

    reading8_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 8th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header8}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading8_lines)
        + "</div>"
    )

    # Mahadasha note for 8th-house lord
    md8 = _md_period_for(h8_lord_pid)
    md8_note_html = ""
    if md8:
        _s8, _e8 = md8
        md8_note_html = (
            f"<p class='text-left mt-2'><strong>"
            f"The above effects would be more prominent in the mahadasha of {h8_lord_name}:</strong> "
            f"{_s8:%Y-%m-%d} – {_e8:%Y-%m-%d}</p>"
        )

    # Weakness note for 8th-house lord (Avasthas & Śaḍbala)
    weak8_note_html = ""
    if weak8:
        weak8_note_html = (
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h8_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note inside this block
    weak8_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb8_val/1020)*100)}%</p>"
        )
    #reading8_html = reading8_html.replace("</div>", f"{md8_note_html}{weak8_note_html}</div>")
    
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
            "With your 9th House Lord in 1st House, you are likely to possess a wealth of knowledge and wisdom that sets you apart. Your charisma and appeal draw people towards you. Recognition and respect from those in positions of power come easily to you. A strong sense of luck and blessings follows you throughout life. You tend to have modest desires and needs. A deep respect and dedication to your mentors and spiritual guides are prominent in your life.",
        ]
    elif h9l_house_no == 2:
        reading9_lines += [
            "Your 9th house lord positioned in the 2nd house indicates a life characterized by sensuality and wealth. You are likely to be well-educated and appreciated by those around you, enjoying the company of a supportive spouse and children. However, be mindful of potential health issues related to the mouth or oral cavity, which may arise from time to time.",
        ]
    elif h9l_house_no == 3:
        reading9_lines += [
            "With your 9th house lord positioned in the 3rd house, it indicates you are likely to be very good-looking, wealthy, and virtuous. This placement suggests strong support from siblings and relatives, enhancing your social network. Additionally, it points to a spouse who possesses a pleasing appearance, contributing to a harmonious and attractive partnership.",
        ]
    elif h9l_house_no == 4:
        reading9_lines += [
            "Your 9th house lord positioned in the 4th house indicates a strong devotion to your mother and a potential for fame. This placement suggests that you may have a deep emotional connection to your home and family, often finding comfort and support from maternal figures. Additionally, you are likely to be prosperous, with the possibility of owning significant assets such as a house, land, and vehicles, which can enhance your sense of security and stability in life.",
        ]
    elif h9l_house_no == 5:
        reading9_lines += [
            "With your 9th house lord placed in the 5th house, you exhibit a strong devotion to preceptors and a deep inclination towards religious and philosophical pursuits. This placement indicates that you are likely well-learned and possess a wealth of knowledge. Additionally, you may experience fortune through your children, who are generally seen as virtuous and bring positive energy into your life. Your conduct is often characterized by moral integrity and a genuine commitment to higher ideals.",
        ]
    elif h9l_house_no == 6:
        reading9_lines += [
            "With your 9th House Lord in the 6th House, you may experience harassment or challenges from adversaries. Limited support or comfort may come from your maternal uncle. Despite facing adversity, you remain actively involved in religious and spiritual pursuits. Be mindful of your health, as it may be delicate or require attention. This configuration suggests a complex interplay of challenges and resilience in your life.",
        ]
    elif h9l_house_no == 7:
        reading9_lines += [
            "With your 9th house lord positioned in the 7th house, you are likely to attract a spouse who embodies truthfulness, beauty, and devotion. This alignment suggests a partnership characterized by overall virtuousness, enhancing both your personal values and relationship dynamics.",
        ]
    elif h9l_house_no == 8:
        reading9_lines += [
            "9th House Lord in the 8th House indicates a challenging period, often characterized by an unfortunate streak in life. You may find limited support or comfort from your elder brother, leading to feelings of isolation. Additionally, this position suggests a tendency to harm living beings, reflecting possible irreligious or transgressive behaviors that may impact your spiritual beliefs and values.",
        ]
    elif h9l_house_no == 9:
        reading9_lines += [
            "9th House Lord in the 9th House signifies a highly fortunate individual, characterized by attractiveness and virtue. You are likely to receive significant support from brothers, enhancing your overall luck and opportunities. Additionally, there is a strong inclination towards religious and philosophical pursuits, suggesting that spiritual growth and wisdom may play a vital role in your life journey.",
        ]
    elif h9l_house_no == 10:
        reading9_lines += [
            "Having your 9th house lord positioned in the 10th house indicates a life marked by virtue and a notable reputation, particularly in the eyes of authority figures. This placement often brings an elevated status in your career or public life, suggesting that your ethical principles and beliefs contribute positively to your professional standing. Additionally, you are likely to be actively religious and may find that your devotion extends to your parents, indicating a strong familial bond that influences your spiritual and professional pursuits.",
        ]
    elif h9l_house_no == 11:
        reading9_lines += [
            "With your 9th house lord positioned in the 11th house, you exhibit a pious and upright nature, which is complemented by a steady inflow of money. This placement indicates longevity and a life marked by religious activity, contributing to your wealth and fame within your community.",
        ]
    elif h9l_house_no == 12:
        reading9_lines += [
            "With your 9th house lord positioned in the 12th house, you may experience some misfortunes, particularly regarding your wealth, which might be directed towards religious deeds and charitable acts. However, this placement also brings honor in foreign lands, suggesting that you could achieve recognition internationally. Additionally, you are likely to be scholarly and possess good looks, enhancing your appeal and intellect as you navigate your journey.",
        ]

    reading9_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 9th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header9}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading9_lines)
        + "</div>"
    )

    # Mahadasha note for 9th-house lord
    md9 = _md_period_for(h9_lord_pid)
    md9_note_html = ""
    if md9:
        _s9, _e9 = md9
        md9_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h9_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    weak9_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb9_val/1020)*100)}%</p>"
        )
    #reading9_html = reading9_html.replace("</div>", f"{md9_note_html}{weak9_note_html}</div>")
    
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
            "10th House Lord in 1st House indicates that you are learned and virtuous, showcasing a strong moral compass. Although you experienced health challenges during childhood, your well-being improves significantly as you grow older. Financially, you can expect a steady upward trajectory in wealth over time. Your relationship with your father is marked by devotion, while you may encounter some friction with your mother, suggesting complex family dynamics.",
        ]
    elif h10l_house_no == 2:
        reading10_lines += [
            "With your 10th house lord positioned in the 2nd house, you are likely to be virtuous and financially prosperous, gaining respect and recognition from authority figures. Your charitable nature shines through, but you may experience tension in your relationship with your mother. Additionally, there may be an underlying acquisitive or avaricious streak that influences your approach to wealth and possessions.",
        ]
    elif h10l_house_no == 3:
        reading10_lines += [
            "With your 10th house lord positioned in the 3rd house, you exhibit a valiant and principled nature, complemented by your eloquence in communication. You are likely to receive support from siblings and colleagues, enhancing your professional endeavors. However, be mindful that your strong adherence to your principles may lead to conflicts with close relationships when those values are challenged.",
        ]
    elif h10l_house_no == 4:
        reading10_lines += [
            "With your 10th house lord positioned in the 4th house, you are likely to experience prosperity and virtue in your life. This placement suggests an affinity for lands, vehicles, and comforts, indicating that you may enjoy a comfortable lifestyle and material success. Additionally, your devotion to both parents is highlighted, suggesting strong familial ties and support that contribute to your overall well-being and achievements.",
        ]
    elif h10l_house_no == 5:
        reading10_lines += [
            "Your 10th house lord is positioned in the 5th house, indicating a strong connection between your career and personal interests. This placement supports wealth accumulation, joyful experiences with children, and a passion for learning. You are likely to find fulfillment in engaging with pious works and philanthropic activities. Additionally, you may receive favor from influential figures, enhancing your professional opportunities. Your appreciation for music and the arts suggests that creative pursuits will play a significant role in your life, enriching both your career and personal satisfaction.",
        ]
    elif h10l_house_no == 6:
        reading10_lines += [
            "With the lord of your 10th house positioned in the 6th house, you may find yourself facing challenges from rivals, resulting in feelings of harassment. While you possess significant skills, recognition and rewards may often elude you. Your relationship with your father may provide little comfort, potentially leading to a sense of emotional distance. Additionally, your temperament may lean towards being quarrelsome, and while your health is generally manageable, it requires attention to ensure you maintain your well-being in the face of these challenges.",
        ]
    elif h10l_house_no == 7:
        reading10_lines += [
            "When your 10th house lord is positioned in the 7th house, it indicates a favorable partnership with a spouse who is not only good-natured but also virtuous and thoughtful. This alignment suggests that your partner is likely to act in accordance with dharma, embodying principles of righteousness and ethical conduct in the relationship. This combination enhances the stability and harmony in your personal and professional life, as the qualities of your spouse positively influence your career and public image.",
        ]
    elif h10l_house_no == 8:
        reading10_lines += [
            "Your 10th house lord's placement in the 8th house indicates a blend of longevity and critical perspective towards others. You may possess a tendency to be cautious and hesitant when it comes to initiating new ventures, often preferring to analyze situations thoroughly before taking action. This placement may also suggest a propensity for harshness or unethical leanings in professional matters, urging you to be mindful of your approach in your career and interactions with authority figures.",
        ]
    elif h10l_house_no == 9:
        reading10_lines += [
            "When your 10th house lord is positioned in the 9th house, it indicates a strong potential for wealth and worthy progeny, suggesting that your career may not only bring financial success but also the possibility of raising children who are esteemed and accomplished. This placement can also signify royal favor or a status that is equal to that of a ruler, enhancing your social standing and influence. Additionally, you are likely to attract noble friends who can support and elevate your aspirations, further contributing to your overall success and fulfillment in both personal and professional realms.",
        ]
    elif h10l_house_no == 10:
        reading10_lines += [
            "Your 10th house lord is positioned in the 10th house, indicating a strong alignment with your career and public life. This placement signifies a truthful and highly capable individual who enjoys material comforts and holds an excellent reputation in their field. Additionally, it suggests a kind disposition towards your mother, reflecting positive familial relationships. Overall, your professional stature is robust, enhancing your ability to achieve success and recognition in your endeavors.",
        ]
    elif h10l_house_no == 11:
        reading10_lines += [
            "With your 10th house lord positioned in the 11th house, you are likely to experience an accumulation of wealth, children, and virtuous qualities throughout your life. This placement suggests a strong sense of truthfulness and contentment, indicating that you may enjoy a long life. Additionally, it points to a nurturing relationship with your mother, who plays a significant role in your well-being and success.",
        ]
    elif h10l_house_no == 12:
        reading10_lines += [
            "Your 10th house lord positioned in the 12th house indicates a clever yet anxious disposition. You may find yourself feeling intimidated by opponents, which can affect your professional confidence. Additionally, there may be significant expenses related to state or authority figures, suggesting a need to navigate these relationships carefully to maintain your career and public standing.",
        ]
        # Special condition from the source: if 10L is a natural malefic in 12H → foreign work/wandering
        NAT_MALEFICS_10 = {
            const._SUN, const._MARS, const._SATURN,
            getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)
        }
        if h10_lord_pid in NAT_MALEFICS_10:
            reading10_lines.append("Since your 10th house lord is a natural malefic, its placement in the 12th house indicates that you may have to wander or work in a foreign land.")

    reading10_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 10th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header10}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading10_lines)
        + "</div>"
    )

    # Mahadasha note for 10th-house lord
    md10 = _md_period_for(h10_lord_pid)
    md10_note_html = ""
    if md10:
        _s10, _e10 = md10
        md10_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h10_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    weak10_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb10_val/1020)*100)}%</p>"
        )
    #reading10_html = reading10_html.replace("</div>", f"{md10_note_html}{weak10_note_html}</div>")
    
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
            "With your 11th house lord positioned in the 1st house, you exhibit a wealthy disposition with a sattvic nature, often expressing yourself poetically and treating others with fairness. You can expect a steady inflow of money, showcasing your strong and brave character. However, some texts caution that this placement may come with a risk of a shorter lifespan.",
        ]
    elif h11l_house_no == 2:
        reading11_lines += [
            "With your 11th House Lord in 2nd House, you are likely to accumulate significant wealth and enjoy a life of comfort. Your financial pursuits may be complemented by a strong inclination towards spirituality and charitable activities. Be mindful of health issues, as there may be indications of a predisposition to sickness and potentially a shorter lifespan.",
        ]
    elif h11l_house_no == 3:
        reading11_lines += [
            "With your 11th house lord positioned in the 3rd house, you exhibit high efficiency in your endeavors and likely have many siblings or allies who support you. This placement enhances your ability to overcome adversaries, showcasing your resilience and strategic thinking. However, it's important to be mindful of your health, as you may be susceptible to abdominal complaints.",
        ]
    elif h11l_house_no == 4:
        reading11_lines += [
            "With your 11th house lord positioned in the 4th house, you can expect to gain wealth through your mother, as well as benefits from properties and land investments. This placement suggests a strong connection to your roots and heritage, often leading you to engage in pilgrimages. Additionally, it indicates longevity and a deep devotion to your father, highlighting your ability to act judiciously when the timing is right.",
        ]
    elif h11l_house_no == 5:
        reading11_lines += [
            "Your 11th house lord positioned in the 5th house suggests a life rich in learning and intellectual pursuits, often leaning towards religious or spiritual engagements. You are likely to enjoy a comfortable lifestyle, supported by your social networks and friendships. This placement indicates the potential for virtuous children and fosters harmonious relationships with your father, enhancing your overall sense of well-being and contentment in familial and creative endeavors.",
        ]
    elif h11l_house_no == 6:
        reading11_lines += [
            "With your 11th house lord positioned in the 6th house, you may experience challenges related to your social circles and aspirations. This placement suggests a tendency towards health issues, potential conflicts with adversaries, and difficulties stemming from powerful enemies. You might also find yourself dealing with harsh circumstances that could stem from these rivalries. Additionally, there may be ties to foreign lands or a residence that influences your social network and challenges, indicating that your friendships or ambitions may be affected by external factors or distance.",
        ]
        if is_h11_malefic_nat:
            reading11_lines.append("With a natural malefic as 11th-lord in the 6th, classical texts indicate threat to life in a foreign land at the hands of a thief.")
    elif h11l_house_no == 7:
        reading11_lines += [
            "Your 11th house lord positioned in the 7th house suggests a blend of virtue and sensuality in your relationships. You possess a generous nature and often find yourself yielding to the guidance of your spouse. This placement indicates potential gains through women in your life, alongside a promise of longevity and elevated status. Embrace these qualities to cultivate fulfilling partnerships and opportunities.",
        ]
    elif h11l_house_no == 8:
        reading11_lines += [
            "11th House Lord in 8th House indicates potential failures or challenges in professional and social spheres, suggesting a need for resilience and adaptability. A long lifespan is likely, but with health issues that may arise; taking proactive care of well-being is essential. There may be a possibility of the spouse predeceasing, indicating a need to cultivate strong emotional and support networks.",
        ]
    elif h11l_house_no == 9:
        reading11_lines += [
            "Your 11th house lord situated in the 9th house indicates a favorable position that brings blessings from those in power, suggesting you may find support from rulers or authority figures. This placement is also associated with wealth and a strong sense of truthfulness, highlighting your integrity and moral values. Furthermore, it suggests a deep commitment to learning, indicating that you are likely to be very educated and knowledgeable. Your devotion to religion and spiritual pursuits may also play a significant role in your life, enriching your experiences and guiding your actions.",
        ]
    elif h11l_house_no == 10:
        reading11_lines += [
            "Your 11th house lord positioned in the 10th house indicates a strong connection to authority and recognition. You are likely to be honored by those in power due to your self-control, truthfulness, and virtuous nature. You consistently follow your own dharma, contributing to a long and fulfilling life. Your devotion to your mother is notable, although you may experience some strain in your relationship with your father. Overall, this placement suggests a balance between personal aspirations and professional achievements, highlighting your commitment to your values.",
        ]
    elif h11l_house_no == 11:
        reading11_lines += [
            "With your 11th house lord positioned in the 11th house, you can expect significant gains from various endeavors, leading to potential fame through your knowledge and material possessions. This placement suggests longevity and the likelihood of having many sons and grandsons. Additionally, you are likely to possess a pleasant appearance, enhancing your social interactions and opportunities.",
        ]
    elif h11l_house_no == 12:
        reading11_lines += [
            "Your 11th house lord positioned in the 12th house indicates a strong connection with foreigners and outsiders, suggesting relationships and associations that may extend beyond your immediate environment. You may find comfort and sensual pleasure through interactions with multiple women. Financially, you are inclined to spend on religious endeavors, reflecting a desire for spiritual fulfillment, yet be cautious as this placement may also lead to engaging in questionable activities. Additionally, chronic ailments may be indicated, so it's advisable to prioritize your health and well-being.",
        ]

    reading11_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 11th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header11}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading11_lines)
        + "</div>"
    )

    # Mahadasha note (shown above weakness note)
    md11 = _md_period_for(h11_lord_pid)
    md11_note_html = ""
    if md11:
        _s11, _e11 = md11
        md11_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h11_lord_name} is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    weak11_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb11_val/1020)*100)}%</p>"
        )
    #reading11_html = reading11_html.replace("</div>", f"{md11_note_html}{weak11_note_html}</div>")
    
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
            "With your 12th house lord located in the 1st house, you may exhibit spend-thrift tendencies and have a weaker physique, which could expose you to risks of poverty. Your mental sharpness may not be at its peak, and indications suggest a likelihood of residing abroad. You are likely to possess a pleasant appearance, but there may be signs of being unmarried or experiencing impotency. Additionally, you could be prone to illnesses related to the Kapha dosha."
        ]
    elif h12l_house_no == 2:
        reading12_lines += [
            "With your 12th House Lord in the 2nd House, you exhibit a strong religious inclination and possess a sweet-tongued demeanor, often spending on good deeds. Financially, you tend to be comfortable, but you may harbor fears related to theft, fire, and authority figures."
        ]
    elif h12l_house_no == 3:
        reading12_lines += [
            "With your 12th House Lord in the 3rd House there is potential estrangement from brothers or living away from them, leading to a sense of independence. You will be often left to fend for oneself, fostering resilience and self-sufficiency. You may adopt a hostile stance toward others, which can affect social interactions. You emphasize caution in spending and financial matters."
        ]
    elif h12l_house_no == 4:
        reading12_lines += [
            "12th House Lord in 4th House indicates lack of land, home, and vehicles. Comforts from mother will be low or absent. Health issues are likely. There might be opposition and conflict with your own sons, leading to general unhappiness at home. Overall, the placement indicates a sense of general misery in domestic life."
        ]
    elif h12l_house_no == 5:
        reading12_lines += [
            "With your 12th house lord positioned in the 5th house, there is a tendency for expenditures related to children, suggesting a focus on their needs and well-being. However, this placement may also indicate challenges in having children or difficulties in pursuing education and learning opportunities. Additionally, there is a strong indication of potential spiritual journeys or pilgrimages that may play a significant role in your life, enriching your spiritual experience despite the challenges faced in the areas of creativity and parenthood."
        ]
    elif h12l_house_no == 6:
        reading12_lines += [
            "With your 12th house lord positioned in the 6th house, you may experience a range of challenges, including a tendency towards short temper and feelings of misery. This placement can suggest sinful tendencies and a hostility towards those close to you. Additionally, you might find yourself drawn to the company of others, potentially leading to addictive behaviors. It's worth noting that this alignment may also indicate a susceptibility to eye diseases."
        ]
        # Special classical clause: Venus as 12L in 6H → blindness
        if h12_lord_pid == const._VENUS:
            reading12_lines.append("Venus as 12th-lord in the 6th house indicates a risk of blindness.")
    elif h12l_house_no == 7:
        reading12_lines += [
            "With the 12th house lord positioned in the 7th house, you may experience significant financial expenditure through your spouse, leading to a sense of deprivation regarding marital comforts. This placement indicates potential weaknesses or dullness in relationships, possibly accompanied by wicked conduct or challenges stemming from your spouse. As a result, you may find yourself suffering due to these dynamics within the partnership."
        ]
    elif h12l_house_no == 8:
        reading12_lines += [
            "With your 12th house lord positioned in the 8th house, you are likely to possess pleasant speech and exhibit a range of good qualities. This alignment suggests a medium life span, and you may find yourself with a notable capacity to acquire wealth throughout your life."
        ]
    elif h12l_house_no == 9:
        reading12_lines += [
            "With your 12th house lord positioned in the 9th house, you may find that your self-serving tendencies can lead to friction in relationships with friends and mentors. However, this placement also suggests opportunities for pilgrimage or spiritual travel, potentially guiding you towards personal growth and deeper understanding through your experiences."
        ]
    elif h12l_house_no == 10:
        reading12_lines += [
            "With your 12th house lord positioned in the 10th house, you may experience limited comfort and support from your father, and there could be potential financial losses related to the state or authority figures. You tend to steer clear of engaging with others' spouses, and while you may face challenges, your ultimate focus lies in accumulating wealth, primarily for the benefit of your children."
        ]
    elif h12l_house_no == 11:
        reading12_lines += [
            "With your 12th house lord positioned in the 11th house, you are likely to experience a life marked by wealth and reputation. Despite this, you may encounter losses even when benefiting from wealth-yogas. Your long-lived nature and fame are notable, complemented by a truthful disposition that shapes your interactions and relationships."
        ]
    elif h12l_house_no == 12:
        reading12_lines += [
            "12th House Lord in the 12th House indicates spend-thrift tendencies. You will be quick to anger. Health Indicators are poor, and there will be tendency to be sickly and short-lived. Cares for cattle/livestock are indicated. Yet, you are likely to become well-known. This placement suggests a complex personality, marked by impulsive spending and emotional volatility, along with potential health challenges. However, it also indicates a nurturing affinity for animals and the potential for gaining recognition or fame in some capacity."
        ]

    reading12_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on 12th-house lord</h3>"
        f"<p class='text-left mb-1'><em>{header12}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading12_lines)
        + "</div>"
    )

    # Mahadasha note for 12th-house lord
    md12 = _md_period_for(h12_lord_pid)
    md12_note_html = ""
    if md12:
        _s12, _e12 = md12
        md12_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {h12_lord_name} is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    weak12_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb12_val/1020)*100)}%</p>"
        )
    #reading12_html = reading12_html.replace("</div>", f"{md12_note_html}{weak12_note_html}</div>")
    
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
            "With your Sun positioned in the 1st house, you may exhibit traits such as a proud and valiant demeanor, often standing tall among peers. However, this placement can also bring challenges, including a tendency towards a harsh or unyielding temper, leading to episodes of anger. Physically, you might notice scant body hair, and you may experience issues like dry eyes or weak vision. Additionally, you may lean towards a more indolent lifestyle, and your unforgiving nature can impact personal relationships.",
        ]
        # Special Lagna conditions for Sun in 1H
        if lagna_sign == 0:   # Aries (exaltation)
            reading_sun_lines.append("Sun in exalted Aries rising indicates poor vision.")
        if lagna_sign == 3:   # Cancer
            reading_sun_lines.append("Sun in Cancer rising likely to cause cataract.")
        if lagna_sign == 4:   # Leo (own sign)
            reading_sun_lines.append("Sun in Leo rising indicates strong constitution but night-blindness.")
        if lagna_sign == 6:   # Libra (debilitation)
            reading_sun_lines.append("Sun in debilitated Libra rising: risk of blindness, poverty and poor progeny comfort.")
        if lagna_sign == 11:  # Pisces
            reading_sun_lines.append("Sun in Pisces rising: served and attended by women is classically stated.")
    elif sun_house_no == 2:
        reading_sun_lines += [
            "With your Sun in the 2nd House, you may experience losses related to authority figures or the state, which could impact your financial stability. Additionally, there may be challenges with facial or dental health, as well as potential speech impediments. However, despite these difficulties, you possess a remarkable capacity for accumulating great wealth, suggesting that financial opportunities may still arise, provided you navigate the challenges wisely.",
        ]
    elif sun_house_no == 3:
        reading_sun_lines += [
            "Your Sun positioned in the 3rd house indicates a personality that is courageous, prosperous, and generous. You possess a strong character and a wealth of knowledge, which aids you in overcoming challenges and defeating adversaries. However, you might experience some discomfort or challenges in your relationships with siblings. Overall, your intellectual pursuits and communication skills shine brightly, showcasing your learned nature.",
        ]
    elif sun_house_no == 4:
        reading_sun_lines += [
            "With your Sun positioned in the 4th house, you may experience a sense of deprivation regarding home comforts, leading to weaker connections with relatives. This placement can also indicate potential challenges such as loss of land or property. Additionally, be mindful of your health, as you might be more prone to cardiac issues.",
        ]
    elif sun_house_no == 5:
        reading_sun_lines += [
            "With your Sun positioned in the 5th house, you may encounter challenges related to progeny, including potential difficulties in having children or even childlessness. This placement can also indicate concerns regarding longevity, suggesting a shorter lifespan than average. Financial worries or poverty may be prominent themes, adding to life's stress. While you possess wisdom, you might find yourself feeling like a wanderer, seeking fulfillment in diverse experiences. Additionally, this placement can be classically adverse for your first-born child, particularly if it is a son, highlighting the need for awareness in familial dynamics.",
        ]
    elif sun_house_no == 6:
        reading_sun_lines += [
            "Your Sun’s placement in the 6th house indicates a life marked by opulence, power, and significant wealth, often accompanied by fame. You are likely to experience victories in your endeavors, enjoying the favor of judicial or royal entities. This position also suggests a robust constitution with strong digestion and a hearty appetite, reflecting your overall vitality and well-being.",
        ]
    elif sun_house_no == 7:
        reading_sun_lines += [
            "Sun in the 7th House suggests challenges in relationships and partnerships, often leading to feelings of poverty or lack. You may experience humiliation or face unpleasant judgments from others, particularly women, which could foster antagonism. Health issues may also arise, reflecting the need for balance in your interactions. Additionally, there is a tendency towards transgressive behavior, urging you to examine your actions and their impact on your connections with those around you.",
        ]
    elif sun_house_no == 8:
        reading_sun_lines += [
            "Your Sun's placement in the 8th house suggests potential challenges in your life. You may experience loss of wealth and comforts, which could lead to financial instability. This position may also indicate having fewer children than desired, as well as a possibility of a shortened life span. Additionally, you might face estrangement from family members, which could create feelings of isolation. It's worth noting that this placement is also linked to health issues, particularly concerning eye diseases.",
        ]
    elif sun_house_no == 9:
        reading_sun_lines += [
            "With your Sun positioned in the 9th house, you can expect positive influences on wealth, friendships, and happiness, along with a strong connection to devotion, particularly towards deities and Brahmins. However, it's important to note that this placement may bring some challenges concerning your relationship with your father, as the Sun, which signifies fatherly energy, is located in the house associated with paternal matters.",
        ]
    elif sun_house_no == 10:
        reading_sun_lines += [
            "With your Sun positioned in the 10th house, you are seen as renowned and wise, exuding a powerful presence that commands respect. Wealth is likely to flow into your life, and you may find that your sons and relatives also prosper under your influence. You have a remarkable ability to complete undertakings successfully, making you appear unconquerable in your pursuits. Your status is akin to that of a king, highlighting your leadership qualities and the high regard in which you are held by others.",
        ]
    elif sun_house_no == 11:
        reading_sun_lines += [
            "With your Sun positioned in the 11th house, you are likely to experience significant wealth and power throughout your life. This placement signifies a natural efficiency in achieving your goals, allowing you to enjoy a diverse range of comforts and gains. Your social connections and friendships may also play a crucial role in your prosperity, enhancing your ability to navigate various opportunities with ease.",
        ]
    elif sun_house_no == 12:
        reading_sun_lines += [
            "Sun in the 12th House indicates potential for health issues, particularly those that may be hidden or chronic in nature. Increased susceptibility to eye-related health problems is likely. You will tend to stray from your true career path or purpose, possibly feeling lost or unfulfilled. You have a tendency for travel or restlessness, often seeking solitude or escape. It also indicates possible strained relationships with father figures, leading to conflicts or misunderstandings. This configuration suggests a complex interplay of challenges and lessons related to health, purpose, and familial connections.",
        ]

    reading_sun_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on the Sun</h3>"
        f"<p class='text-left mb-1'><em>{header_sun}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_sun_lines)
        + "</div>"
    )

    # Mahadasha timing note (Sun)
    md_sun = _md_period_for(sun_pid)
    md_sun_note_html = ""
    if md_sun:
        _sS, _eS = md_sun
        md_sun_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Sun is weak</p>"
        )

    # Attach MD line and the Sun-only weakness note inside this block
    weak_sun_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_sun_val/1020)*100)}%</p>"
        )
    #reading_sun_html = reading_sun_html.replace("</div>", f"{md_sun_note_html}{weak_sun_note_html}</div>")
    
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
                "Individuals with their Moon positioned in the 1st house may experience an unstable mind, which can lead to risks of derangement or sensory issues, such as deafness or mutism. This placement often correlates with a harsher temperament, and those affected might exhibit darker or more intense physical features, reflecting the complexities of their emotional landscape.",
            ]
            if moon_malefic_influence:
                reading_moon_lines.append("With malefic influence on Moon in Lagna: classic texts warn of shortened longevity.")
        # Special sign-specific clauses
        if lagna_sign == 0:   # Mesha/Aries
            reading_moon_lines.append("With Aries rising: many children are indicated.")
        if lagna_sign == 1:   # Vrisha/Taurus (exaltation)
            reading_moon_lines.append("With exalted Taurus rising: You are likely to be wealthy, famous and pleasant-looking.")
        if lagna_sign == 3:   # Karka/Cancer (own sign)
            reading_moon_lines.append("With Cancer rising (own sign): You are likely to be wealthy, famous and good-looking.")
        if is_fullish:
            reading_moon_lines.append("Full Moon in Lagna: You are likely to be fearless, wealthy and long-lived.")
    elif moon_house_no == 2:
        reading_moon_lines += [
            "Moon in the 2nd House indicates a person with a sweet and pleasant manner of speaking, which attracts wealth and comforts into their life. They are likely to have a fondness for women and may enjoy a large family. However, they tend to be sparing with their words, choosing to speak thoughtfully rather than excessively.",
        ]
    elif moon_house_no == 3:
        reading_moon_lines += [
            "Moon in the 3rd House signifies a person who is virtuous and brave, often demonstrating courage in their endeavors. They tend to enjoy strong support from their siblings, fostering close familial bonds. Additionally, education plays a significant role in their life, highlighting a thirst for knowledge and intellectual growth.",
        ]
    elif moon_house_no == 4:
        reading_moon_lines += [
            "You are generally a happy individual, though you may exhibit a certain level of detachment in your emotional expressions. Your experiences have made you learned and sensuous, allowing you to appreciate the finer aspects of life. Additionally, you have a fondness for water sports and travel, reflecting your connection to both adventure and emotional depth.",
        ]
    elif moon_house_no == 5:
        reading_moon_lines += [
            "Your Moon's placement in the 5th house highlights a strong connection to children, wealth, and learning, indicating that these areas of your life are positively influenced and supported. However, this placement also suggests a tendency towards timidity, which may affect your self-expression and confidence in these domains. Embrace your nurturing qualities while working to overcome any hesitations that may arise.",
        ]
    elif moon_house_no == 6:
        reading_moon_lines += [
            "With your Moon placed in the 6th house, your chart suggests certain challenges and sensitivities that shape both body and mind. This position can indicate a somewhat delicate constitution, sometimes pointing to abdominal or digestive complaints, and a tendency to become easily angered or emotionally unsettled. The Moon here also shows that life may present hidden or open opposition, with troubles from adversaries or competitors surfacing from time to time. Spiritually, this placement calls for the cultivation of patience and inner balance, since the fluctuations of the Moon in a house of conflict can shorten vitality if left unchecked. At its higher potential, it urges you to rise above irritation and maintain calm resilience in the face of obstacles, thus transforming opposition into growth.",
        ]
    elif moon_house_no == 7:
        reading_moon_lines += [
            "With your Moon placed in the 7th house, relationships and partnerships take on a central role in your life. This position blesses you with charm, good looks, and a pleasing personality that naturally draws others toward you. Your spouse is likely to be attractive and refined, adding grace and beauty to your married life. At the same time, the 7th house Moon also heightens emotional and sensual desires, creating a strong sexual appetite and a deep need for intimacy and companionship. Because of this restless emotional nature, there may be a tendency to seek variety or to wander in relationships if deeper fulfillment is lacking. Ultimately, your growth comes from learning balance—channeling your emotional intensity into building harmonious, loyal, and meaningful bonds rather than scattering your energies.",
        ]
    elif moon_house_no == 8:
        reading_moon_lines += [
            "Moon in the 8th House suggests a personality that is both wise and fickle, showcasing a depth of intuition paired with a tendency for emotional volatility. Individuals may experience a predisposition to health issues, making them more disease-prone. Additionally, there are indications of a potentially shortened longevity, emphasizing the need for self-care and awareness of health.",
        ]
    elif moon_house_no == 9:
        reading_moon_lines += [
            "With your Moon positioned in the 9th house, you exhibit a sense of duty and responsibility, particularly in areas related to comfort, wealth, and learning. This placement suggests a nurturing approach towards education and a strong inclination towards exploring philosophical or spiritual beliefs. You may find joy in nurturing children and are likely to be admired by women for your wisdom and depth of understanding. Your emotional fulfillment may often be tied to the pursuit of knowledge and the sharing of your insights with others.",
        ]
    elif moon_house_no == 10:
        reading_moon_lines += [
            "Your Moon's placement in the 10th house indicates a personality that is both wealthy and pious, reflecting a strong moral compass in your pursuits. You are efficient in your endeavors, showcasing a powerful and liberal approach to your responsibilities. This placement suggests that you have a tendency to complete undertakings thoroughly, ensuring that no detail is overlooked in your quest for success and fulfillment. Embrace these traits as they guide you toward achieving your goals.",
        ]
    elif moon_house_no == 11:
        reading_moon_lines += [
            "With your Moon positioned in the 11th house, you are likely to experience a life marked by wealth and fame. Your bravery and thoughtfulness stand out, making you a natural leader among peers. This placement suggests blessings in the form of sons and indicates a long and fulfilling life. Embrace your social connections, as they play a crucial role in your success and well-being.",
        ]
    elif moon_house_no == 12:
        reading_moon_lines += [
            "With your Moon placed in the 12th house, you may often feel withdrawn or indolent, as though life’s energy flows more inward than outward. This placement can sometimes bring humiliation, hidden sorrows, or a sense of misery that tests your resilience, and in difficult times may even lead to moral lapses if not consciously guarded against. The eyes and sleep patterns may show vulnerability, hinting at the need for care in these areas. Yet, the 12th house is also the house of foreign lands, so the possibility of residence abroad or significant connections in distant places is strong, offering a way for you to transform isolation into new horizons of experience and growth.",
        ]

    reading_moon_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on the Moon</h3>"
        f"<p class='text-left mb-1'><em>{header_moon}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_moon_lines)
        + "</div>"
    )

    # Mahadasha timing note (Moon)
    md_moon = _md_period_for(moon_pid)
    md_moon_note_html = ""
    if md_moon:
        _sM, _eM = md_moon
        md_moon_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Moon is weak</p>"
        )

    # Attach MD line and Moon-only weakness note
    weak_moon_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_moon_val/1020)*100)}%</p>"
        )
    #reading_moon_html = reading_moon_html.replace("</div>", f"{md_moon_note_html}{weak_moon_note_html}</div>")
    
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
            "With Mars placed in your 1st house, your personality carries a fiery, restless, and daring edge—often making you bold, impulsive, and sometimes even dare-devilish in how you approach life. This placement gives you a natural vigor and assertiveness, but it can also manifest as impatience, a tendency to rush headlong into challenges, and being prone to accidents or injuries if energy is not channeled constructively. Health may need special attention, as vitality can fluctuate, making the body feel more delicate than it appears. At times, life may feel like it runs at a faster, more intense pace, giving the impression of a shortened span, yet this intensity also fuels the courage and drive that make you stand out.",
        ]
    elif mars_house_no == 2:
        mars_lines += [
            "Mars in the 2nd House suggests challenges in both learning and achieving stable wealth, potentially indicating a tendency to struggle with financial matters. You may experience issues related to your oral health or teeth, requiring attention. Additionally, there could be a tendency to drift away from good company, which might affect your social life and support system. A roaming nature is also highlighted, suggesting a desire for exploration and change that may impact your sense of stability.",
        ]
    elif mars_house_no == 3:
        mars_lines += [
            "With Mars placed in your 3rd house, you carry a bold and valiant spirit that thrives on courage and initiative. This placement gives you an indomitable quality—hard to defeat in challenges, you stand tall with strength, resilience, and a natural inclination for upstanding conduct. Your willpower and determination allow you to confront difficulties head-on, often emerging victorious where others falter. At the same time, Mars here may indicate stress or challenges in relation to younger siblings, where conflicts or burdens could weigh on family ties. Overall, this position blesses you with a warrior-like energy that empowers you to excel through valor, clarity of action, and personal integrity.",
        ]
    elif mars_house_no == 4:
        mars_lines += [
            "With Mars positioned in your 4th house, you may experience challenges related to home, land, financial resources, maternal support, and close friendships. Despite these potential deprivations, your courage and resilience remain notably high, enabling you to navigate these difficulties with strength and determination.",
        ]
    elif mars_house_no == 5:
        mars_lines += [
            "With Mars positioned in your 5th house, you may experience an unsettled and unrighteous streak, leading to potential challenges in your relationships with children, financial stability, and alliances. This placement suggests a risk of fewer comforts derived from these areas, which may contribute to a lack of mental peace. It’s essential to be mindful of how this energy manifests in your life, as it can impact your joy and creativity.",
        ]
    elif mars_house_no == 6:
        mars_lines += [
            "Mars is in the 6th House. You possess an exceptional level of energy and motivation, enabling you to tackle daily tasks with enthusiasm. Your strong constitution allows you to take on challenges, both physically and mentally, with a robust ability to process and digest experiences. In competitive situations, you have a natural edge, often outperforming rivals and overcoming obstacles with ease. Your assertiveness shines through, making you a natural leader who can command respect and inspire others in your professional and personal life.",
        ]
    elif mars_house_no == 7:
        mars_lines += [
            "With Mars positioned in your 7th house, you may experience a harsh temperament that could lead to conflicts in relationships. This placement suggests potential health issues and a risk of separation or challenges concerning a spouse. Additionally, you might have a slim frame, and be aware that there may be a tendency for financial struggles, which could result in quarrels. It's essential to navigate these dynamics with care to foster healthier relationships.",
        ]
    elif mars_house_no == 8:
        mars_lines += [
            "With your Mars placed in the 8th house, your chart shows a life-path that pulls you into deep, transformative experiences where both vulnerability and resilience are tested. This position can manifest as recurring challenges with health, vitality, and longevity, suggesting periods of physical strain or sudden injuries. It also indicates a magnetic draw toward subjects and experiences that others may consider taboo, hidden, or even forbidden—this can range from exploring the mysteries of life and death to involvement in unconventional relationships or pursuits. Mars here makes you fearless in confronting the darker realities of existence, but at the same time it may bring turbulence, sudden losses, or crises that force growth. While suffering and trials are possible, these experiences ultimately forge inner strength and the ability to navigate life’s most difficult transitions with courage and intensity.",
        ]
    elif mars_house_no == 9:
        mars_lines += [
            "With Mars placed in your 9th house, your path in life is marked by a fiery and rebellious approach to dharma and higher principles—you may often challenge traditional beliefs or established norms. This placement can create a tendency towards conflicts in matters of faith, philosophy, or ethics, and at times it may even signal treachery or situations where you face harm through ideological clashes. It also brings stress and challenges related to your parents, particularly the father, either through strained relationships or health concerns. Yet, this energy is not without its rewards: despite the turbulence, you can attract strong patronage and support from authority figures or institutions, who recognize your courage and conviction, ultimately helping you rise through struggle.",
        ]
    elif mars_house_no == 10:
        mars_lines += [
            "Mars in the 10th House indicates a formidable and liberal individual, characterized by courage and a strong presence in public life. You may enjoy a status akin to royalty, often garnering fame and high regard from those around you. Your assertive nature and ambition drive you toward success in your career and public endeavors.",
        ]
    elif mars_house_no == 11:
        mars_lines += [
            "Mars in the 11th House indicates a strong potential for financial growth and success through social networks and friendships, and reflects a courageous and ambitious nature, driving you to pursue your goals with determination. It also suggests an assertive personality, particularly in intimate relationships, where passion and boldness are prominent, and highlights the ability to achieve your dreams and aspirations, often fueled by your social interactions and alliances.",
        ]
    elif mars_house_no == 12:
        mars_lines += [
            "With Mars positioned in your 12th house, you may often feel a tendency toward harsh or impulsive behavior that can sometimes come across as repulsive to others, creating friction in close relationships and even posing risks to marital harmony. This placement also signals hidden struggles—there may be dangers of confinement, whether through circumstances, health issues, or inner turmoil, leading at times to periods of misery or isolation. Physically, the 12th house influence of Mars can manifest in eye-related troubles or ailments connected to vitality being drained in unseen ways. This is a placement that calls for conscious self-awareness, restraint, and channeling your Martian energy into constructive, spiritual, or service-oriented pursuits to neutralize its harsher effects.",
        ]

    reading_mars_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Mars</h3>"
        f"<p class='text-left mb-1'><em>{header_mars}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in mars_lines)
        + "</div>"
    )

    # Mahadasha note for Mars
    md_mars = _md_period_for(const._MARS)
    md_mars_note_html = ""
    if md_mars:
        _sm, _em = md_mars
        md_mars_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Mars is weak</p>"
        )

    # Attach MD line and the weakness note inside this block
    weak_mars_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_mars_val/1020)*100)}%</p>"
        )
    #reading_mars_html = reading_mars_html.replace("</div>", f"{md_mars_note_html}{weak_mars_note_html}</div>")
    
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
            "With Mercury positioned in your 1st house, you are blessed with a healthy constitution and an ability to articulate your thoughts clearly and pleasantly. Your sharp intellect shines through, particularly in mathematical and logical pursuits, indicating a strong affinity for analytical thinking. Additionally, you possess a scholarly inclination, suggesting a deep interest in scriptural studies or academic endeavors. This placement also hints at the potential for a long life, enhancing your overall vitality and mental acuity throughout the years.",
        ]
    elif merc_house_no == 2:
        reading_mercury_lines += [
            "With Mercury positioned in your 2nd house, you possess polished speech and a refined way of expressing yourself. Your education and intellect are key assets, contributing to a sense of wealth, both materially and intellectually. You have a strong appreciation for fine meals and comforts, indicating that you not only value quality in your surroundings but also seek to indulge in life's pleasures. This placement suggests a harmonious blend of communication skills and a love for the finer things, enhancing your overall life experience.",
        ]
    elif merc_house_no == 3:
        reading_mercury_lines += [
            "Mercury in the 3rd House indicates a restless and variable disposition, making you a dynamic thinker and communicator. You are likely a hands-on hard worker, capable of tackling tasks with enthusiasm. However, be mindful of the potential for craftiness or deception in your interactions. You may also find yourself drawn to occult or magical interests, exploring the mysteries of life. Additionally, your relationships with siblings are generally supportive, providing a solid foundation for your intellectual pursuits.",
        ]
    elif merc_house_no == 4:
        reading_mercury_lines += [
            "Your placement of Mercury in the 4th house indicates a strong inclination towards knowledge and learning, which is further enhanced by the support of wealth and material comforts, including vehicles and a cozy home environment. This position also suggests that you will cultivate good friendships and build a strong social network centered around your home base, enriching your personal life and fostering a sense of community.",
        ]
    elif merc_house_no == 5:
        reading_mercury_lines += [
            "With Mercury positioned in the 5th house, you are likely to find recognition through your learning and skills, particularly in areas involving mantra or technical proficiency. This placement suggests that many children may be indicated in your life, and you are characterized by a courageous spirit while generally maintaining a sense of contentment.",
        ]
    elif merc_house_no == 6:
        reading_mercury_lines += [
            "You possess a sharp and argumentative nature, often excelling in debates and frequently coming out on top against your opponents. However, this quick-tempered disposition may lead to moments of indolence, making you susceptible to ailments. Additionally, your focus on personal challenges can sometimes leave you less supportive of your close relations, as your attention may be drawn more towards your own battles.",
        ]
    elif merc_house_no == 7:
        reading_mercury_lines += [
            "This placement indicates a person who is knowledgeable, wise, and reputable in their interactions. Your spouse is likely to be resourceful and may possess wealth, enhancing the partnership with their practical skills and financial acumen.",
        ]
    elif merc_house_no == 8:
        reading_mercury_lines += [
            "With your Mercury placed in the 8th house, you carry a destiny marked by resilience and transformation. Despite obstacles or sudden challenges, you are likely to gain name and recognition, often remembered for how you navigate difficulties with intellect and adaptability. This placement gives you indications of longevity and an ability to endure through life’s storms, sharpening your wisdom over time. Mercury here also bestows a natural talent for analysis, mediation, and resolving conflicts—whether through judicial, arbitration, or advisory roles—making you someone who can uncover hidden truths and bring clarity where others find only confusion.",
        ]
    elif merc_house_no == 9:
        reading_mercury_lines += [
            "PYour Mercury in the 9th house indicates a prosperous and learned individual, characterized by eloquence and cleverness. You possess a natural inclination towards virtue and uphold a strong sense of law-abiding principles, enriching your interactions and pursuits with wisdom and integrity.",
        ]
    elif merc_house_no == 10:
        reading_mercury_lines += [
            "With Mercury positioned in your 10th house, you are recognized for your accomplishments and efficiency in your career. Your professional conduct is marked by righteousness, earning you respect and admiration in your field. You are well-known for your skills, which contribute significantly to your reputation and success.",
        ]
    elif merc_house_no == 11:
        reading_mercury_lines += [
            "This placement suggests a long life characterized by truthfulness and a strong intellectual capacity. You may find yourself experiencing wealth and recognition in your pursuits, along with a penchant for sensual enjoyments. Embrace your ability to connect with others intellectually, as it can lead to fulfilling relationships and opportunities for growth.",
        ]
    elif merc_house_no == 12:
        reading_mercury_lines += [
            "With Mercury placed in your 12th house, your mind tends to operate in a more private, hidden, or contemplative sphere. This often makes you appear somewhat withdrawn, austere, or indifferent to outer appearances, which can sometimes be perceived as unappealing on the surface. Yet beneath this quiet exterior lies sharp intelligence and a gift for learning. You may prefer seclusion, research, or spiritual study to the bustle of everyday chatter, but when you do speak, your words carry sweetness and refinement, leaving a lasting impression. This placement grants you the ability to balance austerity with eloquence, giving you an unusual depth—someone who may be overlooked at first glance, but whose inner wisdom and thoughtful communication reveal a truly dignified presence.",
        ]

    reading_mercury_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Mercury</h3>"
        f"<p class='text-left mb-1'><em>{header_mercury}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_mercury_lines)
        + "</div>"
    )

    # Mahadasha window for Mercury
    md_mer = _md_period_for(merc_pid)
    md_mer_note_html = ""
    if md_mer:
        _sm, _em = md_mer
        md_mer_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Mercury is weak</p>"
        )

    # Attach MD line and weakness note inside this block
    weak_mer_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_merc/1020)*100)}%</p>"
        )
    #reading_mercury_html = reading_mercury_html.replace("</div>", f"{md_mer_note_html}{weak_mer_note_html}</div>")
    
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
            "You are characterized by your intelligence and fearlessness, which contribute to a long and fulfilling life. Your balanced perspective allows you to navigate challenges with grace, while your appearance is often regarded as handsome. Additionally, there are strong indications of wealth in your life, suggesting not only financial success but also a richness in experiences.",
        ]
    elif j_house_no == 2:
        reading_j_lines += [
            "With your Jupiter positioned in the 2nd house, you are likely to experience a wealth of benefits in your life. This placement suggests that you may enjoy financial prosperity and abundance, complemented by your eloquent communication skills. Your pleasant appearance adds to your charm, making you socially appealing. You have a taste for good food and enjoy indulging in culinary delights, reflecting your appreciation for life's pleasures. Additionally, your helpful and liberal nature indicates a willingness to assist others, further enhancing your social standing and creating opportunities for personal growth and success.",
        ]
    elif j_house_no == 3:
        reading_j_lines += [
            "Your Jupiter placement suggests that you may experience challenges in your relationships with siblings and your spouse, leading to feelings of being subdued or troubled. However, this placement also indicates resilience, as you have the ability to overcome opponents despite these challenges. You might find that your digestion is somewhat sluggish, which could affect your overall well-being. On a positive note, this position of Jupiter offers strong support for your siblings, highlighting the importance of family connections in your life.",
        ]
    elif j_house_no == 4:
        reading_j_lines += [
            "With Jupiter positioned in your 4th house, you can expect a strong focus on comforts and wealth, bringing abundance to your home life. This placement enhances your connections with family and loved ones, fostering a nurturing environment filled with wise counsel. You are likely to enjoy the benefits of vehicles and travel, contributing to your overall sense of comfort and security. Additionally, Jupiter here may help you overcome adversaries, leading to a content and attractive demeanor that draws others to you. Embrace the blessings of this placement, as it signifies a harmonious and prosperous domestic life.",
        ]
    elif j_house_no == 5:
        reading_j_lines += [
            "With Jupiter placed in your 5th house, you carry the mark of wisdom and fortune in the sphere of creativity, learning, and progeny. This placement bestows you with knowledge, virtue, and a sense of dignity that can elevate you to fame, wealth, and even positions of advisory or ministerial authority. You are naturally inclined toward higher learning, spiritual pursuits, and guiding others, which makes your words and counsel carry weight. Prosperity tends to flow toward you, but life may test you through matters related to children—either through responsibilities, concerns, or emotional strain in that area. Overall, this is a powerful placement that makes you learned, respected, and capable of influencing others positively, while also calling you to grow stronger through your experiences with progeny.",
        ]
    elif j_house_no == 6:
        reading_j_lines += [
            "You may exhibit a tendency towards laziness, but you possess the ability to overcome adversaries despite this. Pay attention to your health, as you may experience digestive issues that require care. You may find yourself easily influenced or dominated by others, particularly in personal relationships. Your reputation may precede you, leading to significant recognition and fame in your endeavors. You could experience physical frailty, coupled with strong desires, which may need to be balanced for overall well-being.",
        ]
    elif j_house_no == 7:
        reading_j_lines += [
            "Your Jupiter placement in the 7th house signifies a life marked by learning and recognition, suggesting that you will achieve a level of success that surpasses that of your father. This position also brings blessings in your personal life, indicating that you are likely to have a supportive and loving spouse, along with children who enhance your joy and fulfillment. Overall, this placement highlights a harmonious balance between personal relationships and personal growth.",
        ]
    elif j_house_no == 8:
        reading_j_lines += [
            "This placement suggests a life characterized by a servile tone, indicating challenges in personal expression and a tendency towards feeling miserable. Your livelihood may primarily come through service to others, reflecting a focus on support roles. It is important to be mindful of unclean habits that could impact your overall well-being. Additionally, there is an indication of an adulterous streak, which may affect personal relationships. On a more positive note, this placement also suggests the potential for a long life, offering a silver lining amidst the complexities of your experiences.",
        ]
    elif j_house_no == 9:
        reading_j_lines += [
            "You are characterized by a deep devotion and extensive knowledge, which contribute to your wealth and fame. Your life is blessed with children, and you may find yourself in a leadership or ministerial role, guiding others with your wisdom and insight.",
        ]
    elif j_house_no == 10:
        reading_j_lines += [
            "Your placement of Jupiter in the 10th house signifies a strong inclination towards successfully completing your undertakings. This position endows you with wisdom, allowing you to navigate your career and public life effectively. You are likely to attract wealth and possess a sense of virtue that enhances your reputation and leadership potential. Embrace these qualities to achieve your professional aspirations and make a positive impact in your field.",
        ]
    elif j_house_no == 11:
        reading_j_lines += [
            "With Jupiter positioned in your 11th house, you can expect a life marked by wealth, stability, and longevity. This placement suggests that you may enjoy financial success and a strong support network, but it also indicates that you may have fewer sons than average. Embrace the abundance and connections that come your way while recognizing the unique family dynamics that may arise.",
        ]
    elif j_house_no == 12:
        reading_j_lines += [
            "Your Jupiter's placement in the 12th house suggests a tendency towards indolence and a lack of decisiveness, which may lead to moral compromises. This position can indicate a servile nature, where you might find yourself overly accommodating to others at the expense of your own needs. Additionally, there are indications of challenges regarding progeny, suggesting potential difficulties or delays in having children. Embrace self-awareness and consider ways to cultivate personal growth and resilience.",
        ]

    reading_jupiter_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Jupiter</h3>"
        f"<p class='text-left mb-1'><em>{header_j}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_j_lines)
        + "</div>"
    )

    # Mahadasha note for Jupiter
    md_j = _md_period_for(j_pid)
    md_j_note_html = ""
    if md_j:
        _sj, _ej = md_j
        md_j_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the Jupiter is weak</p>"
        )

    # Attach MD line and the weakness note directly inside this block
    weak_j_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_j_val/1020)*100)}%</p>"
        )
    #reading_jupiter_html = reading_jupiter_html.replace("</div>", f"{md_j_note_html}{weak_j_note_html}</div>")
    
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
            "You possess an attractive presence that draws others to you, making you naturally romantic and charming. Your education enhances your allure, and you approach life with a generally contented attitude. This positive outlook contributes to a sense of fulfillment, and you may find that you enjoy a long and prosperous life.",
        ]
    elif v_house_no == 2:
        v_lines += [
            "Venus in 2nd house bestows wealth, grace, refined speech and poetic or creative talent.",
        ]
    elif v_house_no == 3:
        v_lines += [
            "You are characterized by a desire for material possessions, yet you possess the financial means to fulfill these cravings. Your financial decisions may often be influenced or managed by your spouse, indicating a reliance on their judgment. Additionally, you tend to avoid strenuous ventures, preferring a more relaxed approach to challenges and opportunities that come your way.",
        ]
    elif v_house_no == 4:
        v_lines += [
            "With Venus positioned in your 4th house, you are likely to enjoy a harmonious home environment filled with beautiful ornaments and stylish clothing, along with a fondness for quality vehicles. Your pleasing appearance enhances your charm, but you may also possess a boastful streak that surfaces from time to time. Additionally, you have a tendency to yield to your spouse's desires, indicating a strong connection and willingness to support their needs within the relationship.",
        ]
    elif v_house_no == 5:
        v_lines += [
            "With Venus positioned in your 5th house, you are likely to experience an abundance of wealth and sensuality that enhances your status. Your comforts are magnified, and you find joy in your relationships with children and friends, who provide additional support. This placement also suggests that you possess a pleasing appearance that draws others to you, enriching your social interactions and personal experiences.",
        ]
    elif v_house_no == 6:
        v_lines += [
            "With Venus in your 6th house, you may face a few open enemies and experience themes of poverty or misery in your life. However, this placement also indicates that you will likely have many romantic ties. On the downside, you might find little joy from your spouse, and your overall reputation could suffer as a result.",
        ]
    elif v_house_no == 7:
        v_lines += [
            "With Venus positioned in your 7th house, you embody a passionate and charismatic presence that can often lead to quarrels in relationships. Your charming appearance draws others to you, but be mindful of your associations, as you may attract alluring individuals who may not have the best intentions. Embrace your passionate nature while remaining aware of the potential challenges in your partnerships.",
        ]
    elif v_house_no == 8:
        v_lines += [
            "Your life is likely to be filled with abundance and wealth, suggesting a long-lasting comfort. You are destined to enjoy numerous luxuries and pleasures that enhance your quality of life. Your presence may command respect and admiration, resembling the dignity and grace associated with royalty. A general sense of satisfaction and emotional well-being permeates your experiences, fostering a positive outlook on life..",
        ]
    elif v_house_no == 9:
        v_lines += [
            "With Venus positioned in your 9th house, you are likely to embody a learned and wealthy persona, attracting abundance through your pursuit of knowledge and higher ideals. Your relationships, including those with your spouse, children, and friends, are marked by harmony and comfort, suggesting a supportive and nurturing environment. Additionally, this placement indicates a strong inclination towards spirituality or religious beliefs, enriching your life with deeper meaning and connections.",
        ]
    elif v_house_no == 10:
        v_lines += [
            "With Venus positioned in your 10th house, you are likely to experience high status, significant influence, and the potential for wealth in your career. Your public image is notably strengthened, enhancing your reputation and appeal in professional settings. Additionally, you may find that women play a crucial role in your success, providing valuable support and assistance throughout your journey.",
        ]
    elif v_house_no == 11:
        v_lines += [
            "Your placement of Venus in the 11th house suggests a strong potential for affluence and financial gain through social connections and friendships. You may find yourself drawn to partnerships with women, potentially leading to relationships that are unconventional or outside societal norms. Additionally, this positioning indicates a capacity for alleviating personal pains and miseries, possibly through supportive networks or engaging in enjoyable social activities that enhance your overall well-being.",
        ]
    elif v_house_no == 12:
        v_lines += [
            "With Venus positioned in the 12th house, you may experience a tendency towards indolence and a potential fall from your personal standards. However, this placement also indicates that you possess a remarkable skill in matters of love, suggesting a deep, intuitive understanding of romantic relationships. It's important to be mindful of the potential for debauchery, as this may manifest in your interactions or desires, urging you to seek balance and moderation in your pursuits of pleasure.",
        ]

    reading_venus_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Venus</h3>"
        f"<p class='text-left mb-1'><em>{header_v}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in v_lines)
        + "</div>"
    )

    # Mahadasha note for Venus
    md_v = _md_period_for(v_pid)
    md_v_note_html = ""
    if md_v:
        _sv, _ev = md_v
        md_v_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Venus is weak</p>"
        )

    # Attach MD line + weakness note within this block
    weak_v_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_v_val/1020)*100)}%</p>"
        )
    #reading_venus_html = reading_venus_html.replace("</div>", f"{md_v_note_html}{weak_v_note_html}</div>")
    
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
                "With Saturn positioned in your 1st house, this placement is exceptionally auspicious, bestowing you with a strong sense of status and authority akin to that of a king or headman. You are likely to experience a long life characterized by virtue and a commitment to scholarship, suggesting that your journey will be marked by wisdom and moral integrity. Embrace this powerful influence as it shapes your identity and life path.",
            ]
        else:
            s_lines += [
                "Difficult ascendant placement of Saturn indicates misery, lethargy, lust, poor health, poor looks, risk of bodily defects and bad odour.",
            ]
    elif s_house_no == 2:
        s_lines += [
            "In the early phases of life, you may experience a sense of want and express yourself with harsh speech, which could lead to issues related to the mouth or throat. However, as you progress into later years, you are likely to leave your birthplace and find success in accumulating wealth, possessions, and comforts, indicating a significant transformation in your financial and material circumstances.",
        ]
    elif s_house_no == 3:
        s_lines += [
            "With Saturn positioned in your 3rd house, you may exhibit a morally rough or slothful streak, yet you possess a remarkable physical strength. Your personality shines with liberal thoughts, wisdom, and a tendency towards financial success, suggesting a blend of practical insights and potential challenges in communication and relationships with siblings or neighbors. Embrace your strengths while working on overcoming any tendencies towards laziness, as your wisdom can lead you to wealth and personal growth.",
        ]
    elif s_house_no == 4:
        s_lines += [
            "Saturn in the 4th house creates stress related to mother or home and distance from the near and dear. Nevertheless you are likely to be wise and wealthy. Childhood/early sickness is indicated.",
        ]
    elif s_house_no == 5:
        s_lines += [
            "With Saturn positioned in your 5th house, you may experience mental unrest and instability, leading to feelings of unhappiness and denial, particularly concerning children, personal comforts, and the pursuit of wisdom. However, despite these challenges, you possess the resilience and capability to overcome any opponents that may arise in your path. Embrace the journey of self-discovery, as it can ultimately lead to growth and fulfillment.",
        ]
    elif s_house_no == 6:
        s_lines += [
            "Saturn in 6th house bestows wealth, strong appetite, libido and pleasing looks. Yet, you may face harassment by enemies.",
        ]
    elif s_house_no == 7:
        s_lines += [
            "Your Saturn placement in the 7th house suggests potential challenges in relationships, including chronic health issues and themes of poverty. You may find that your spouse experiences health problems, which can create additional strain in your partnership. Additionally, there might be an underlying sense of uncleanness or a general aversion to others, impacting your social interactions and how you connect with people around you.",
        ]
    elif s_house_no == 8:
        s_lines += [
            "Your Saturn in the 8th house indicates a journey that begins with strength and determination, showcasing heroic qualities and a forceful approach to challenges. However, be mindful as this placement suggests a potential decline in power and financial stability as time progresses. Additionally, there is a noted risk for perianal disease. On a positive note, despite these challenges, Saturn in the 8th house is traditionally viewed as supportive for overall health and longevity, offering a balanced perspective on your life’s trajectory.",
        ]
    elif s_house_no == 9:
        s_lines += [
            "Saturn in the 9th House indicates a tendency towards an irreligious outlook, often leading to philosophical skepticism. You may encounter periods of misfortune and financial challenges throughout your life. Relations with your father may be strained or unfavorable, impacting your personal development. Additionally, there is a potential for your words or actions to unintentionally hurt those around you, suggesting a need for mindfulness in your interactions.",
        ]
    elif s_house_no == 10:
        s_lines += [
            "You are characterized by a learned and knowledgeable nature, often leading to wealth and power in your professional life. Your placement suggests potential for judicial or leadership roles, where your proud and heroic bearing can shine. Embrace these attributes as they may guide you toward significant accomplishments in your career.",
        ]
    elif s_house_no == 11:
        s_lines += [
            "Saturn in 11th house signifies a stable reputation and a strong social network, contributing positively to your overall standing in the community. You are likely to enjoy good health and experience significant wealth accumulation throughout your life. Additionally, there is a notable emphasis on sensuality, suggesting a deep appreciation for life's pleasures. Furthermore, this positioning indicates longevity, reflecting a robust constitution and the potential for a long, fulfilling life.",
        ]
    elif s_house_no == 12:
        s_lines += [
            "With Saturn positioned in your 12th house, you may experience challenges such as eye troubles and tendencies toward wasteful spending. This placement can lead to shameless conduct and personal suffering, yet it also endows you with a unique ability to demonstrate leadership even in harsh or difficult situations. Embrace the lessons from these challenges to foster growth and resilience in your life.",
        ]

    reading_saturn_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Saturn</h3>"
        f"<p class='text-left mb-1'><em>{header_s}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in s_lines)
        + "</div>"
    )

    # Mahadasha note for Saturn
    md_s = _md_period_for(s_pid)
    md_s_note_html = ""
    if md_s:
        _ss, _es = md_s
        md_s_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Saturn is weak</p>"
        )

    # Attach MD line + weakness note within this block
    weak_s_note_html = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_s_val/1020)*100)}%</p>"
        )
    #reading_saturn_html = reading_saturn_html.replace("</div>", f"{md_s_note_html}{weak_s_note_html}</div>")
    
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
            "Your placement of Rahu in the 1st house indicates a personality marked by a harsh and unyielding streak, often leading to turbulent experiences with wealth. This positioning suggests themes of shorter longevity, as well as a bold demeanor that can sometimes come across as cruel, with little compassion for others. You may also notice roughness in your hair, nails, and overall appearance, and there could be potential health concerns focused on the upper body.",
        ]
        # Mesha/Karka/Siṁha special
        if lagna_sign in {0, 3, 4}:
            r_lines.append("Because Lagna is Aries, Cancer, or Leo: pleasures and affluence are specifically indicated despite the harsh tone.")
        # Benefic aspect on Lagna
        if _benefic_aspects_asc():
            r_lines.append("Benefic aspect on the ascendant adds comforts and enjoyments, softening Rahu’s edge.")
    elif r_house_no == 2:
        r_lines += [
            "Your Rahu placement in the 2nd house suggests a potential for quarrels and themes related to poverty, along with tendencies towards theft-like behaviors. You may find yourself relying on patronage or authority figures for your income. Communication may be unclear or carry double meanings, and you might experience issues related to your mouth or teeth. Additionally, your career or trades may be connected to unconventional commodities such as skins or fish.",
        ]
    elif r_house_no == 3:
        r_lines += [
            "Rahu in 3rd house indicates you will be wealthy, valiant, proud, long-lived and enjoy the comforts of spouse, friends and other pleasures. However, tension with brothers is also indicated.",
        ]
        # Rahu exaltation (Vr̥ṣabha/Taurus)
        if r_sign == 1:
            r_lines.append("Rahu in Taurus indicates gains in vehicles and attendants.")
    elif r_house_no == 4:
        r_lines += [
            "Rahu in the 4th House indicates destitution and folly, reduced lifespan, loss of wealth and home comforts and conflict with spouse around domestic matters. You may experience challenges related to your emotional security and home life. This placement can indicate potential hardships, such as financial instability or a sense of disconnection from your domestic comforts. Additionally, it may lead to conflicts with your spouse, particularly concerning household issues, which could further impact your sense of stability and well-being. Be mindful of these influences as you navigate domestic affairs and focus on fostering harmony in your personal relationships to mitigate the effects.",
        ]
    elif r_house_no == 5:
        r_lines += [
            "With Rahu positioned in your 5th house, you may exhibit an irascible temperament, leading to impulsiveness. This placement suggests a propensity for taking risks, particularly concerning children, which may evoke concerns about their well-being. While you possess a compassionate nature, it is often tempered by underlying phobias that can affect your relationships and interactions. Additionally, be mindful of potential abdominal disorders, as this placement may indicate vulnerabilities in that area of health.",
        ]
    elif r_house_no == 6:
        r_lines += [
            "Rahu in 6th house indicates that while you will be troubles by enemies yet you will overcome them. Wealth, children and many comforts will show up. You are likely to be tempted for adultery, and may be afflicted by perianal disease. Yet, a longer life indicated.",
        ]
    elif r_house_no == 7:
        r_lines += [
            "Rahu in the 7th House indicates potential challenges in relationships, often leading to loss or difficulties associated with women. You may encounter issues such as adulterous ties and the possibility of separation from a spouse, which can leave you feeling bereft. While this position may suggest a tendency towards wickedness, it also highlights a brave nature. Additionally, be mindful of chronic ailments that could arise, emphasizing the need for self-care and attention to health in your partnerships.",
        ]
    elif r_house_no == 8:
        r_lines += [
            "Individuals with Rahu positioned in the 8th house may experience shorter longevity patterns, potentially leading to health concerns. They may exhibit vāta disorders, which could indicate issues related to the nervous system or digestive health. This placement suggests a likelihood of having few children, and it may also be associated with emotional challenges, including feelings of misery paired with fearlessness. Additionally, there could be a tendency towards perianal diseases and overall lethargy, impacting their energy levels and vitality.",
        ]
    elif r_house_no == 9:
        r_lines += [
            "With Rahu positioned in your 9th house, you may exhibit traits akin to a leader or headman, often taking charge and guiding others. However, this placement can lead to conflicts with paternal figures, as you might find yourself opposing your father or fatherly influences. Your expression can be perceived as harsh or cruel at times, which may alienate those around you. Additionally, you may feel harried by opponents, facing challenges from rivals or adversaries in your pursuits.",
        ]
    elif r_house_no == 10:
        r_lines += [
            "Rahu in the 10th House indicates a fearless individual who is helpful and may achieve fame. You possess a sensual nature but could be prone to engaging in unlawful ventures. Your potential for learning is significant, and with it comes the possibility of wealth, making you an excellent advisor, akin to a minister. However, there is a sense of detachment and a tendency towards wandering, suggesting a complex relationship with your ambitions and responsibilities.",
        ]
    elif r_house_no == 11:
        r_lines += [
            "With Rahu positioned in the 11th house, you may experience a strong inclination towards wealth and longevity. Your chart suggests you might have fewer children, along with a combative spirit tempered by self-control. You are likely perceived as handsome and succinct in communication. Your interests may lean towards scriptural studies, and there is an indication of potential foreign residence. Additionally, be mindful of ear disorders that could arise.",
        ]
    elif r_house_no == 12:
        r_lines += [
            "With Rahu positioned in the 12th house, you may experience a loss of comforts and financial stability, alongside challenges to your sense of virtue. This placement can indicate tendencies toward immorality and secretive behaviors, potentially leading to fickleness in relationships and chronic health issues. Additionally, you may be prone to water-borne diseases and could find yourself drawn to foreign lands or living away from your homeland.",
        ]

    reading_rahu_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Rahu</h3>"
        f"<p class='text-left mb-1'><em>{header_r}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in r_lines)
        + "</div>"
    )

    # Mahadasha note for Rahu
    md_r = _md_period_for(r_pid)
    md_r_note_html = ""
    if md_r:
        _sr, _er = md_r
        md_r_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            "<p class='text-left mt-2'><strong>Note:</strong> "
            "The above predictions may not manifest very strongly, since the Rahu is weak</p>"
        )

    # Attach MD line + weakness note within this block
    weak_r_note_html = ""
    #reading_rahu_html = reading_rahu_html.replace("</div>", f"{md_r_note_html}{weak_r_note_html}</div>")
    
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
            "Ketu in 1st house bestows harsh temperament with courage, very little compassion and unattractive appearance and nails/hair. Upper-body ailments are possible; wealth can come despite a rough edge.",
        ]
        # (i) Benefic aspect on Lagna
        if _benefic_aspects_house_local(0):
            reading_ketu_lines.append("Ketu has benefic aspect on the ascendant: comforts and enjoyments are indicated.")
        # (ii) In Lagna in a Saturn sign
        if asc_sign in SATURN_SIGNS:
            reading_ketu_lines.append("Ketu is placed in a Saturnic ascendant: You are likely to enjoy wealth and children.")
    elif k_house_no == 2:
        reading_ketu_lines += [
            "Ketu in 2nd house makes you quarrelsome and sharp-tongued and creates risk of unclear speech or mouth/teeth issues. You are likely to face poverty or dependence on patrons, although gains may come through authorities or unusual trades (skins, fish, etc.).",
        ]
        # Benefic sign condition
        if _sign_is_of_benefic(k_sign):
            reading_ketu_lines.append("Because it is in a sign of a natural benefic: physical comforts are improved.")
    elif k_house_no == 3:
        reading_ketu_lines += [
            "Ketu in 3rd house makes you wealthy, valiant and proud. You are likely to have a long life althoguh conflict with siblings is possible. You may enjoy vehicles and servants.",
        ]
    elif k_house_no == 4:
        reading_ketu_lines += [
            "Ketu in 4th house indicates loss of mother’s support and homely comforts, financial strain and frequent relocations. It also indicates opposition to spouse and inclination to spread malicious talk.",
        ]
    elif k_house_no == 5:
        reading_ketu_lines += [
            "You may experience intense fears, particularly related to water, which can manifest as a phobia. There could be a tendency toward abdominal health concerns, warranting attention to your diet and stress management. You may find that learning comes with some difficulties, possibly indicating a need for alternative educational approaches. There might be challenges or delays related to having children, suggesting a need for patience and introspection in matters of family planning.",
        ]
    elif k_house_no == 6:
        reading_ketu_lines += [
            "With Ketu positioned in your 6th house, you possess the ability to conquer enemies and experience good health, accompanied by a generous spirit. Your intellectual pursuits are marked by sharp erudition, allowing you to excel in various fields. However, be mindful of potential humiliation stemming from dealings with a maternal uncle. On a positive note, you may find financial gains through livestock or quadrupeds, enhancing your overall prosperity.",
        ]
    elif k_house_no == 7:
        reading_ketu_lines += [
            "Ketu in the 7th House suggests challenges in marital comfort and relationships, often leading to wandering tendencies and poor judgment when selecting partners. You may experience losses connected to women, along with potential health issues related to intestinal or seminal disorders. Additionally, there may be feelings of humiliation and a fear associated with water, which could manifest in various aspects of life.",
        ]
        # Exaltation note (Vrischika/Scorpio)
        if k_sign == 7:  # Scorpio
            reading_ketu_lines.append("In Scorpio in the 7th: multiple material benefits are classically indicated.")
    elif k_house_no == 8:
        reading_ketu_lines += [
            "With Ketu positioned in the 8th house, you may experience challenges such as perianal disease and potential separation from close relationships. There is also an increased risk of danger from weapons or accidents. This placement can bring forth tendencies toward avarice and immorality, indicating a need for self-awareness and caution. Additionally, your health may be delicate, necessitating attention to both physical and emotional well-being.",
        ]
        # Wealth-gain signs in the 8th (Mesha, Vrisha, Mithuna, Kanya, Vrischika)
        if k_sign in {0, 1, 2, 5, 7}:
            reading_ketu_lines.append("In this sign in the 8th: gains of wealth are indicated despite the harsh significations.")
    elif k_house_no == 9:
        reading_ketu_lines += [
            "Individuals with Ketu positioned in the 9th house often exhibit a blend of short-temperedness and eloquence in communication. They harbor a strong desire for progeny but may experience conflicts with their father and find limited support from siblings. However, their fortunes tend to improve significantly through assistance from foreigners or those who do not share their beliefs.",
        ]
    elif k_house_no == 10:
        reading_ketu_lines += [
            "You possess a strong presence that can lead to authority and recognition in your professional life. You have a knack for overcoming adversaries, often emerging victorious in conflicts. There is a profound inner spirituality or gnostic understanding that guides your actions and decisions. You may experience limited comfort or support from your father, which could influence your emotional landscape. A tendency to explore or change directions in your life path may arise, leading to diverse experiences. Your physical appearance might come across as severe or unappealing to others, reflecting your inner focus and detachment from superficial concerns.",
        ]
        # Extra classical note (strong placements like Mesha, Vrisha, Kanya, Vrischika are combative) already implicit.
    elif k_house_no == 11:
        reading_ketu_lines += [
            "With Ketu positioned in your 11th house, you are characterized by a valiant and powerful nature, coupled with virtuous qualities. Your learned disposition enhances your appeal, making you good-looking and charismatic. However, you may experience subtle fears that could influence your social interactions. While children might present some challenges, your efforts are likely to lead to significant gains, particularly in your aspirations and friendships. Embrace your strengths while navigating the complexities of this placement.",
        ]
    elif k_house_no == 12:
        reading_ketu_lines += [
            "Ketu in the 12th House suggests a tendency toward secretive misconduct, which may manifest in hidden activities or behaviors. Be mindful of potential health issues, particularly related to the legs, feet, anal region, and eyes. You possess a strong ability to triumph in conflicts, yet this victory often comes with a propensity to spend generously on charitable causes. However, your resolve may be inconsistent, leading to fluctuations in your commitments and decisions.",
        ]

    reading_ketu_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Reading based on Ketu</h3>"
        f"<p class='text-left mb-1'><em>{header_ketu}</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {line}</p>" for line in reading_ketu_lines)
        + "</div>"
    )

    # Mahadasha line for Ketu
    mdK = _md_period_for(KETU)
    mdK_note_html = ""
    if mdK:
        _sK, _eK = mdK
        mdK_note_html = (
            f"<p class='text-left mt-2'><strong>"
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
            f"<p class='text-left mt-2'><strong>Note:</strong> "
            f"The above predictions may not manifest very strongly, since the {ketu_name} is weak</p>"
        )

    # Attach MD line and weakness note directly to Ketu block
    weakK_note_html = ""
    #reading_ketu_html = reading_ketu_html.replace("</div>", f"{mdK_note_html}{weakK_note_html}</div>")
    
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
        return (f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val/1020)*100)}%</p>"
        )

    def _md_note_for(pid: int, pname: str) -> str:
        md = _md_period_for(pid)
        if not md:
            return ""
        s, e = md
        return (f"<p class='text-left mt-2'><strong>"
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
            if   n == 1:  lines += ["You will go on long travels, indulge in pleasures, and frequently get involved in conflict."]
            elif n == 2:  lines += ["You will enjoy servants or support staff, have a dignified bearing and will be favoured by authorities."]
            elif n == 3:  lines += ["You crave riches (especially gold), and are chaste. You will command/control people."]
            elif n == 4:  lines += ["You may bring harm to your mother"]
            elif n == 5:  lines += ["You could experience trouble due to daughters, although many sons indicated."]
            elif n == 6:  lines += ["You will conquer enemies and work for warriors or authority in kṣatriya-related contexts."]
            elif n == 7:  lines += ["You will have a beautiful spouse. Your conduct will be good. You will be honoured by rulers. You may experience ascetic leanings."]
            elif n == 8:  lines += ["You are likely to stir strife and could be prone to ailments."]
            elif n == 9:  lines += ["You have a religious bent and are truthful, but could suffer due to relatives."]
            elif n == 10: lines += ["You are likely to be exceptionally wealthy and praised by the affluent."]
            elif n == 11: lines += ["You will enjoy royal dignity. You will be multi-skilled, famous and head of family."]
            elif n == 12: lines += ["You may experience eye-related troubles."]

        # ——— MARS from Moon ———
        elif pid == const._MARS:
            if   n == 1:  lines += ["You may have reddish eyes or complexion. Bleeding wounds are indicated."]
            elif n == 2:  lines += ["You are likely to own land and obtain a son inclined to agriculture."]
            elif n == 3:  lines += ["You likely have many brothers. You are good-natured and generally comfortable."]
            elif n == 4:  lines += ["It indicates loss of comforts/wealth and risk of losing wife."]
            elif n == 5:  lines += ["You could be deprived of sons."]
            elif n == 6:  lines += ["You have tendency for irreligious acts, illness and enmity."]
            elif n == 7:  lines += ["Your spouse could be ill-natured and irritable."]
            elif n == 8:  lines += ["It indicates sinful, violent and dishonest tendencies."]
            elif n == 9:  lines += ["You will enjoy wealth and comforts from son in old age."]
            elif n == 10: lines += ["Conveyances, comforts and money are indicated."]
            elif n == 11: lines += ["Dignity at court and handsome presence are indicated."]
            elif n == 12: lines += ["This inauspicious placement indicates that you could be hurtful to everyone you come across, including your mother."]

        # ——— MERCURY from Moon ———
        elif pid == const._MERCURY:
            if   n == 1:  lines += ["You could lack ease and physical grace, have harsh speech and be a restless wanderer."]
            elif n == 2:  lines += ["You will enjoy wealth, house and kin but have risk of cold-borne ailments."]
            elif n == 3:  lines += ["You are likely to enjoy property and wealth. Gains via great persons or rulers are also indicated."]
            elif n == 4:  lines += ["You will be comfortable. Gains through maternal relations are also indicated."]
            elif n == 5:  lines += ["You have sharp intellect, good learning, pleasing looks and sensual tendencies, but you are harsh of tongue."]
            elif n == 6:  lines += ["You are likely to be miserly, timid and conflict-averse, and have hairy body and large eyes."]
            elif n == 7:  lines += ["You could be dominated by women, miser yet wealthy and enjoy long life."]
            elif n == 8:  lines += ["You are cold-natured, recognized by rulers and feared by foes."]
            elif n == 9:  lines += ["You could oppose your own religion and become absorbed in others’ religions. You could create opposition for many."]
            elif n == 10:
                lines += ["This is a Rāja-yoga that indicates status/authority."]
                # extra condition from the text – only emit if true
                if _house_idx_or_sign(const._MOON) == 9:  # Moon actually in 10th from lagna
                    lines += ["Because the Moon is in the 10th, status in the family rises and you will be leader of the clan."]
            elif n == 11: lines += ["You will enjoy gains at every step. Very early marriage is indicated."]
            elif n == 12: lines += ["You tend to be ever miserly and your son is likely to be unsuccessful."]

        # ——— JUPITER from Moon ———
        elif pid == const._JUPITER:
            if   n == 1:  lines += ["You are likely to be long-lived, healthy, powerful and consistently wealthy."]
            elif n == 2:  lines += ["You are likely to be respected by rulers, swift, valorous, virtuous and long lived (≈100 years)."]
            elif n == 3:  lines += ["You are liked by women and your father will likely gain wealth in your 17th year."]
            elif n == 4:  lines += ["You are likely to lack comforts and face troubles related to mother. You could serve in others’ homes."]
            elif n == 5:  lines += ["You are likely to enjoy good eyesight, valor, wealth and sons. You have a dominating nature."]
            elif n == 6:  lines += ["You are likely to have an indifferent nature. You could have to face homelessness. Although you will live long, you might have to make a living by low deeds or alms."]
            elif n == 7:  lines += ["You are likely to be a charismatic and influential figure within your family, possessing a long life and good health, yet facing challenges related to fertility."]
            elif n == 8:  lines += ["Frequent ailments and discomforts are indicated."]
            elif n == 9:  lines += ["You are likely to find fulfillment in serving both your spiritual beliefs and those you admire, leading to a life of wealth and virtue."]
            elif n == 10: lines += ["You will soon embrace a path of asceticism, choosing to renounce familial ties in pursuit of spiritual growth."]
            elif n == 11: lines += ["You will embrace a life filled with blessings, driving forward with confidence and a regal sense of purpose."]
            elif n == 12:
                lines += ["You are likely to oppose your own people."]
                # extra condition (only when Jupiter aspects 6th house from lagna)
                if _jupiter_aspects_house(5):
                    lines += ["Still, Jupiter’s aspect to the 6th house promises comfort."]
        
        # ——— VENUS from Moon ———
        elif pid == const._VENUS:
            if   n == 1:  lines += ["You may encounter significant dangers in your life, including the potential for serious accidents or unforeseen events that could lead to severe consequences."]
            elif n == 2:  lines += ["You are likely to embody a blend of wealth, intellectual prowess, and a regal bravery that commands respect."]
            elif n == 3:  lines += ["You are likely to find wisdom in your religious beliefs and may experience financial growth through connections with foreigners."]
            elif n == 4:  lines += ["You may experience a tendency towards a calm and steady demeanor, but your physical health might present challenges as you age, potentially leading to financial difficulties in your later years."]
            elif n == 5:  lines += ["You are likely to have a wealthy background with multiple daughters but may not seek or achieve widespread recognition."]
            elif n == 6:  lines += ["You may find yourself making impulsive decisions that lead to losses in conflicts."]
            elif n == 7:  lines += ["You may find yourself struggling with self-motivation and often viewing situations with skepticism."]
            elif n == 8:  lines += ["You are likely to become a renowned individual who, through your generosity and wealth, enjoys a life filled with various comforts."]
            elif n == 9:  lines += ["You are likely have a close-knit circle of siblings and friends who support and enrich your life."]
            elif n == 10: lines += ["You are likely to lead a long life while providing support to both of your parents."]
            elif n == 11: lines += ["You will likely enjoy a long and healthy life, facing few challenges or adversaries along the way."]
            elif n == 12: lines += ["You may find yourself drawn to inappropriate relationships, often acting impulsively without considering the consequences."]

        # ——— SATURN from Moon ———
        elif pid == const._SATURN:
            if   n == 1:  lines += ["You may find that your relationships with friends and relatives could be negatively impacting your health."]
            elif n == 2:  lines += ["You may find yourself facing challenges in your life, but you will thrive by relying on unconventional sources of strength and nourishment."]
            elif n == 3:  lines += ["You may find yourself contemplating the themes of loss and mortality, perhaps reflecting on the fragility of life as you consider the experiences of those who face early deaths, like classical daughters in literature."]
            elif n == 4:  lines += ["You will demonstrate purposeful effort in overcoming challenges and defeating your adversaries."]
            elif n == 5:  lines += ["Your spouse could be dark-complexioned and sweet-tongued."]
            elif n == 6:  lines += ["You could be short-lived and face many troubles."]
            elif n == 7:  lines += ["You are likely to be religious and generous; multiple marriages are possible."]
            elif n == 8:  lines += ["You tend to be bad for your father. Charity could reduce ill effects."]
            elif n == 9:  lines += ["Loss of wealth during Saturn’s mahadasha."]
            elif n == 10: lines += ["You will enjoy king-like status. Although you are miserly you will be wealthy."]
            elif n == 11: lines += ["You could have poor health and be irreligious."]
            elif n == 12: lines += ["You could be poor, beggarly and irreligious."]

        # ——— RAHU from Moon ———
        elif pid == getattr(const, "_RAHU", -1):
            if   n in (1, 10, 9): lines += ["You may rise to a king-like status, but in old age you will retain only wealth."]
            elif n in (6, 12):    lines += ["You are likely to enjoy status like a king or minister and be wealthy."]
            elif n in (4, 7):     lines += ["You tend to be adverse for parents and chronically unhappy."]
            elif n in (2, 11):    lines += ["Although you will enjoy fame and wealth there might be very little real comfort."]
            elif n == 5:          lines += ["You face risk of death by drowning and few comforts."]
            elif n == 3:          lines += ["Rahu in the third house from the Moon indicates significant courage, ambition, and a potential for success in areas like media, sales, and entrepreneurship, but it can also lead to overconfidence, recklessness, and conflicts, particularly with siblings. This placement suggests a person who is street-smart, willing to take risks, and has a strong will, though they may struggle with emotional control and trusting others' judgment."]            
            else:                 lines += ["You may have been born in a family with many dark secrets. You may have dark tendencies and taboo desires. Interest in the occult could be indicated by this placement."]

        # Build HTML block
        block = (
            f"<div class='mt-4'>"
            f"<h3 class='h6 text-center'>Reading based on {pname} from the Moon</h3>"
            f"<p class='text-left mb-1'><em>{header}</em></p>"
            + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines)
            #+ _md_note_for(pid, pname)
            #+ _weak_note_for(pid, pname)
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
        sb_val = _extract_shadbala_val(sb_res, pid)
        return (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val/1020)*100)}%</p>"
        )

    def _md_line(pid: int, name: str) -> str:
        md = _md_period_for(pid)
        if not md:
            return ""
        s, e = md
        return (
            f"<p class='text-left mt-2'><strong>"
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
            "You will likely exhibit a bold and combative nature, driven by a strong desire to succeed; your path to fame will come through your writing, but your restlessness and quick temper may present challenges. You may experience fluctuations in wealth, with your earnings potentially tied to industries involving force or weaponry, while you may also need to manage blood or Pitta-related issues.",
        ]
        # “If exalted, adverse influences are less marked”
        if _EXALTS.get(const._SUN) == sun_sign:
            sun_lines.append("Because the Sun is exalted here, the harsher notes tend to be muted.")
    elif sun_sign == 1:  # Taurus
        sun_lines += [
            "You are likely to be tolerant and shrewd in your dealings, potentially earning through your unique sense of style or unconventional means, while preferring to keep female company at a distance; you may also have a musical talent and could be prone to mouth or eye issues.",
        ]
    elif sun_sign == 2:  # Gemini
        sun_lines += [
            "You will likely be perceived as attractive, educated, and affluent, possessing a sweet demeanor, a talent for astrology, and a knack for learning quickly, which will help you gain status; the symbolism of ‘two mothers’ may also be significant in your life.",
        ]
    elif sun_sign == 3:  # Cancer
        sun_lines += [
            "You may experience feelings of being overwhelmed or financially strained, face friction in relationships with your father or relatives, engage in demanding work, yet you possess a strong ability to articulate your thoughts and lean towards spirituality, which may indicate potential Kapha-Pitta imbalances.",
        ]
        if _benefic_touches_pid(const._SUN):
            sun_lines.append("Benefic support here gives a distinct royal bearing.")
    elif sun_sign == 4:  # Leo
        sun_lines += [
            "You will be a firm, vigorous, and knowledgeable individual who excels at overcoming challenges, enjoys outdoor activities, appreciates a meaty diet, and achieves financial success, though you may occasionally experience ear troubles.",
        ]
    elif sun_sign == 5:  # Virgo
        sun_lines += [
            "You are likely a refined, creative, and mathematically inclined individual who possesses a shy demeanor, is multilingual, shows deep respect for others, and has the ability to earn well despite any physical delicacy.",
        ]
    elif sun_sign == 6:  # Libra
        sun_lines += [
            "You may find yourself often in conflict and struggling with stability, feeling vulnerable to authority figures, experiencing financial challenges, drawn to the partners of others, experimenting with alcohol or metalwork, and exhibiting impulsive and reckless bravery.",
        ]
    elif sun_sign == 7:  # Scorpio
        sun_lines += [
            "You are likely to be argumentative and quick to engage in conflict, possessing weapon skills and a daring nature that can sometimes come across as harsh; you may experience clashes with your parents and face risks related to poison or fire, yet you have the ability to adhere to proper religious discipline.",
        ]
    elif sun_sign == 8:  # Sagittarius
        sun_lines += [
            "You will be respected by authority, embodying scholarly wisdom, devout strength, adeptness with weapons, and a deep understanding of medical knowledge, earning you the reverence of those around you.",
        ]
    elif sun_sign == 9:  # Capricorn
        sun_lines += [
            "You will be a covetous wanderer, seeking poor comforts, often opposing your own desires while displaying cleverness, yet indulging in unworthy acts as you enjoy the wealth of others.",
        ]
    elif sun_sign == 10:  # Aquarius
        sun_lines += [
            "You will likely navigate life with few comforts and children, exhibit strength in your limbs, indulge in basic pleasures, hold rigid views, experience unstable friendships, and may be prone to cardiac strain.",
        ]
    elif sun_sign == 11:  # Pisces
        sun_lines += [
            "You will be well-liked and knowledgeable, capable of overcoming your adversaries, and likely to achieve success through ventures in water products or land irrigation; you may have many brothers, and there could be a hidden ailment to be mindful of.",
        ]
    sun_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Sun</h3>"
        f"<p class='text-left mb-1'><em>Sun is in {SIGN_NAMES[sun_sign]}.</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in sun_lines)
        #+ _md_line(const._SUN, "Sun") + _weak_note_line("Sun", const._SUN)
        + "</div>"
    )

    # ◼ MOON – sign based reading
    moon_sign = _planet_sign(const._MOON)
    moon_deg_in_sign = (_get_lon(const._MOON) % 30.0)
    moon_lines = []
    if moon_sign == 0:
        moon_lines += [
            "You are likely a wealthy individual who may have a quick temper and few siblings, possibly with sons of your own; you exhibit courage in your adventures, have a strong attraction to romance, and possess the ability to charm women, earning you respect from leaders, while you tend to shy away from deep waters and may experience some knee weakness, complemented by round, pretty eyes, a scar on your head, and minimal body hair.",
        ]
    elif moon_sign == 1:
        moon_lines += [
            "You will be a charitable and sensuous individual, honored and pleasure-loving, displaying great bravery and strength, nurturing many daughters, and embodying forgiveness and steadfastness in friendship; however, you may find that finances, family, and progeny could face challenges.",
        ]
        # condition: first half vs second half of Taurus
        if moon_deg_in_sign < 15:
            moon_lines.append("Moon in the first half of Taurus tends to be adverse for the mother.")
        else:
            moon_lines.append("Moon in the second half of Taurus tends to be adverse for the father.")
    elif moon_sign == 2:
        moon_lines += [
            "You are a poetic and skillful lover, possessing handsome features and great intelligence; your jovial nature, deep understanding of scripture, ability to read hidden thoughts, and sweet tongue make you truly captivating.",
        ]
    elif moon_sign == 3:
        moon_lines += [
            "You are likely to experience fluctuations in wealth, possess an astrological inclination, walk briskly, own homes or land, enjoy fortunate friendships, have a sensuous nature, and develop a fondness for water sports or orchards.",
        ]
    elif moon_sign == 4:
        moon_lines += [
            "You are likely to have a strong affinity for mountains and forests, possess broad features, exhibit high energy levels, may be less inclined towards women, and could experience hunger, thirst, as well as abdominal or dental issues; you likely enjoy eating meat, display both charitable and aggressive traits, have few sons, and demonstrate a strong sense of duty towards your parents.",
        ]
    elif moon_sign == 5:
        moon_lines += [
            "You will likely be an attractive and well-educated individual with a teacher-like demeanor, possessing a strong sense of spirituality, sweetness, and truthfulness; you will be composed and helpful, have many daughters and few sons, appreciate the arts, enjoy the wealth of others, and may find yourself residing abroad.",
        ]
    elif moon_sign == 6:
        moon_lines += [
            "You will likely possess prominent features, exhibit a slim build, and demonstrate a strong sense of ethics in your trading skills, while navigating fluctuating fortunes and facing some health challenges, ultimately finding yourself helpful to relatives who may, in turn, abandon you.",
        ]
    elif moon_sign == 7:
        moon_lines += [
            "You are likely to face early health challenges, but will later develop a strong physique; you may struggle with a covetous and atheistic mindset, possess captivating eyes, and achieve financial success, though you might find yourself drawn to other people's partners and exhibiting a cruel heart, leading to estrangement from relatives and potential losses at the hands of authority figures; your prominent abdomen and forehead may reflect your inner conflicts, and you may harbor secret sins.",
        ]
    elif moon_sign == 8:
        moon_lines += [
            "You will embody a sāttvic nature, displaying wealth and a touch of haughtiness, while showcasing your multi-talented abilities; you will inherit property and demonstrate a charitable spirit, combined with strength and eloquence, navigating a devout path even as you challenge your own kin, ultimately yielding only to love and kindness.",
        ]
    elif moon_sign == 9:
        moon_lines += [
            "You will likely be a musical and learned individual, often subdued by the influence of women, known for your charitable and forgiving nature, which pleases your spouse. You may have a strong sense of religion, a tendency to wander or be lazy, a dislike for cold weather, and possess fine eyes and skin, along with a tall and handsome stature.",
        ]
    elif moon_sign == 10:
        moon_lines += [
            "You will likely be seen as clever yet indolent, drawn to the partners of others, and prone to sinful behavior; as a sculptor, you may find favor among friends, but your ill-natured disposition and financial struggles could lead you to enjoy the wealth of those around you.",
        ]
    elif moon_sign == 11:
        moon_lines += [
            "You are likely a highly talented individual who earns a living through sea products, is devoted to your family, excels as a sculptor, consistently triumphs over opponents, shows a gentle disposition towards women, and embodies kindness and charity, all while possessing a beautiful and well-proportioned physique.",
        ]
    moon_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Moon</h3>"
        f"<p class='text-left mb-1'><em>Moon is in {SIGN_NAMES[moon_sign]}.</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in moon_lines)
        #+ _md_line(const._MOON, "Moon") + _weak_note_line("Moon", const._MOON)
        + "</div>"
    )

    # ◼ MARS – sign based reading
    mars_sign = _planet_sign(const._MARS)
    mars_lines = []
    if mars_sign == 0:
        mars_lines += [
            "You will be a truth-teller, bold and battle-ready, destined for fame and wealth, known for your eloquence and well-liked by all, experiencing gains in cattle and agriculture, though your quick temper may lead to many relationships.",
        ]
    elif mars_sign == 1:
        mars_lines += [
            "You are likely to face many adversaries and find few sources of comfort; you may possess a sharp tongue and a rebellious spirit, yet you can also sing beautifully, though there may be a tendency to complicate the lives of virtuous women.",
        ]
    elif mars_sign == 2:
        mars_lines += [
            "You will likely embrace a large family, possess an attractive demeanor, excel in multiple disciplines, create art as both a poet and sculptor, hold strong religious beliefs, and enjoy opportunities for foreign travel.",
        ]
    elif mars_sign == 3:
        mars_lines += [
            "You will find yourself frequently living off others, feeling unwell and discontent, while earning a living through land and water activities.",
        ]
    elif mars_sign == 4:
        mars_lines += [
            "You are likely to embody a valorous yet struggling spirit, drawn to the challenges of the forest and hard work, with an intolerance for limitations; your adventurous hunter's streak may reveal irreligious tendencies, posing a risk to your first marriage.",
        ]
    elif mars_sign == 5:
        mars_lines += [
            "You are likely to be a wealthy individual with a large family, possessing a sweet disposition and a wealth of knowledge, tending to be a spendthrift while also being deeply religious and cautious of potential adversaries.",
        ]
    elif mars_sign == 6:
        mars_lines += [
            "You are likely to be an itinerant speaker with good looks, showing affection to your spouse, mentors, and friends, but you may face challenges that could pose a risk to your first marriage.",
        ]
    elif mars_sign == 7:
        mars_lines += [
            "You are likely to embody the traits of a conqueror and gang leader, displaying honesty while posing a threat to your adversaries, which may expose you to risks from poison, fire, or weapons.",
        ]
    elif mars_sign == 8:
        mars_lines += [
            "You will likely face challenges in your high-ranking position, as weapon injuries may weaken your resolve, leading to bitter speech and a tendency to disregard the wisdom of elders and mentors while engaging in hard labor.",
        ]
    elif mars_sign == 9:
        mars_lines += [
            "You will embody the qualities of a courageous leader, displaying bravery in battle, earning your achievements through your own efforts, and remaining devoted to your homeland.",
        ]
    elif mars_sign == 10:
        mars_lines += [
            "You may often find yourself struggling with feelings of insecurity and jealousy, leading to conflicts with those around you, while your self-perception might come across as arrogant, and you could feel unlucky in various aspects of life. Hairly body is likely.",
        ]
    elif mars_sign == 11:
        mars_lines += [
            "You will find yourself feeling humiliated by those around you, showing disrespect towards authority figures, grappling with your own health and moral challenges, while seeking validation and praise from others abroad.",
        ]
    mars_rashi_html = (
        f"<div class='mt-4'><h3 class='h6 text-center'>Rāśi-based reading: Mars</h3>"
        f"<p class='text-left mb-1'><em>Mars is in {SIGN_NAMES[mars_sign]}.</em></p>"
        + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in mars_lines)
        #+ _md_line(const._MARS, "Mars") + _weak_note_line("Mars", const._MARS)
        + "</div>"
    )
    
        # ── Rashi-location readings (Mercury, Jupiter, Venus, Saturn, Rahu, Ketu) ──
    def _build_rashi_block(pid: int, name: str, sign_idx: int, mapping: dict[int, list[str]]):
        header = f"{name} is in {SIGN_NAMES[sign_idx]}"
        lines = list(mapping.get(sign_idx, []))

        # Compose base HTML
        html = (
            f"<div class='mt-4'><h3 class='h6 text-center'>Rashi reading — {name}</h3>"
            f"<p class='text-left mb-1'><em>{header}</em></p>"
            + "".join(f"<p class='text-left mb-1'>• {txt}</p>" for txt in lines)
            + "</div>"
        )

        # Mahadasha line
        md = _md_period_for(pid)
        md_note = ""
        if md:
            _s, _e = md
            md_note = (
                f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {name}:</strong> "
                f"{_s:%Y-%m-%d} – {_e:%Y-%m-%d}</p>"
            )

        # Weak-note (avasthas or sub-threshold shadbala)
        weak_note = ""
        sb_val = _extract_shadbala_val(sb_res, pid)
        under_thresh = (pid in SHAD_THRESH) and (sb_val is not None) and (sb_val < SHAD_THRESH[pid])
        is_weak = (pid in avs["bala"]) or (pid in avs["mrita"]) or (pid in avs["sushupti"]) or under_thresh
        if is_weak:
            weak_note = (
                f"<p class='text-left mt-2'><strong>Note:</strong> "
                f"The above predictions may not manifest very strongly, since the {name} is weak</p>"
            )
        weak_note = (
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val/1020)*100)}%</p>"
        )
        return html
        #return html.replace("</div>", f"{md_note}{weak_note}</div>")

    # Sign → bullet lines (reworded, faithful to source; not sugar-coated)
    mercury_sign_readings = {
        0: ["You are sharp but contentious, cunning and restless, with a tendency toward exaggeration or deceit. You enjoy the performing arts and carry a highly sensual streak. You may spend freely and could face debt or periods of restriction."],
        1: ["You tend to attract wealth and come across as dependable and charitable. Multi-skilled, witty and musical, you balance sensuality with a respected public image."],
        2: ["You present well and often prosper, with a strong orator’s voice and a touch of pride. You may be cooler toward sex, carry themes of dual nurturing or ‘two-mother’ influences, and be versed in scripture, living generally comfortably."],
        3: ["You come across as scholarly and may live or spend time abroad. Talkative to a fault, you’re drawn to pretty partners, artistic pursuits, and may experience clashes with friends or relatives, while gains can come through water-related work."],
        4: ["You may wander widely and even gain notice, yet studies can suffer and memory may feel unreliable. Wealth or property can be strained, relationships with women may be challenging, and obligations of service may weigh on you."],
        5: ["You possess a religious or philosophical intellect and can be a learned poet, speaker, or writer. You’re honored and fearless, prone to argument yet ultimately forgiving."],
        6: ["You have a silver tongue and many skills in the arts, with a devout streak and a trader’s mind. You spend readily and may indulge sensual tastes."],
        7: ["You can be industrious yet dismissive of tradition, at times shameless or grasping. You may attract questionable partners, lean toward deceit, and find yourself coveting what belongs to others."],
        8: ["You show scriptural mastery and a forgiving heart, often gaining renown as a teacher or guide. Brave and potentially wealthy, you write persuasively and associate with worthy women."],
        9: ["You may feel servile or unstable at times, slip into gossip, or feel shunned by kin. Hyper-fickleness and lust can pull you off course, accompanied by bluster that masks insecurity."],
        10:["You may feel harried by opponents and struggle with duty, refinement, or clean routines. Speech may falter, and patterns of timidity or servility can surface."],
        11:["You’re good-natured, pious, capable, and helpful, readily winning friends’ affection, though material growth may feel limited. Themes of distant lands tend to recur."],
    }

    jupiter_sign_readings = {
        0: ["You are pious yet combative in debate, enjoy adornment, and can be wealthy and well-known. You spend generously, may face opposition, carry scars from scrapes, and show a harsh streak at times."],
        1: ["You tend toward robust health and fortune, with devotion to sacred traditions and care for land or cattle. Loyal to your spouse, you come across as wise and benevolent."],
        2: ["You have a ministerial air, draw support from friends and children, and present as attractive with fine eyes. Eloquence and religious leanings stand out."],
        3: ["You are likely to be wealthy and learned, strong and truthful, adored by others, and carry a king-like stature."],
        4: ["You are strong, learned, and wealthy, a pious commander or leader with an aggressive edge; your life may revolve around forts, forests, or mountains."],
        5: ["You are learned, pious, and efficient, with a love for scents and flowers. You tend to outclass opponents and are widely versed."],
        6: ["You come across as wise and soft-spoken, attractive and trade-oriented, with scriptural erudition and earnings linked to foreign lands."],
        7: ["You can be a clever scriptural commentator who keeps worthy company, yet periods of illness and hard toil arise. A quick temper and occasional forays into forbidden pursuits color your path."],
        8: ["You resemble a religious teacher—very wealthy, charitable, and of high rank—with life marked by pilgrimages and foreign circuits."],
        9: ["You may feel overworked and servile, with pleasures curtailed and vitality low; irreligious fears can arise, and distant-lands themes recur."],
        10:["You can be prone to illness and grasping tendencies, with money losses from poor judgment and vulnerability to abdominal or dental issues; some traditions note results akin to Cancer here."],
        11:["You may be a lovable, famous Vedic scholar, and a notably hairy physique can be a signature."],
    }

    venus_sign_readings = {
        0: ["You have a knack for leading teams or troops and a restless longing to go abroad. Be mindful of chasing unavailable partners or provoking authority, as legal tangles could follow; reliability needs care, and there may be risks of night-blindness."],
        1: ["You may be surrounded by women and children, prosper through agriculture or cattle, and enjoy scents and flowers. You tend to be attractive and relatively free of enemies."],
        2: ["You are versed in scripture and can become very famous, with a beautiful presence and talent as a writer or poet. Friendly and devout yet sensual, you may earn through song or dance."],
        3: ["You pursue good deeds and learning, standing strong and religious while obtaining what you desire. Two marriages are possible, and excess with liquor or lovers can bring sickness."],
        4: ["You may gain money through women, have fewer children, and at times defer to women. You overcome enemies, remain devoted to teachers or priests, and live generally comfortable and wealthy."],
        5: ["You can become very rich and persuasive with women, with pilgrimages and learning prominent; at times material comforts still feel lacking."],
        6: ["You earn through steady effort, love garlands and fine clothes, and take foreign journeys. Religious inclination is present, though you may waver under pressure."],
        7: ["You may become quarrelsome or notorious, dismissive of religion, and overly talkative, with strained ties to brothers. Violent skills might surface, poverty can intrude, and genitourinary issues need care."],
        8: ["You are virtuous and well-liked, potentially wealthy and high-ranking, with a larger build and a life marked by honors."],
        9: ["You may lean toward over-sensuality or older partners, spend freely, and push boundaries; watch for heart or potency issues and the pull to covet others’ wealth."],
        10:["You risk entanglement with others’ spouses and irreligious attitudes, with clashes involving mentors or children; periods of low self-presentation and anxiety can appear."],
        11:["You can be very wealthy, subduing opponents while gaining fame and royal favor. Charitable and learned, you speak gently and love swimming."],
    }

    saturn_sign_readings = {
        0: ["You may carry a weak constitution, worn by labor or excess, with flashes of ill temper and deceit. Estrangement from kin, lapses in cleanliness, and a troubled reputation may need conscious repair."],
        1: ["You can feel poor or servile and may consort with older partners or circles that pull you off-track, including entanglements with others’ spouses. Versatile yet transgressive in mate choice, you often test social norms."],
        2: ["You may feel hounded by debt, confinement, and hard toil, with patterns of deceit or lust that sap momentum and drift into laziness or vice."],
        3: ["You may be frail in childhood or carry mother-loss themes, growing learned despite lean means and possibly becoming famous. Conflicts with relatives occur, and health issues can linger."],
        4: ["You can be a skilled writer yet quarrelsome and socially non-conforming, feeling miserable or servile at times. Separation from spouse or friends is possible, taboo pursuits tempt, and anger runs quick."],
        5: ["You may seem unsteady and face repeated setbacks, with soft or refined traits and a chase for lax-morals company. Yet a sculptor’s or artisan’s bent emerges, and paradoxically you can still be helpful, with wealth and progeny."],
        6: ["You carry a regal bearing—eloquent, publicly honored, and prone to wandering—alongside a taste for sexual indulgence and ties with courtesans or performers."],
        7: ["You must guard against harm by fire, weapons, or poison, as well as a hot temper and conceit. Grabbing others’ assets or dabbling in taboos can bring insincerity, losses, and illness."],
        8: ["You can attain broad fame and contentment, with steady income, many disciplines, capable children, concise speech, and wide honors."],
        9: ["You align with power, display learning and craft, and may be admired and famous, with courage and foreign travel in the mix; be mindful of controlling impulses regarding others’ partners or wealth."],
        10:["You may grow very rich yet veer into deceit, heavy drink, and entanglements with others’ spouses, with fickle, irreligious tendencies that need discipline."],
        11:["You are respected, helpful, and wealthy, inclined to religious pursuits, with a mild, cool temperament and a knowing eye for gems."],
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
                f"<p class='text-left mb-1'><em>{header_r}</em></p>"
                + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines_r)
                + "</div>"
            )
            # MD line
            #mdR = _md_period_for(rahu_pid)
            #if mdR:
            #    _sr, _er = mdR
            #    rashi_rahu_html = rashi_rahu_html.replace("</div>",
            #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of Rahu:</strong> {_sr:%Y-%m-%d} – {_er:%Y-%m-%d}</p></div>"
            #    )
            # Weak note (only avasthas apply; shadbala thresholds are not classically defined for nodes)
            #if (rahu_pid in avs["bala"]) or (rahu_pid in avs["mrita"]) or (rahu_pid in avs["sushupti"]):
            #    rashi_rahu_html = rashi_rahu_html.replace("</div>",
            #        "<p class='text-left mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the Rahu is weak</p></div>"
            #    )

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
                f"<p class='text-left mb-1'><em>{header_k}</em></p>"
                + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines_k)
                + "</div>"
            )
            # MD line
            #mdK = _md_period_for(ketu_pid)
            #if mdK:
            #    _sk, _ek = mdK
            #    rashi_ketu_html = rashi_ketu_html.replace("</div>",
            #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of Ketu:</strong> {_sk:%Y-%m-%d} – {_ek:%Y-%m-%d}</p></div>"
            #    )
            # Weak note (only avasthas apply; shadbala thresholds are not classically defined for nodes)
            #if (ketu_pid in avs["bala"]) or (ketu_pid in avs["mrita"]) or (ketu_pid in avs["sushupti"]):
            #    rashi_ketu_html = rashi_ketu_html.replace("</div>",
            #        "<p class='text-left mt-2'><strong>Note:</strong> The above predictions may not manifest very strongly, since the Ketu is weak</p></div>"
            #    )
    
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
            const._MOON:    ["You tend to be charitable and gentle in appearance yet attractive. You receive help or support from attendants or helpers, and you’re often drawn toward sensual, pleasurable company."],
            const._MARS:    ["You come across as very strong, with a hard-edged intensity that shows in your gaze; in conflict you keep your composure and hold your ground."],
            const._MERCURY: ["At times you may lose your nerve and some of your usual comforts, feeling obliged to serve others while your resources and presence feel diminished."],
            const._JUPITER: ["Your wealth is likely to rise, and you may step into an advisory or judicial stature; generosity grows, and you’re respected within your family."],
            const._VENUS:   ["You may feel magnetized to complicated or disreputable liaisons, drawing opposition from many; real friends can be few for a time, and you should watch for skin sensitivities and periods of financial strain."],
            const._SATURN:  ["Your courage may sag at times and health can feel fragile, leaving you feeling dull or ungainly in appearance."],
        },
        "venus": {
            const._MOON:    ["You may entertain multiple romantic ties and feel drawn to refined, courtesan-like company; earnings can connect with water, beverages, liquids, or maritime trades."],
            const._MARS:    ["Under pressure you remain composed, showing strength and boldness, and you gain steadily through your own toil."],
            const._MERCURY: ["You carry a natural talent for music, poetry, and writing, and your appearance tends to be pleasing."],
            const._JUPITER: ["Your world fills with both allies and adversaries, yet you can rise to a minister-grade stature; wealth accumulates and you feel broadly content."],
            const._VENUS:   ["Your eyes are fine and expressive, though your heart can be timid; you often serve those in power and remain comfortably well-off."],
            const._SATURN:  ["A lethargic streak can surface, with spells of ill health or scarcity, and you may keep close company with older women."],
        },
        "mercury": {
            const._MOON:    ["Both friends and rivals can wear you down, bringing low spirits; foreign travel or residence may come with hassles and adjustments."],
            const._MARS:    ["You may be wary of enemies yet prone to quarrels, with contests sometimes going against you and moments of humiliation possible."],
            const._MERCURY: ["You carry a regal bearing and can become well known, supported by friends with few true enemies; just keep an eye on eye-related sensitivities."],
            const._JUPITER: ["You become very learned and adept with mantra or sacred speech; your mind is sharp, though inner calm may ebb, and your path brings frequent movement to or from foreign places."],
            const._VENUS:   ["Spouse, children, and growing wealth comfort you; you present well and generally enjoy good health."],
            const._SATURN:  ["Agitation comes easily and a tendency toward clever tricks can backfire; you may command many helpers yet still face periods of muddled judgment."],
        },
        "moon": {
            const._MOON:    ["You exude king-like confidence, at times with a harsh edge, and wealth can flow through water-linked pursuits."],
            const._MARS:    ["Tendencies toward inflammation or perianal discomfort may surface; circles of friendship can feel thin, and comforts through children may fluctuate."],
            const._MERCURY: ["You may gain renown for learning and status, finding favor with authorities; your wit shines and you remain largely free of foes."],
            const._JUPITER: ["You carry an envoy’s or diplomat’s vibe, with potential for high office; fame can spread widely and your talents prove many."],
            const._VENUS:   ["Income may arrive through women or feminine networks; you do tangible good for others, speak sweetly, and act with quiet bravery."],
            const._SATURN:  ["Kapha-Vata type ailments may visit the body; guard against coveting others’ wealth, skewed judgment, or back-biting tendencies."],
        },
        "own": {
            const._MOON:    ["You are shrewd and persuasive, liked by the powerful, though Kapha-type issues may surface."],
            const._MARS:    ["You appear gallant and quick-witted, draw many lovers, and are quietly feared by rivals."],
            const._MERCURY: ["You write with skill and love to travel, even if physical stamina is sometimes middling."],
            const._JUPITER: ["You’re the sort who builds—temples, orchards, reservoirs—standing strong and wise while enjoying stretches of solitude."],
            const._VENUS:   ["A harsh, shameless note can slip into your style, bringing friction with relatives; tend the skin, as complaints are possible."],
            const._SATURN:  ["You may press or harass close ones and, at times, derail others’ efforts; for some, there’s a risk of weakened virility."],
        },
        "jupiter": {
            const._MOON:    ["You are blessed with children, learning, and a touch of fame; your attractiveness grows and you enjoy king-like contentment."],
            const._MARS:    ["Renown can come through assertive clarity—even in competitive or martial arenas—and wealth and comforts accrue."],
            const._MERCURY: ["You’re poetic, multilingual, and sweet-spoken, with a working knowledge of minerals, metals, and allied crafts."],
            const._JUPITER: ["You remain learned and affluent, moving with high company and dignitaries."],
            const._VENUS:   ["You attract a virtuous, beautiful spouse and delight in fine clothes and finery."],
            const._SATURN:  ["A rough-hewn, unkempt phase is possible, serving those who have fallen on hard times; beware coveting others’ food, though caretaking of cattle or stock may feature."],
        },
        "saturn": {
            const._MOON:    ["A cunning, unsettled mood may arise, and wealth or comfort can ebb through entanglements with women."],
            const._MARS:    ["You may feel dogged by illness and adversaries, with a greater likelihood of physical scrapes or injuries."],
            const._MERCURY: ["You present boldly, yet may feel detached or asexual at times, lapsing into untidy habits and a tendency to covet others’ assets."],
            const._JUPITER: ["You grow wise and well known, becoming a refuge for many."],
            const._VENUS:   ["Livelihood may link to crafts, shells, ornaments, or similar trades, and gains can arrive through unconventional or stigmatized feminine networks."],
            const._SATURN:  ["You steadily overcome foes, earn the trust of leaders, and remain content at heart."],
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
                f"<p class='text-left mt-2'><strong>"
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
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val_x/1020)*100)}%</p>"
        )
        block = (
            f"<p class='text-left mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects the Sun in {grp_label}:</em></p>"
            + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines)
            #+ md_html + weak_html
        )
        sub_sections.append(block)

    sun_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on the Sun (by sign of the Sun)</h3>"
        + "".join(sub_sections if sub_sections else [
            "<p class='text-left mb-1'><em>No planetary aspects to the Sun found.</em></p>"
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
            const._SUN:     ["You may have a quick temper and at times face financial pressure, even feeling dependent on others in difficult ways."],
            const._MARS:    ["You could experience dental or eye troubles and some risk of injuries, though your status may still rise despite issues with the urinary system."],
            const._MERCURY: ["You are likely to be educated, articulate, and admired for your speech or poetry, with reputation following you."],
            const._JUPITER: ["You may gain king-like stature and see wealth steadily accrue in your life."],
            const._VENUS:   ["You are very agreeable, virtuous, and a persuasive speaker who draws others toward you."],
            const._SATURN:  ["You may suffer from poor health at times, show untruthful tendencies, or get involved in underhanded acts or theft."],
        },
        1: {  # Vrisha / Taurus
            const._SUN:     ["Your work may be tied to land or agriculture, involving hard labor or even servile roles."],
            const._MARS:    ["You may be over-indulgent sexually, popular with women, and good company, though this could lead to loss of property."],
            const._MERCURY: ["You are learned, eloquent, and highly skilled, gaining respect through your abilities."],
            const._JUPITER: ["You are virtuous, famous, and admirable, blessed with a good spouse and children."],
            const._VENUS:   ["You will enjoy many comforts and ease, almost at the level of royalty."],
            const._SATURN:  ["You may accumulate wealth but at the same time become harsh in nature, with strain on your mother’s wellbeing."],
        },
        2: {  # Mithuna / Gemini
            const._SUN:     ["You may be clever and attractive but face financial hardships that persist."],
            const._MARS:    ["You are brave and learned, possibly linked to the arms trade, though a bodily defect could trouble you."],
            const._MERCURY: ["You may become a confidant to leaders and will often defeat rivals with your intelligence."],
            const._JUPITER: ["You are discerning and carry the qualities of a teacher, deeply learned."],
            const._VENUS:   ["You will enjoy a fearless and beautiful spouse, along with vehicles and ornaments."],
            const._SATURN:  ["You may suffer losses of wealth, spouse, vehicles, or children, and could find yourself in menial weaving-type work."],
        },
        3: {  # Karka / Cancer
            const._SUN:     ["You may face eye problems, take on custodian duties of estates or forts, and at times struggle with poverty."],
            const._MARS:    ["You will be bold and hold status, though your body may remain weak."],
            const._MERCURY: ["You are a learned poet and may serve as an advisor or minister."],
            const._JUPITER: ["You will be learned, famed, valiant, and carry the archetype of a ruler."],
            const._VENUS:   ["You may acquire gems and ornaments, and be attractive, but risk entanglements with women of ill repute."],
            const._SATURN:  ["You may live a wandering life, face hostility toward your mother, and work in trades like iron or arms."],
        },
        4: {  # Simha / Leo
            const._SUN:     ["You are brave and of fine qualities, rising close to royal standing, though children may be delayed or denied."],
            const._MARS:    ["You may command authority and forces like a ruler but carry a sharp temper."],
            const._MERCURY: ["You will be devoted to your spouse, learned, and inclined toward astrology."],
            const._JUPITER: ["You are wealthy, virtuous, and famous in society."],
            const._VENUS:   ["You combine scholarship with some frailty, remain devoted to your spouse, and enjoy royal ease."],
            const._SATURN:  ["You may focus on agriculture but lose wealth or comforts at home, leaning toward sinful actions or lowly professions."],
        },
        5: {  # Kanya / Virgo
            const._SUN:     ["You may serve women and enjoy varied comforts in life."],
            const._MARS:    ["You may become a sculptor or fabricator, gaining fame and wealth, and remain battle-ready."],
            const._MERCURY: ["You may excel as a poet or astrologer, win debates, and gain king-like recognition."],
            const._JUPITER: ["You may be favored by rulers, take military leadership, and keep your promises."],
            const._VENUS:   ["You are likely to have multiple spouses, be wealthy, learned, and multi-talented."],
            const._SATURN:  ["You may lose wealth and wisdom, depend on women, and suffer from weak memory."],
        },
        6: {  # Tula / Libra
            const._SUN:     ["You may face a wandering life with sickness, poverty, humiliation, and lack of comforts."],
            const._MARS:    ["You may carry a harsh temper, risk adultery, act violently, or suffer eye disease."],
            const._MERCURY: ["You are multi-talented, very wealthy, learned, and eloquent."],
            const._JUPITER: ["You will be highly respected and may trade in gold or precious stones."],
            const._VENUS:   ["You are healthy, attractive, wealthy, learned, and likely to succeed in trade."],
            const._SATURN:  ["You may become harsh in nature, wealthy, yet indulgent in sensuality."],
        },
        7: {  # Vrischika / Scorpio
            const._SUN:     ["You may be learned yet wandering, deprived of wealth and comforts, and face social dislike."],
            const._MARS:    ["You may become famous, win wars, carry a royal bearing, and be a voracious eater."],
            const._MERCURY: ["You may have abrasive speech, father twins, and be capable in crafts."],
            const._JUPITER: ["You will be norm-abiding with a pleasing appearance."],
            const._VENUS:   ["You will be wealthy and pleasant, able to spot others’ weaknesses, but may associate with washerman-type groups."],
            const._SATURN:  ["You may be sickly, suffer bodily defects, or be overly greedy."],
        },
        8: {  # Dhanu / Sagittarius
            const._SUN:     ["You may be wealthy, famous, and carry king-like stature."],
            const._MARS:    ["You may rise as an army leader, wealthy, valorous, and renowned."],
            const._MERCURY: ["You may become a sculptor or astrologer and protect your kin."],
            const._JUPITER: ["You may be handsome and devout, holding ministerial status and great wealth."],
            const._VENUS:   ["You may be good-looking, enjoy many comforts, have loyal friends and spouse, and give refuge to others."],
            const._SATURN:  ["You are a fine speaker, strong, proud, with a philosophical bent, though at risk of courtesan attachments."],
        },
        9: {  # Makara / Capricorn
            const._SUN:     ["You may live as a poor wanderer with plain looks, yet remain helpful to others."],
            const._MARS:    ["You may be famed, wealthy, and fortunate, comparable to a king."],
            const._MERCURY: ["You may enjoy king-like status but face estrangement from spouse or children."],
            const._JUPITER: ["You may be valorous, ruler-like, and have many wives, children, and friends."],
            const._VENUS:   ["You are learned but may enjoy others’ wealth and women."],
            const._SATURN:  ["You may appear indolent and plain yet rich, with attraction toward others’ spouses."],
        },
        10: {  # Kumbha / Aquarius
            const._SUN:     ["You may appear unpleasant in looks, carry immoral tones, and focus on farming."],
            const._MARS:    ["You may be honest but lazy, servile, and harsh in character."],
            const._MERCURY: ["You will enjoy comforts, speak well, and gain favor of rulers."],
            const._JUPITER: ["You may live with royal equivalence, with status and possessions in abundance."],
            const._VENUS:   ["You may be drawn to others’ wives, have little sensual comfort, and risk falling into cowardice or sin."],
            const._SATURN:  ["You may be irreligious and drawn to others’ wives, though benefic aspects can turn this into fame and prosperity."],
        },
        11: {  # Meena / Pisces
            const._SUN:     ["You may be highly sensuous, wealthy, and capable of leading forces, though leaning toward sinful paths."],
            const._MARS:    ["You may act harshly, face humiliation, and lack comforts."],
            const._MERCURY: ["You may be witty, wealthy, and famed, though consorting with others’ wives."],
            const._JUPITER: ["You may be attractive, very wealthy, surrounded by many women, and hold king-like stature."],
            const._VENUS:   ["You are learned and pleasant, immersed in music, dance, and singing."],
            const._SATURN:  ["You may be tormented by lust, drawn to women of low standing, and risk falling onto a sinful track."],
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
                f"<p class='text-left mt-2'><strong>"
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
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val_x/1020)*100)}%</p>"
        )

        block = (
            f"<p class='text-left mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects the Moon in "
            f"{SIGN_NAMES[moon_sign]}:</em></p>"
            + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines)
            #+ md_html + weak_html
        )
        sub_sections_moon.append(block)

    moon_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on the Moon (by sign of the Moon)</h3>"
        + "".join(sub_sections_moon if sub_sections_moon else [
            "<p class='text-left mb-1'><em>No qualifying planetary aspects to the Moon found for the current rules.</em></p>"
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
            const._SUN:    ["You are likely to have a ministerial or judicial streak, with the gift of persuasive speech. Your life may bring wealth, a good spouse, and sons."],
            const._MOON:   ["You will be brave, but there may be attraction to others’ partners and risks of injury. Some strain with your mother is also indicated."],
            const._MERCURY:["You may be sensual and drawn to women of easy morals, with a tendency to covet the wealth of others."],
            const._JUPITER:["You are learned, sweet-tongued, and devoted to your father. Wealth will come to you in life."],
            const._VENUS:  ["You will have a strong appetite and may suffer due to women in your life."],
            const._SATURN: ["You may be drawn to other people’s wives, shunned by your own kin, and suffer from a weak constitution."],
        },
        "venus": {  # Mars in Taurus/Libra
            const._SUN:    ["Your path may lead you into forests and hills, with a quick temper and a natural antipathy toward women."],
            const._MOON:   ["You may find yourself opposed to your mother, timid, yet drawn to multiple women."],
            const._MERCURY:["You will be learned, talkative, and quarrelsome, while maintaining a pleasant appearance."],
            const._JUPITER:["You will be fortunate, with a natural attraction to music and dance."],
            const._VENUS:  ["You will be worthy of praise, with the potential to rise as a minister or commander, enjoying many comforts."],
            const._SATURN: ["You may become famous, amiable, wealthy, and learned in life."],
        },
        "mercury": {  # Mars in Gemini/Virgo
            const._SUN:    ["You are learned, wealthy, and valorous, with a life connected to forts, forests, or mountains."],
            const._MOON:   ["You will lead women and be agreeable, wise, and wealthy, often taking on royal or security roles."],
            const._MERCURY:["You will talk a lot, enjoy poetry, possess mathematical talent, and charm others with harmless fibs."],
            const._JUPITER:["You will carry the presence of an envoy or sovereign, highly skilful, a leader of men, and may even leave your homeland."],
            const._VENUS:  ["You will enjoy wealth, fine food and attire, and be devoted to your spouse."],
            const._SATURN: ["You may turn to agriculture, appearing lazy yet brave, with a rough outward look."],
        },
        "moon": {  # Mars in Cancer
            const._SUN:    ["You may experience pitta aggravation, but will carry a judge-like presence, dispensing punishment with authority."],
            const._MOON:   ["You are likely to be sickly, of low character, and have plain looks."],
            const._MERCURY:["You may be unattractive, shameless, sinful, and friendless."],
            const._JUPITER:["Your life will bring you fame, learning, and a high office."],
            const._VENUS:  ["You may be tormented through women, suffer humiliation, and lose money in unworthy pursuits."],
            const._SATURN: ["You may earn through sea trade or maritime ventures, with good looks and a standing close to royalty."],
        },
        "sun": {  # Mars in Leo
            const._SUN:    ["You may wander in woods and mountains, with a forceful nature and a tendency to protect your own."],
            const._MOON:   ["You will have a hardy body and a harsh heart, facing stress connected to your mother, yet remaining skilful and bright."],
            const._MERCURY:["You may become a sculptor, painter, or poet, with greed but also exceptional cleverness."],
            const._JUPITER:["You are destined for army leadership, royal favour, learning, and the ability to fulfil others’ wishes."],
            const._VENUS:  ["You will be attractive, enjoy many liaisons, and remain famous and youthful."],
            const._SATURN: ["You may look prematurely aged, worry about poverty, and live in the homes of others."],
        },
        "jupiter": {  # Mars in Sagittarius/Pisces
            const._SUN:    ["You will be adored by people, dwell in wild or fortified places, and display a harsh edge in character."],
            const._MOON:   ["You may be a quarrelsome scholar, opposing authority."],
            const._MERCURY:["You will be very clever, learned, and agreeable, with a sculptor’s talent."],
            const._JUPITER:["You may leave your homeland, live without wife or comforts, and be perpetually battling foes."],
            const._VENUS:  ["You may be addicted to women, while still enjoying many comforts."],
            const._SATURN: ["You may live a servile and wandering life, with poor looks and sinful tendencies."],
        },
        "saturn": {  # Mars in Capricorn/Aquarius
            const._SUN:    ["You will be aggressive and brave, with wealth, a spouse, and progeny promised."],
            const._MOON:   ["You may face strain with your mother, fickle friendships, and displacement from your residence."],
            const._MERCURY:["You may speak very sweetly yet remain poor or weak, while being deceitful and irreligious."],
            const._JUPITER:["You will live long, remain handsome, enjoy royal favour, and be blessed with brothers."],
            const._VENUS:  ["You may be quarrelsome and hen-pecked, but still enjoy abundant comforts."],
            const._SATURN: ["You may be very wealthy, with an aversion to women, many children, great learning, a king-like aura, and battle valour."],
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
                f"<p class='text-left mt-2'><strong>"
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
        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        f"{int((sb_val_x/1020)*100)}%</p>"
        )

        block = (
            f"<p class='text-left mb-1'><em>When {PLANET_NAMES[asp_pid]} aspects Mars in "
            f"{SIGN_NAMES[mars_sign]}:</em></p>"
            + "".join(f"<p class='text-left mb-1'>• {t}</p>" for t in lines)
            #+ md_html + weak_html
        )
        sub_sections_mars.append(block)

    mars_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Mars (by sign of Mars)</h3>"
        + "".join(sub_sections_mars if sub_sections_mars else [
            "<p class='text-left mb-1'><em>No qualifying planetary aspects to Mars found for the current rules.</em></p>"
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
                "You are straightforward in speech, bond well with siblings, and enjoy tangible comforts."
            ],
            const._MOON: [
                "You are drawn to dance, music, and sensual pleasures. You may be fond of women and sometimes face moral temptations, but you will have access to staff and vehicles."
            ],
            const._MARS: [
                "You may be prone to quarrels or speaking untruths, yet you remain articulate, learned, and very wealthy. Excessive thirst could trouble you."
            ],
            const._JUPITER: [
                "You will experience wealth and contentment, with a pleasing and soft nature."
            ],
            const._VENUS: [
                "You are persuasive and courteous, winning the trust of others, especially women."
            ],
            const._SATURN: [
                "You may display a harsh streak, combining courage with suffering—aggressive yet carrying inner misery."
            ],
        },
        "venus": {  # Mercury in Taurus/Libra
            const._SUN: [
                "Your health may feel fragile, and you may face humiliations or servility, with resources feeling tight."
            ],
            const._MOON: [
                "You will enjoy wealth and reputation, be reliable, healthy, and may serve the establishment."
            ],
            const._MARS: [
                "Rivals and illness may trouble you, and authority could humble you, leaving pleasures diminished."
            ],
            const._JUPITER: [
                "You are learned and trusted, well-known in your community, and carry leadership potential."
            ],
            const._VENUS: [
                "You are fortunate, fond of fine clothes and ornaments, and carry a youthful charm that attracts young women."
            ],
            const._SATURN: [
                "Your comforts may be stripped, and you may face strain from spouse, children, or friends."
            ],
        },
        "own": {  # Mercury in Gemini/Virgo
            const._SUN: [
                "You incline toward truth, have a pleasant appearance, and are favoured by authority."
            ],
            const._MOON: [
                "You love scriptures and speak sweetly, yet you may talk excessively and have a quarrelsome edge."
            ],
            const._MARS: [
                "You are well-liked and useful to those in power, but you may also indulge in backbiting."
            ],
            const._JUPITER: [
                "You may attain high government status, showing bravery, wealth, and a presentable personality."
            ],
            const._VENUS: [
                "You shine with scholarly polish, work for rulers, remain steadfast in friendships, and may encounter entanglements with wayward women."
            ],
            const._SATURN: [
                "You are kind-hearted, complete what you begin, and gain wealth through perseverance."
            ],
        },
        "moon": {  # Mercury in Cancer
            const._SUN: [
                "You have a craftsman’s touch, working with garlands, building, or polishing as livelihood."
            ],
            const._MOON: [
                "Your vitality may be drained through women, and a weak constitution could limit comforts."
            ],
            const._MARS: [
                "Your education may remain limited, yet you speak much, appear attractive, and tell agreeable lies, with a tendency toward thievery."
            ],
            const._JUPITER: [
                "You are wise, humane, and fortunate, respected for your learning and appreciated by the state."
            ],
            const._VENUS: [
                "You appear attractive like Kāma, with a sweet tongue and love for dance and music."
            ],
            const._SATURN: [
                "You may fall into deceitful or ungrateful patterns, with risk of imprisonment."
            ],
        },
        "sun": {  # Mercury in Leo
            const._SUN: [
                "You may show jealousy, servility, harshness, or fickleness, becoming shameless under pressure."
            ],
            const._MOON: [
                "You are well put together, capable, wealthy, fond of poetry, dance, and music, and dress well."
            ],
            const._MARS: [
                "Unwise decisions may bring harm, and you are at risk of bodily injuries."
            ],
            const._JUPITER: [
                "Your constitution may be tender, yet you possess razor-sharp intellect, are an impressive speaker, and may hold high rank."
            ],
            const._VENUS: [
                "You have good looks, seek pleasures, and accumulate wealth."
            ],
            const._SATURN: [
                "You may be tall but afflicted, with foul body odour making social interactions difficult."
            ],
        },
        "jupiter": {  # Mercury in Sagittarius/Pisces
            const._SUN: [
                "You are brave and cool-tempered, though you may face issues like kidney problems, stones, or diabetes."
            ],
            const._MOON: [
                "You have a writer’s talent, a pleasant presence, and are well-liked and supported by friends."
            ],
            const._MARS: [
                "You may combine writing skill with an underworld tint, carrying the archetype of a leader among thieves."
            ],
            const._JUPITER: [
                "You are very learned, blessed with superb memory, pious, handsome, and hold high station with treasurer-like trust."
            ],
            const._VENUS: [
                "You have a ministerial flair, are youthful and brave, but may be prone to theft."
            ],
            const._SATURN: [
                "You may live in forts or forests, have a voracious appetite, yet display wickedness or incompetence."
            ],
        },
        "saturn": {  # Mercury in Capricorn/Aquarius
            const._SUN: [
                "You are likely to have a big family, a rough nature, and talent in wrestling or grappling. You may carry a voracious appetite and notoriety."
            ],
            const._MOON: [
                "Your earnings may come from liquids, water trades, or liquor commerce, but cowardice may also appear."
            ],
            const._MARS: [
                "You may be shy and low in activity, yet decent in nature and able to build wealth."
            ],
            const._JUPITER: [
                "You are very wealthy, prominent, and may become a leader in your town or village."
            ],
            const._VENUS: [
                "You may have many children, though your looks may be lacking. You are highly sensual and may be tied to a wayward spouse."
            ],
            const._SATURN: [
                "You may live a sinner’s arc, facing poverty, servility, misery, and destitution."
            ],
        },
    }

    # Build sub-sections only for planets that truly aspect Mercury now
    sub_sections_mercury = []
    for aspector_pid, lines in P_MERCURY[_host].items():
        if not _does_aspect(aspector_pid, merc_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Mercury in {SIGN_NAMES[merc_sign]}:"
        block = [f"<p class='text-left mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-left mb-1'>• {t}</p>" for t in lines]

        # per-aspecting-graha MD window
        mdx = _md_period_for(aspector_pid)
        #if mdx:
        #    s, e = mdx
        #    block.append(
        #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
        #    )

        # per-aspecting-graha weakness note (any of the four conditions)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        #if is_weak_aspector:
        #    block.append(
        #    weak_html = (
        #        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        #        f"{int((sb_val_x/1020)*100)}%</p>"
        #    ))

        sub_sections_mercury.append("".join(block))

    if not sub_sections_mercury:
        sub_sections_mercury.append("<p class='text-left mb-1'>No classical aspect on Mercury is exact by canonical aspects right now.</p>")

    mercury_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Mercury (by sign of Mercury)</h3>"
        f"<p class='text-left mb-1'><em>Mercury is in {host_label}.</em></p>"
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
                "You are deeply pious and truthful, likely to become famous and may have a hairy body."
            ],
            const._MOON: [
                "You will be soft-spoken, well-liked by your spouse, religiously inclined, and scholarly in nature."
            ],
            const._MARS: [
                "You will be brave and forceful, able to crush the pride of opponents, and capable of commanding groups."
            ],
            const._MERCURY: [
                "You may have cheating tendencies, point out others’ faults, appear outwardly polite, yet sometimes resort to lies."
            ],
            const._VENUS: [
                "You may show a streak of cowardice while enjoying finery, women, and sensual pleasures."
            ],
            const._SATURN: [
                "You may appear unattractive, display greed, and have friendships that are unstable."
            ],
        },
        "venus": {  # Jupiter in Taurus/Libra
            const._SUN: [
                "You are a wandering, learned type, likely to serve those in authority, and will gain vehicles or cattle."
            ],
            const._MOON: [
                "You will be very wealthy, attractive, adored by women, and somewhat indulgent."
            ],
            const._MARS: [
                "You will be favoured by rulers, liked by women and children, and become both learned and wealthy."
            ],
            const._MERCURY: [
                "You will be learned, clever, likeable, virtuous, and good-looking."
            ],
            const._VENUS: [
                "You are likely to be wealthy and famous, maintain clean habits, and enjoy many comforts."
            ],
            const._SATURN: [
                "You will be scholarly and wealthy, capable of leading a town or village, but may appear unclean and be shunned by women."
            ],
        },
        "mercury": {  # Jupiter in Gemini/Virgo
            const._SUN: [
                "You will likely head a village or town, have a large family, and be widely known."
            ],
            const._MOON: [
                "You will be virtuous, very famous and wealthy, favoured by your mother, and blessed with excellent qualities."
            ],
            const._MARS: [
                "Your life will involve constant sensuality; you will be victorious, wealthy, admirable, but may bear injury scars."
            ],
            const._MERCURY: [
                "You may excel as an astrologer, savant, or sculptor, be articulate, and blessed with spouse and children."
            ],
            const._VENUS: [
                "You will gain wealth, spouse, progeny, lands, and houses, though you may also be addicted to wayward women."
            ],
            const._SATURN: [
                "You will lead a town or city, enjoy good looks, and be honoured by those in authority."
            ],
        },
        "moon": {  # Jupiter in Cancer
            const._SUN: [
                "You may experience a loss of your wife’s wealth or children, but later recover all and command men."
            ],
            const._MOON: [
                "You will control treasury, be wealthy, hold high status, and enjoy many comforts."
            ],
            const._MARS: [
                "You may marry a young girl, become wealthy, scholarly, and bear injury marks."
            ],
            const._MERCURY: [
                "You will support your brothers, gain wealth, be quarrelsome yet trustworthy."
            ],
            const._VENUS: [
                "You will have many wives, achieve great fame, and live a fortunate life."
            ],
            const._SATURN: [
                "You will lead a village, town, or army, be very talkative, and enjoy sensual comforts in old age."
            ],
        },
        "sun": {  # Jupiter in Leo
            const._SUN: [
                "You will overspend, yet be famous, kind-hearted, and carry a kingly bearing."
            ],
            const._MOON: [
                "You will be exceptionally fortunate, gaining wealth through your wife’s help."
            ],
            const._MARS: [
                "You will remain loyal to preceptors and friends, perform hard tasks, be pious yet harsh, and emerge as a leader."
            ],
            const._MERCURY: [
                "You may have a builder’s or scientific bent, possess strong oratory, and be both ministerial and scholarly."
            ],
            const._VENUS: [
                "You will be fond of women, gain status via the ruler, and remain robust."
            ],
            const._SATURN: [
                "You may talk too much, lack comforts, face defeat in battle, and see your status fall."
            ],
        },
        "own": {  # Jupiter in Sagittarius/Pisces
            const._SUN: [
                "You may clash with authority and be shunned by friends and relatives."
            ],
            const._MOON: [
                "You will enjoy many comforts, be desired by women, and take pride in your wealth and status."
            ],
            const._MARS: [
                "You may be wounded in battle, come across as harsh or harmful, but still be helpful to others."
            ],
            const._MERCURY: [
                "You may take on a ministerial or kingly role, please all, and gain wealth, sons, and good fortune."
            ],
            const._VENUS: [
                "You are likely to be wealthy, content, famous, learned, and long-lived."
            ],
            const._SATURN: [
                "You may display unclean habits, cowardice, and face a loss of standing."
            ],
        },
        "saturn": {  # Jupiter in Capricorn/Aquarius
            const._SUN: [
                "You will be learned and kingly, attractive, brave, and enjoy numerous comforts."
            ],
            const._MOON: [
                "You will have a keen mind, be religious, proud yet respectful to your parents, and remain wealthy and learned."
            ],
            const._MARS: [
                "You will be brave, fight for the ruler, appear arrogant yet courageous, and be honoured."
            ],
            const._MERCURY: [
                "You may easily yield to women, lead groups, be rich and religious, take on a driver or vehicle-related role, and have many friends."
            ],
            const._VENUS: [
                "You will attract women and enjoy abundant pleasures and possessions."
            ],
            const._SATURN: [
                "You will have high moral fibre, be learned, famous, king-like, and fond of comforts."
            ],
        },
    }

    # Build html for only those planets actually aspecting Jupiter now
    sub_sections_jupiter = []
    for aspector_pid, lines in P_JUPITER[_host].items():
        if not _does_aspect(aspector_pid, jup_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Jupiter in {SIGN_NAMES[jup_sign]}:"
        block = [f"<p class='text-left mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-left mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdx = _md_period_for(aspector_pid)
        #if mdx:
        #    s, e = mdx
        #    block.append(
        #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
        #    )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        #if is_weak_aspector:
        #    block.append(
        #        weak_html = (
        #        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        #        f"{int((sb_val_x/1020)*100)}%</p>"
        #    ))

        sub_sections_jupiter.append(''.join(block))

    if not sub_sections_jupiter:
        sub_sections_jupiter.append("<p class='text-left mb-1'>No canonical aspect on Jupiter is present right now.</p>")

    jupiter_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Jupiter (by sign of Jupiter)</h3>"
        f"<p class='text-left mb-1'><em>Jupiter is in {host_label}.</em></p>"
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
                "You are favoured by rulers or those in authority. However, you may face torment through your wife. At the same time, you are scholarly and learned."
            ],
            const._MOON: [
                "You may be very fickle and restless, with a risk of incarceration. Your life is driven strongly by excessive sexual urges."
            ],
            const._MARS: [
                "You are likely to suffer loss of money and social status, and may find yourself in servile situations."
            ],
            const._MERCURY: [
                "You may come across as hard-hearted and wicked, shunned by relatives, and inclined to earn through illegitimate means."
            ],
            const._JUPITER: [
                "You are blessed with good looks, charitable nature, and tall stature. You may find a good spouse and display pleasant manners."
            ],
            const._SATURN: [
                "You may appear indolent and unattractive, wandering without stability. There is a tendency toward thievery and keeping secret possessions."
            ],
        },
        "own": {  # Venus in Taurus/Libra
            const._SUN: [
                "You are blessed with a beautiful spouse, association with attractive women, and wealth."
            ],
            const._MOON: [
                "You are supported by a virtuous mother and blessed with sons, wealth, status, and good looks. At the same time, you may consort with women of easy morals."
            ],
            const._MARS: [
                "You may be deprived of home comforts, yet remain sensuous. Conflicts tend to leave you subdued or defeated."
            ],
            const._MERCURY: [
                "You are learned, well-mannered, and sensuous. Your virtuous nature brings you fame."
            ],
            const._JUPITER: [
                "You obtain your desires—whether friends, women, children, vehicles, or houses."
            ],
            const._SATURN: [
                "You may be poor, wicked, and sickly, married to a difficult or immoral woman."
            ],
        },
        "mercury": {  # Venus in Gemini/Virgo
            const._SUN: [
                "You may find yourself serving women, yet you are wise, affluent, and enjoy many comforts."
            ],
            const._MOON: [
                "You are gifted with beautiful hair and eyes, a youthful appearance, and enjoy many comforts in life."
            ],
            const._MARS: [
                "You are fortunate and sensuous, skilful in sexual matters, but prone to wasting money on women."
            ],
            const._MERCURY: [
                "You are learned, good-looking, and wealthy, and may naturally rise to lead a group or community."
            ],
            const._JUPITER: [
                "You may become a preceptor or teacher, or develop artistic or photographic talents, while enjoying many comforts."
            ],
            const._SATURN: [
                "You are likely to experience humiliation and misery, being shunned by people."
            ],
        },
        "moon": {  # Venus in Cancer
            const._SUN: [
                "You are quick-tempered, with a wealthy spouse, though often troubled by opponents."
            ],
            const._MOON: [
                "Your first child may be a daughter followed by sons, and you will treat both mother and step-mother equally."
            ],
            const._MARS: [
                "You are a master of several arts, wealthy, and favourable toward relatives, though troubled by women."
            ],
            const._MERCURY: [
                "You are learned, blessed with a learned spouse, wealthy, and inclined toward wandering."
            ],
            const._JUPITER: [
                "You are blessed with wealth, children, servants, vehicles, and friends, and favoured by authority."
            ],
            const._SATURN: [
                "You may be overpowered by women, poor, fallen, and deprived of comforts."
            ],
        },
        "sun": {  # Venus in Leo
            const._SUN: [
                "You may be jealous and strongly driven by desire. Much of your earnings may come through women."
            ],
            const._MOON: [
                "You may have an inconsistent nature, experience the presence of two mothers, and be famed, yet suffer due to women."
            ],
            const._MARS: [
                "You are favoured by rulers and become famous. You are fond of women, addicted to others’ wives, and wealthy."
            ],
            const._MERCURY: [
                "You may be hoarding and greedy, inclined to falsehood, and driven by excessive lust."
            ],
            const._JUPITER: [
                "You attain high status, are surrounded by many women and children, and remain rich."
            ],
            const._SATURN: [
                "You may achieve king-like stature, be good-looking, and have a spouse who may be a widow."
            ],
        },
        "jupiter": {  # Venus in Sagittarius/Pisces
            const._SUN: [
                "You may be short-tempered yet learned, wealthy, and strong, often travelling abroad."
            ],
            const._MOON: [
                "You are famous and very strong, enjoying numerous physical pleasures."
            ],
            const._MARS: [
                "You may have an aversion to women but enjoy varied comforts, showing natural leadership."
            ],
            const._MERCURY: [
                "You delight in ornaments, good dress, fine food, and vehicles."
            ],
            const._JUPITER: [
                "You may have many wives and children, great wealth, and abundant sensual pleasures."
            ],
            const._SATURN: [
                "You are fortunate, rich, indulgent, and a good earner."
            ],
        },
        "saturn": {  # Venus in Capricorn/Aquarius
            const._SUN: [
                "You have a steady nature and may become famous, wealthy, powerful, and truthful."
            ],
            const._MOON: [
                "You are valorous, powerful, attractive, and wealthy."
            ],
            const._MARS: [
                "You may struggle with sickliness, exhaustion from labour, and poverty."
            ],
            const._MERCURY: [
                "You are learned, accumulate wealth, speak truthfully, and are highly scholarly."
            ],
            const._JUPITER: [
                "You remain youthful, love music and scents, associate with worthy women, and are fond of finery."
            ],
            const._SATURN: [
                "You may have a darker complexion, yet are blessed with servants and many comforts."
            ],
        },
    }

    # Build html for only those planets actually aspecting Venus now
    sub_sections_venus = []
    for aspector_pid, lines in P_VENUS[_host_v].items():
        if not _does_aspect(aspector_pid, v_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Venus in {SIGN_NAMES[v_sign]}:"
        block = [f"<p class='text-left mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-left mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdv = _md_period_for(aspector_pid)
        #if mdv:
        #    s, e = mdv
        #    block.append(
        #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
        #    )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        #if is_weak_aspector:
        #    block.append(
        #        weak_html = (
        #        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        #        f"{int((sb_val_x/1020)*100)}%</p>"
        #    ))

        sub_sections_venus.append(''.join(block))

    if not sub_sections_venus:
        sub_sections_venus.append("<p class='text-left mb-1'>No canonical aspect on Venus is present right now.</p>")

    venus_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Venus (by sign of Venus)</h3>"
        f"<p class='text-left mb-1'><em>Venus is in {host_label_v}.</em></p>"
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
            const._SUN:     ["You may turn towards agriculture and are likely to be wealthy, with a strong inclination towards tending cattle."],
            const._MOON:    ["You may find yourself keeping low company, sometimes fickle or drawn to wicked ways, and may be attracted to coarse or less conventional partners."],
            const._MARS:    ["You could become wretched and cruel to animals, inclined to lead thieves, and may indulge excessively in meat, women, and wine."],
            const._MERCURY: ["You may often be quarrelsome, irreligious, and voracious, gaining notoriety as a thief."],
            const._JUPITER: ["You are likely to be religious and fortunate, enjoying high status with rulers, living like a minister, and possessing wealth."],
            const._VENUS:   ["Your life may feel ever-changing, with an ill appearance, addictions towards others’ spouses, and periods of destitution."],
        },
        "venus": {  # Saturn in Taurus/Libra
            const._SUN:     ["You may lack wealth, though you will be learned, weak in body, yet gifted with clear speech."],
            const._MOON:    ["You may attain high status with rulers, receive help from women, and enjoy fine clothes and ornaments."],
            const._MARS:    ["You are likely to be skilled in warfare, kind-hearted, talkative, and rich."],
            const._MERCURY: ["You may be very witty, eager to please women, and enjoy favour from rulers."],
            const._JUPITER: ["You are inclined to help others, show charity, and display great skill."],
            const._VENUS:   ["You may be favoured by rulers, gain from gems, and indulge in wine and women."],
        },
        "mercury": {  # Saturn in Gemini/Virgo
            const._SUN:     ["You may be deprived of wealth, pleasures, and anger, yet remain religious and content."],
            const._MOON:    ["You are likely to be king-like, with soft skin, loved and respected by women."],
            const._MARS:    ["You may be a fighter or wrestler, wise, possibly bearing a limb defect, yet well-known."],
            const._MERCURY: ["You are likely to be wealthy, skilled in fighting and dance, and talented as a singer, painter, or sculptor."],
            const._JUPITER: ["You may be favoured by rulers, virtuous, and liked by friends."],
            const._VENUS:   ["You are fond of women, versed in Yoga-śāstra, and adept at serving women."],
        },
        "moon": {  # Saturn in Cancer
            const._SUN:     ["You may face early loss of father and live without money, spouse, or comforts, leading at times towards sinful actions."],
            const._MOON:    ["You are likely to be wealthy, though your presence may harm your mother and brothers."],
            const._MARS:    ["You may lack strength, yet be favoured by rulers, though often anxious or worrisome."],
            const._MERCURY: ["You may become a wanderer, deceitful and harsh, though an effective orator."],
            const._JUPITER: ["You will have friends, sons, lands, and houses, and be wealthy."],
            const._VENUS:   ["Despite a good birth, you may be deprived of many comforts."],
        },
        "sun": {  # Saturn in Leo
            const._SUN:     ["You may find yourself without money or comforts, poor in qualities, prone to lying, fond of drink, slim, and at times miserable."],
            const._MOON:    ["You may gain fame, wealth, women, and gems, and enjoy the favour of rulers."],
            const._MARS:    ["You may become a wanderer, dwelling in forts or mountains, and known as a cruel fighter."],
            const._MERCURY: ["You may show deceit, indolence, poverty, and ugliness."],
            const._JUPITER: ["You may lead a village, town, or group, and be both wealthy and virtuous."],
            const._VENUS:   ["You are likely to be good-looking and wealthy, though troubled by women."],
        },
        "jupiter": {  # Saturn in Sagittarius/Pisces
            const._SUN:     ["You may become famous and show a fondness for other people’s children."],
            const._MOON:    ["Though motherless, you may still be blessed with a wife, sons, and riches."],
            const._MARS:    ["You may suffer from vaata-related ailments and spend time in foreign lands."],
            const._MERCURY: ["You may be king-like, respectable, rich, and good-looking."],
            const._JUPITER: ["You may rise equal to a king, command armies, and wield great power."],
            const._VENUS:   ["You are likely to live abroad, have two mothers or fathers, and pursue many interests at once."],
        },
        "own": {  # Saturn in Capricorn/Aquarius
            const._SUN:     ["You may be sickly, with an unattractive spouse, wandering and miserable, often carrying burdens."],
            const._MOON:    ["You will have wealth and a wife, yet be opposed to your mother, with tendencies towards sexual excess."],
            const._MARS:    ["You may be courageous, famous, and powerful, a leader of multitudes, though harsh in nature."],
            const._MERCURY: ["You may be powerful, quick-tempered, and famous, though limited in wealth."],
            const._JUPITER: ["You are likely to be famous, virtuous, long-lived, healthy, and handsome."],
            const._VENUS:   ["You may be very wealthy, sensuous, addicted to others’ wives, and inclined towards breaking social norms."],
        },
    }

    # Build html for only those planets actually aspecting Saturn now
    sub_sections_sat = []
    for aspector_pid, lines in P_SATURN[_host_s].items():
        if not _does_aspect(aspector_pid, sat_house_idx):
            continue

        title = f"{PLANET_NAMES.get(aspector_pid)} aspecting Saturn in {SIGN_NAMES[sat_sign]}:"
        block = [f"<p class='text-left mb-1'><strong>{title}</strong></p>"]
        block += [f"<p class='text-left mb-1'>• {t}</p>" for t in lines]

        # Mahadasha window for the aspecting graha
        mdv = _md_period_for(aspector_pid)
        #if mdv:
        #    s, e = mdv
        #    block.append(
        #        f"<p class='text-left mt-2'><strong>The above effects would be more prominent in the mahadasha of {PLANET_NAMES.get(aspector_pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>"
        #    )

        # Weakness check for the aspecting graha (avasthas + shadbala)
        sbv = _extract_shadbala_val(sb_res, aspector_pid)
        sb_is_weak = (aspector_pid in SHAD_THRESH and sbv is not None and sbv < SHAD_THRESH[aspector_pid])
        is_weak_aspector = (
            aspector_pid in avs["bala"] or
            aspector_pid in avs["mrita"] or
            aspector_pid in avs["sushupti"] or
            sb_is_weak
        )
        #if is_weak_aspector:
        #    block.append(
        #        weak_html = (
        #        f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
        #        f"{int((sb_val_x/1020)*100)}%</p>"
        #    ))

        sub_sections_sat.append(''.join(block))

    if not sub_sections_sat:
        sub_sections_sat.append("<p class='text-left mb-1'>No canonical aspect on Saturn is present right now.</p>")

    saturn_aspects_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Aspects on Saturn (by sign of Saturn)</h3>"
        f"<p class='text-left mb-1'><em>Saturn is in {host_label_s}.</em></p>"
        + "".join(sub_sections_sat)
        + "</div>"
    )
    
    # ───────────────────────────────────────────────────────────────────────────
    # Classical Yogas/Doshas – enumerate with evidence and predicted effects
    # Renders a full list instead of counts.
    # Requires: natal_pp, asc_sign, p2h, PLANET_NAMES, _SIGN_LORD, _planet_sign
    # ───────────────────────────────────────────────────────────────────────────

    def _house_of(pid: int) -> int | None:
        """Return 0-based house index for planet, robust to missing p2h entries."""
        h = p2h.get(pid)
        if h is not None:
            return int(h)
        try:
            s = _planet_sign(pid)
            return (s - asc_sign) % 12
        except Exception:
            return None

    def _houses_ruled_by(pid: int) -> list[int]:
        """Return list of 1-based houses ruled by pid for this lagna."""
        out = []
        for h0 in range(12):
            lord = _SIGN_LORD[(asc_sign + h0) % 12]
            if lord == pid:
                out.append(h0 + 1)
        return out

    def _is_conj(p1: int, p2: int) -> bool:
        h1, h2 = _house_of(p1), _house_of(p2)
        return (h1 is not None) and (h1 == h2)

    def _is_exchange(p1: int, p2: int) -> bool:
        """Simple parivartana: lord of house A placed in house B and vice-versa (by house lords)."""
        # find each planet's owned houses and their current houses
        h1_now, h2_now = _house_of(p1), _house_of(p2)
        if h1_now is None or h2_now is None:
            return False
        # If p1 rules the sign in h2_now and p2 rules the sign in h1_now
        lord_h1now = _SIGN_LORD[(asc_sign + h1_now) % 12]
        lord_h2now = _SIGN_LORD[(asc_sign + h2_now) % 12]
        return lord_h2now == p1 and lord_h1now == p2

    def _house_name(h0: int) -> str:
        return ["1st","2nd","3rd","4th","5th","6th","7th","8th","9th","10th","11th","12th"][h0]

    def _planet_name(pid: int) -> str:
        return PLANET_NAMES.get(pid, f"P{pid}")

    def _yoga_item(name: str, evidence: list[str], effects: str) -> dict:
        return {"name": name, "evidence": evidence, "effects": effects}

    def _build_yoga_list() -> list[dict]:
        items: list[dict] = []

        # --- Raja Yoga (kendra–trikona lords association / placement) -------------
        kendra = {1,4,7,10}
        trikona = {1,5,9}
        # collect lords
        k_lords = {h: _SIGN_LORD[(asc_sign + (h-1)) % 12] for h in kendra}
        t_lords = {h: _SIGN_LORD[(asc_sign + (h-1)) % 12] for h in trikona}

        raja_evd: list[str] = []
        # (a) trinal lord in a kendra OR kendra lord in a trine
        for th, tl in t_lords.items():
            h_now = _house_of(tl)
            if h_now in {h-1 for h in kendra}:
                raja_evd.append(f"L{th} ({_planet_name(tl)}) is in a kendra ({_house_name(h_now)})")
        for kh, kl in k_lords.items():
            h_now = _house_of(kl)
            if h_now in {h-1 for h in trikona}:
                raja_evd.append(f"L{kh} ({_planet_name(kl)}) is in a trine ({_house_name(h_now)})")
        # (b) conjunction between any kendra lord and any trikona lord
        for kh, kl in k_lords.items():
            for th, tl in t_lords.items():
                if _is_conj(kl, tl):
                    h0 = _house_of(kl)
                    raja_evd.append(f"L{kh} ({_planet_name(kl)}) conjunct L{th} ({_planet_name(tl)}) in {_house_name(h0)}")
                elif _is_exchange(kl, tl):
                    raja_evd.append(f"L{kh} ({_planet_name(kl)}) in parivartana with L{th} ({_planet_name(tl)})")

        if raja_evd:
            items.append(_yoga_item(
                "Rāja-yoga",
                raja_evd,
                "Because Raja Yoga is active in your chart, you’re wired for visible achievement: you step into roles of authority, gain recognition for real substance, and attract patrons who open doors you once knocked on. Promotions, honors, and tangible comforts—better pay, property, vehicles, and a steadier support system—cluster around the periods when your yogas are triggered, and your work lands in front of the right people with less friction. You navigate institutions well, make decisive calls, and your name starts to carry weight; even early obstacles tend to become ladders you climb. Relationships align with your rise—alliances with capable, respected people—and travel or public-facing projects expand your reach. The fine print: each elevation brings responsibility, so your biggest wins arrive when you lead ethically, share credit, and keep a dharmic compass; do that, and Raja Yoga turns influence into legacy."
            ))

        # --- Dhana Yogas (2/11 and wealth-supporting combinations) ----------------
        L1 = _SIGN_LORD[asc_sign]
        L2 = _SIGN_LORD[(asc_sign + 1) % 12]
        L5 = _SIGN_LORD[(asc_sign + 4) % 12]
        L9 = _SIGN_LORD[(asc_sign + 8) % 12]
        L11 = _SIGN_LORD[(asc_sign + 10) % 12]

        dhana_evd: list[str] = []
        # Core rules
        if _is_conj(L2, L11) or _is_exchange(L2, L11):
            h0 = _house_of(L2)
            dhana_evd.append(f"L2 ({_planet_name(L2)}) associated with L11 ({_planet_name(L11)})"
                             + (f" in {_house_name(h0)}" if h0 is not None else ""))
        hL2, hL11 = _house_of(L2), _house_of(L11)
        if hL2 == 10:  # 11th house is index 10 (0-based)
            dhana_evd.append("L2 placed in 11th (income/gains)")
        if hL11 == 1:  # 2nd house is index 1 (0-based)
            dhana_evd.append("L11 placed in 2nd (accumulation/wealth)")
        # supportive: L1 with L2/11, L5/9 with L2/11
        for p, tag in [(L1,"L1"), (L5,"L5"), (L9,"L9")]:
            if _is_conj(p, L2):
                h0 = _house_of(p)
                dhana_evd.append(f"{tag} ({_planet_name(p)}) conjunct L2 ({_planet_name(L2)}) in {_house_name(h0)}")
            if _is_conj(p, L11):
                h0 = _house_of(p)
                dhana_evd.append(f"{tag} ({_planet_name(p)}) conjunct L11 ({_planet_name(L11)}) in {_house_name(h0)}")

        if dhana_evd:
            items.append(_yoga_item(
                "Dhana-yoga",
                dhana_evd,
                "Because you have Dhana Yoga, you’re primed to attract wealth and build it steadily: income channels open up, savings grow, and opportunities to monetize your skills show up reliably."
            ))

        # --- Gaja-Keśarī (Guru in kendra from Moon) --------------------------------
        moon_h = _house_of(const._MOON)
        jup_h  = _house_of(const._JUPITER)
        if (moon_h is not None) and (jup_h is not None):
            if (jup_h - moon_h) % 12 in {0,3,6,9}:  # 1/4/7/10 from Moon
                items.append(_yoga_item(
                    "Gaja-Keśarī-yoga",
                    [f"Jupiter in a kendra from Moon ({_house_name(jup_h)} from Moon’s sign)"],
                    "Because you have Gaja Kesari yoga, you naturally draw respect and opportunity: you tend to be wealthy, well-known, learned, virtuous, and recognized by people in authority, with the potential for lasting fame."
                ))

        # --- Viparīta Rāja-yoga (6/8/12 lords in other dusthānas or exchange) -----
        L6  = _SIGN_LORD[(asc_sign + 5) % 12]
        L8  = _SIGN_LORD[(asc_sign + 7) % 12]
        L12 = _SIGN_LORD[(asc_sign + 11) % 12]
        vry_evd: list[str] = []
        for p, lbl in [(L6, "L6"), (L8, "L8"), (L12, "L12")]:
            h = _house_of(p)
            if h in {5,7,11}:  # 6th/8th/12th are 5/7/11 in 0-based
                vry_evd.append(f"{lbl} ({_planet_name(p)}) placed in another dusthāna ({_house_name(h)})")
        if _is_exchange(L6, L8) or _is_exchange(L6, L12) or _is_exchange(L8, L12):
            vry_evd.append("Exchange between dusthāna lords (parivartana)")

        if vry_evd:
            items.append(_yoga_item(
                "Viparīta Rāja-yoga",
                vry_evd,
                "This yoga will turn losses, challenges and hardships into success, wealth and status. You will find that when things appear to be falling apart, they are in fact rearranging in your favor. Losses that might devastate others can somehow lead you to better opportunities, and hardships that seem unfair at first eventually open doors to strength, wisdom, and stability. Again and again, you may notice that challenges transform into turning points—pushing you toward unexpected growth and success. This yoga makes you resilient, almost as if you carry a secret blessing that protects and uplifts you through adversity. Where others see misfortune, you find hidden wealth, not just in money, but in stability, reputation, and the ability to rise above. It gives your life a remarkable quality: the power to turn setbacks into steppingstones and to emerge stronger, wealthier, and more secure after every storm."
            ))

        # --- Chandra-Maṅgala (Moon & Mars conjunction) ----------------------------
        if _is_conj(const._MOON, const._MARS):
            h0 = _house_of(const._MOON)
            items.append(_yoga_item(
                "Chandra-Maṅgala-yoga",
                [f"Moon conjunct Mars in {_house_name(h0)}"],
                "Chandra Maṅgala Yoga gives your life a special fire. You are naturally driven, energetic, and ambitious — money, land, and influence may flow to you more easily than to others, and you often find yourself in positions where your determination shines. Opportunities for growth and wealth seem to come your way, and you rarely shy away from hard work. At the same time, this yoga gives you an emotional intensity that others notice — you may sometimes react strongly, become stubborn in your views, or struggle to let go of hurt feelings. Your relationships, especially with your mother or maternal side of the family, may feel complicated, swinging between deep attachment and friction. This combination gives you the power to rise and shine in life, but it also asks you to balance your emotions with patience and understanding, so that your drive brings you not only success, but also peace in your personal life."
            ))

        # --- Pancha-Mahāpurūṣa (single-planet) yogas ----------------------------
        # Helper: exaltation map for PMY (soft fail if consts missing)
        try:
            _EXALT_SIGNS = {
                getattr(const, "_MARS"): 9,      # Mars exalted in Makara (Capricorn)
                getattr(const, "_MERCURY"): 5,  # Mercury exalted in Kanyā (Virgo)
                getattr(const, "_JUPITER"): 3,  # Jupiter exalted in Karka (Cancer)
                getattr(const, "_VENUS"): 11,   # Venus exalted in Mīna (Pisces)
                getattr(const, "_SATURN"): 6,   # Saturn exalted in Tulā (Libra)
            }
        except Exception:
            _EXALT_SIGNS = {}

        def _own_or_exalt(pid: int) -> bool:
            try:
                s = _planet_sign(pid)
                in_own = _SIGN_LORD[s] == pid
                in_exalt = _EXALT_SIGNS.get(pid) == s
                return bool(in_own or in_exalt)
            except Exception:
                return False

        def _in_kendra_from_lagna(pid: int) -> bool:
            h = _house_of(pid)
            return h in {0,3,6,9}

        pmy_defs = [
            (getattr(const, "_MARS", None),   "Rucaka-yoga",   "Mars"),
            (getattr(const, "_MERCURY", None),"Bhadra-yoga",   "Mercury"),
            (getattr(const, "_JUPITER", None),"Haṃsa-yoga",   "Jupiter"),
            (getattr(const, "_VENUS", None),  "Mālavya-yoga", "Venus"),
            (getattr(const, "_SATURN", None), "Śaśa-yoga",   "Saturn"),
        ]
        for pid, yname, pname in pmy_defs:
            if pid is None:
                continue
            if _in_kendra_from_lagna(pid) and _own_or_exalt(pid):
                h0 = _house_of(pid)
                items.append(_yoga_item(
                    yname,
                    [f"{pname} in a kendra ({_house_name(h0)}) in own/exalted sign"],
                    "Pancha-Mahāpurūṣa yoga gives stature, charisma and tangible success. Leadership, resources and public impact rise strongly."
                ))

        # --- Parivartana (exchange) yogas: Maha / Dainya / Khala -----------------
        def _parivartana_category(houses_a: list[int], houses_b: list[int]) -> str:
            good = {1,2,4,5,7,9,10,11}
            trik = {6,8,12}
            all_h = set(houses_a) | set(houses_b)
            # trik-trik is a Viparīta style exchange
            if set(houses_a).issubset(trik) and set(houses_b).issubset(trik):
                return "Viparīta (trik-trik)"
            if all_h & trik:
                return "Dainya"
            if 3 in all_h:
                return "Khala"
            return "Mahā"

        classical = [getattr(const, "_SUN", None), getattr(const, "_MOON", None), getattr(const, "_MARS", None),
                     getattr(const, "_MERCURY", None), getattr(const, "_JUPITER", None), getattr(const, "_VENUS", None), getattr(const, "_SATURN", None)]

        # Emit one item per actual Parivartana found; no generic description when none exist
        for i in range(len(classical)):
            for j in range(i+1, len(classical)):
                p1, p2 = classical[i], classical[j]
                if p1 is None or p2 is None:
                    continue
                if _is_exchange(p1, p2):
                    ha, hb = _houses_ruled_by(p1), _houses_ruled_by(p2)
                    cat = _parivartana_category(ha, hb)
                    h1, h2 = _house_of(p1), _house_of(p2)
                    title_map = {
                        "Mahā": "Mahā Parivartana-yoga",
                        "Dainya": "Dainya Parivartana-yoga",
                        "Viparīta (trik-trik)": "Viparīta Parivartana-yoga",
                        "Khala": "Khala Parivartana-yoga",
                    }
                    desc_map = {
                        "Mahā": "You will get stable growth in status and wealth.",
                        "Dainya": "You will get mixed outcomes— gains will come through contests, debts or service. Manage risk carefully.",
                        "Viparīta (trik-trik)": "Although you will face adversity you will also get unexpected relief and recovery in relevant periods.",
                        "Khala": "You will make effort and hustle, yet receive fluctuating returns.",
                    }
                    title = title_map.get(cat, "Parivartana-yoga")
                    evidence = [
                        f"{_planet_name(p1)} ⇄ {_planet_name(p2)} ({_house_name(h1)} ⇄ {_house_name(h2)})",
                        f"Houses ruled: {','.join(str(h) for h in sorted(ha))} ↔ {','.join(str(h) for h in sorted(hb))}",
                        f"Category: {cat}",
                    ]
                    items.append(_yoga_item(title, evidence, desc_map.get(cat, "Exchange of house-lords joins destinies; outcomes depend on house quality and strength.")))

        # --- Adhi-yoga (benefics 6/7/8 from Moon; no malefics there) -------------
        if moon_h is not None:
            m6, m7, m8 = (moon_h+5)%12, (moon_h+6)%12, (moon_h+7)%12
            benefics = [getattr(const, "_JUPITER", None), getattr(const, "_VENUS", None), getattr(const, "_MERCURY", None)]
            malefics = [getattr(const, "_MARS", None), getattr(const, "_SATURN", None)]
            ben_here = [p for p in benefics if p is not None and _house_of(p) in {m6,m7,m8}]
            mal_here = [p for p in malefics if p is not None and _house_of(p) in {m6,m7,m8}]
            if len(ben_here) >= 2 and not mal_here:
                items.append(_yoga_item(
                    "Adhi-yoga",
                    [f"Benefics in 6/7/8 from Moon; none of Mars/Saturn there"],
                    "Adhi Yoga confers rank, influence and support: calm authority, administrative ability and protection from powerful patrons."
                ))

        # --- Amala-yoga (benefic in 10th from lagna) ------------------------------
        ben10 = [p for p in [getattr(const, "_JUPITER", None), getattr(const, "_VENUS", None), getattr(const, "_MERCURY", None)] if p is not None and _house_of(p) == 9]
        if ben10:
            items.append(_yoga_item(
                "Amala-yoga",
                [f"Benefic in the 10th from lagna: {', '.join(_planet_name(p) for p in ben10)}"],
                "Amala Yoga gives spotless reputation and lasting career credit; your work leaves a clean, admired track-record."
            ))

        # --- Nabhasa yogas (life-pattern) ----------------------------------------
        def _sign_type(idx: int) -> str:
            return 'movable' if idx in {0,3,6,9} else ('fixed' if idx in {1,4,7,10} else 'dual')

        P7 = [getattr(const, "_SUN", None), getattr(const, "_MOON", None), getattr(const, "_MARS", None),
              getattr(const, "_MERCURY", None), getattr(const, "_JUPITER", None), getattr(const, "_VENUS", None), getattr(const, "_SATURN", None)]
        P7 = [p for p in P7 if p is not None]
        signs = [(_planet_sign(p) if _planet_sign(p) is not None else None) for p in P7]
        if all(s is not None for s in signs):
            types = {_sign_type(s) for s in signs}
            if len(types) == 1:
                t = types.pop()
                name = { 'movable': 'Rajju (Aāśraya)', 'fixed': 'Mūsala (Aāśraya)', 'dual': 'Nāla (Aāśraya)'}[t]
                items.append(_yoga_item(
                    f"{name}",
                    [f"All seven classical planets in {t} signs"],
                    "A dominant life-tendency: mobility (Rajju), stability (Mūsala), or adaptable duality (Nāla) shapes choices and outcomes throughout life."
                ))
            # Dala: Maala & Sarpa (Moon excluded by classics)
            ben = [getattr(const, "_JUPITER", None), getattr(const, "_VENUS", None), getattr(const, "_MERCURY", None)]
            mal = [getattr(const, "_SUN", None), getattr(const, "_MARS", None), getattr(const, "_SATURN", None)]
            ben_k = { _house_of(p) for p in ben if p is not None }
            mal_k = { _house_of(p) for p in mal if p is not None }
            kset = {0,3,6,9}
            if len(ben_k & kset) >= 3 and not (mal_k & kset):
                items.append(_yoga_item(
                    "Mālā (Dala) yoga",
                    ["Benefics occupy three kendras; malefics not in kendras"],
                    "Constant enjoyments and resources; supportive environment and comforts."
                ))
            if len(mal_k & kset) >= 3 and not (ben_k & kset):
                items.append(_yoga_item(
                    "Sarpa (Dala) yoga",
                    ["Malefics occupy three kendras; benefics outside kendras"],
                    "Hard-fought progress with periods of pressure, dependence or scarcity sharpening resilience."
                ))
            # Aakriti highlights
            houses = [_house_of(p) for p in P7]
            hs = set(houses)
            # Kamala: all planets in kendras
            if hs and hs.issubset({0,3,6,9}):
                items.append(_yoga_item(
                    "Kamala (Aākṛti) yoga",
                    ["All planets confined to kendras (1/4/7/10)"],
                    "Lotus-like spread of status and protections; prominence and recognition."
                ))
            # Vaapi: all planets outside kendras
            if not (hs & {0,3,6,9}):
                items.append(_yoga_item(
                    "Vāpi (Aākṛti) yoga",
                    ["All planets in non-kendra houses"],
                    "Hoarding/accumulation tendency; steady small comforts and guarded growth."
                ))
            # Gada: all planets in two adjacent kendras
            for pair in [{0,3},{3,6},{6,9},{9,0}]:
                if hs and hs.issubset(pair):
                    items.append(_yoga_item(
                        "Gada (Aākṛti) yoga",
                        [f"All planets in adjacent kendras: {', '.join(_house_name(h) for h in sorted(pair))}"],
                        "Wealth focus and ceaseless earning temperament; results hinge on strength of the occupied kendras."
                    ))
                    break
            # Pakshi (Vihaga): all planets in 4 and 10
            if hs and hs.issubset({3,9}):
                items.append(_yoga_item(
                    "Pakṣi/Vihaga (Aākṛti) yoga",
                    ["All planets distributed between 4th and 10th houses"],
                    "Restless, service-oriented, messenger/mediator roles; frequent movements."
                ))
            # Shakata (Aakriti): all planets in 1 and 7
            if hs and hs.issubset({0,6}):
                items.append(_yoga_item(
                    "Śakata (Aākṛti) yoga",
                    ["All planets distributed between 1st and 7th houses"],
                    "Oscillation between highs and lows; partnerships become pivotal axis of life."
                ))
            # Vajra & Yava
            ben_h = { _house_of(p) for p in ben if p is not None }
            mal_h = { _house_of(p) for p in mal if p is not None }
            if {0,6}.issubset(ben_h) and {3,9}.issubset(mal_h):
                items.append(_yoga_item(
                    "Vajra (Aākṛti) yoga",
                    ["Benefics in 1 & 7; malefics in 4 & 10"],
                    "Strong early and late life comforts with testing middle years; courage with refinement."
                ))
            if {0,6}.issubset(mal_h) and {3,9}.issubset(ben_h):
                items.append(_yoga_item(
                    "Yava (Aākṛti) yoga",
                    ["Malefics in 1 & 7; benefics in 4 & 10"],
                    "Consistent nature, charity and mid-life strength with tested beginnings/endings."
                ))

        # --- Moon-based yogas & Kemadruma logic ----------------------------------
        # Sunāpha: planets (except Sun) in 2nd from Moon
        sunapha_ev, anapha_ev, durudhara_ev = [], [], []
        if moon_h is not None:
            h2 = (moon_h + 1) % 12
            h12 = (moon_h + 11) % 12
            klass = [getattr(const, "_MERCURY", None), getattr(const, "_VENUS", None), getattr(const, "_MARS", None), getattr(const, "_JUPITER", None), getattr(const, "_SATURN", None)]
            if any(_house_of(p) == h2 for p in klass if p is not None):
                sunapha_list = [p for p in klass if p is not None and _house_of(p) == h2]
                sunapha_ev.append(f"Planets in 2nd from Moon: {', '.join(_planet_name(p) for p in sunapha_list)}")
                items.append(_yoga_item(
                    "Sunāpha-yoga",
                    sunapha_ev,
                    "Self-made prosperity and initiative; resources accrue by one’s effort and skills."
                ))
            if any(_house_of(p) == h12 for p in klass if p is not None):
                anapha_list = [p for p in klass if p is not None and _house_of(p) == h12]
                anapha_ev.append(f"Planets in 12th from Moon: {', '.join(_planet_name(p) for p in anapha_list)}")
                items.append(_yoga_item(
                    "Anāpha-yoga",
                    anapha_ev,
                    "Composure, dignity and comforts; a self-contained nature that resists needless dependence."
                ))
            if any(_house_of(p) == h2 for p in klass if p is not None) and any(_house_of(p) == h12 for p in klass if p is not None):
                d_left = [p for p in klass if p is not None and _house_of(p) == h12]
                d_right = [p for p in klass if p is not None and _house_of(p) == h2]
                durudhara_ev.append(f"Planets flank Moon in 12th & 2nd: left({', '.join(_planet_name(p) for p in d_left)}), right({', '.join(_planet_name(p) for p in d_right)})")
                items.append(_yoga_item(
                    "Durdhurā-yoga",
                    durudhara_ev,
                    "Supportive flanks to the mind: resources, allies and continuity surround your initiatives."
                ))

        # --- A few headline doṣas & related checks ----------------------------
        # (1) Śakata-doṣa: Moon in 6/8 from Jupiter
        if (moon_h is not None) and (jup_h is not None) and ((moon_h - jup_h) % 12 in {5,7}):
            items.append(_yoga_item(
                "Śakata-doṣa",
                [f"Moon is {_house_name(moon_h)} from Jupiter (6/8 relationship)"],
                "Highs and lows in fortune; resilience grows by navigating alternating tides of gain and loss."
            ))

        # (2) Kemadruma: only if Sunāpha/Anāpha/Durdhurā absent
        has_moon_support = bool(sunapha_ev or anapha_ev or durudhara_ev)
        if (moon_h is not None) and (not has_moon_support):
            empties = True
            for off in (1, 11):  # 2nd/12th from Moon (0-based offsets)
                any_planet = False
                for p in (getattr(const, "_MERCURY", None), getattr(const, "_VENUS", None), getattr(const, "_MARS", None), getattr(const, "_JUPITER", None), getattr(const, "_SATURN", None)):
                    if p is not None and _house_of(p) == (moon_h + off) % 12:
                        any_planet = True
                        break
                if any_planet:
                    empties = False
                    break
            if empties:
                items.append(_yoga_item(
                    "Kemadruma-doṣa",
                    ["No Mercury/Venus/Mars/Jupiter/Saturn in 2nd and 12th from Moon"],
                    "Periods of isolation and financial strain test self-reliance; remedies come via routine, alliances and lunar strengthening."
                ))

        # (3) Harṣa / Sarala / Vimala (Viparīta subtypes)
        if _house_of(L6) == 5:
            items.append(_yoga_item(
                "Harṣa (Viparīta) rāja-yoga",
                ["L6 placed in the 6th"],
                "Victory over enemies, litigation and illness; strength rises through disciplined service."
            ))
        if _house_of(L8) == 7:
            items.append(_yoga_item(
                "Sarala (Viparīta) rāja-yoga",
                ["L8 placed in the 8th"],
                "Hidden strengths and inheritance-like benefits; transformations empower."
            ))
        if _house_of(L12) == 11:
            items.append(_yoga_item(
                "Vimala (Viparīta) rāja-yoga",
                ["L12 placed in the 12th"],
                "Frugality and detachment convert losses into savings, sanctuary and quiet authority."
            ))

        # (4) Arisṣta (ill-health/misfortune) basic patterns
        arishta_evd: list[str] = []
        if _house_of(L1) in {5,7,11}:
            arishta_evd.append("L1 placed in a dusthāna (6/8/12)")
        for p,lbl in [(L6,"L6"),(L8,"L8"),(L12,"L12")]:
            if _is_conj(L1, p):
                arishta_evd.append(f"L1 conjunct {lbl}")
            if _is_exchange(L1, p):
                arishta_evd.append(f"L1 in parivartana with {lbl}")
        if arishta_evd:
            items.append(_yoga_item(
                "Arisṣta-yogas (basic)",
                arishta_evd,
                "Periods of strain or illness can interrupt rise; prioritize health systems, boundaries and timing."
            ))

        # (5) Daridra (poverty/strain) basic set (simplified; no aspect checks)
        dar_evd: list[str] = []
        if _house_of(L1) == 11 and _house_of(L12) == 0:
            dar_evd.append("L1 in 12th and L12 in 1st")
        if _house_of(L1) == 5 and _house_of(L6) == 0:
            dar_evd.append("L1 in 6th and L6 in 1st")
        # Mars & Saturn together in 2nd
        if _is_conj(getattr(const, "_MARS", None), getattr(const, "_SATURN", None)) and _house_of(getattr(const, "_MARS", None)) == 1:
            dar_evd.append("Mars and Saturn in 2nd house together")
        if _house_of(L2) == 5:
            dar_evd.append("L2 placed in 6th (strain on accumulation)")
        if dar_evd:
            items.append(_yoga_item(
                "Daridra-yogas (basic)",
                dar_evd,
                "Guard wealth during vulnerable periods; build buffers, reduce leverage and diversify income streams."
            ))

        return items

    def _render_yoga_list_html(items: list[dict]) -> str:
        if not items:
            return (
                "<div class='mt-4'>"
                "<h3 class='h6 text-center'>Applicable Yogas & Doṣas</h3>"
                "<p class='text-left'>No classical yoga/doṣa matched the basic rules checked.</p>"
                "</div>"
            )
        parts = ["<div class='mt-4'><h3 class='h6 text-center'>Yogas in Your Chart</h3>"]
        for it in items:
            parts.append(f"<h4 class='h6 text-center mt-3'>{it['name']}</h4>")
            if it.get("evidence"):
                parts.append("<p class='text-left mb-1'><strong>Reason For the Yoga:</strong></p>")
                for ev in it["evidence"]:
                    parts.append(f"<p class='text-left mb-1'>• {ev}</p>")
            if it.get("effects"):
                parts.append(f"<p class='text-left mb-2'><strong>Predicted effects:</strong> {it['effects']}</p>")
        parts.append("</div>")
        return "".join(parts)

    # Build and append the section
    _yoga_items = _build_yoga_list()
    yogas_html = _render_yoga_list_html(_yoga_items)
    
    # ───────────────────────────────────────────────────────────────────────────
    # Deeptādi & Lajjitādi Avasthas – per-planet reading with evidence & MD note
    # ───────────────────────────────────────────────────────────────────────────

    def _house_of(pid: int) -> int | None:
        """0-based house index for pid; fallback from sign if p2h is missing."""
        h = p2h.get(pid)
        if h is not None:
            return int(h)
        try:
            s = _planet_sign(pid)
            return (s - asc_sign) % 12
        except Exception:
            return None

    def _house_name(h0: int) -> str:
        return ["1st","2nd","3rd","4th","5th","6th","7th","8th","9th","10th","11th","12th"][h0]

    def _sign_name(sign: int) -> str:
        return ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"][sign]

    def _planet_name(pid: int) -> str:
        return PLANET_NAMES.get(pid, f"P{pid}")

    # Mūlatrikoṇa signs (sign-only granularity; degrees ignored for robustness)
    _MOOL = {
        const._SUN: 4,       # Leo
        const._MOON: 1,      # Taurus
        const._MARS: 0,      # Aries
        const._MERCURY: 5,   # Virgo
        const._JUPITER: 8,   # Sagittarius
        const._VENUS: 6,     # Libra
        const._SATURN: 10,   # Aquarius
    }

    # Natural malefics / benefics
    NAT_MALEFICS = {const._SUN, const._MARS, const._SATURN, getattr(const, "_RAHU", -1), getattr(const, "_KETU", -2)}
    NAT_BENEFICS = {const._JUPITER, const._VENUS, const._MOON, const._MERCURY}

    # Aspect model: everyone 7th; + special—Mars (4,8), Jupiter (5,9), Saturn (3,10), Rahu/Ketu (5,9)
    def _aspects(asp_pid: int, target_house_idx: int) -> bool:
        src = _house_of(asp_pid)
        if src is None:
            return False
        delta = (target_house_idx - src) % 12
        special = {
            const._MARS: {3, 6, 7},     # 4th, 7th, 8th
            const._JUPITER: {4, 6, 8},  # 5th, 7th, 9th
            const._SATURN: {2, 6, 9},   # 3rd, 7th, 10th
            getattr(const, "_RAHU", -1): {4, 6, 8},
            getattr(const, "_KETU", -2): {4, 6, 8},
        }
        return delta in special.get(asp_pid, {6})

    def _any_aspect_from(group: set[int], target_house_idx: int) -> bool:
        return any(_aspects(p, target_house_idx) for p in group if _house_of(p) is not None)

    def _conj_with(pid: int, group: set[int]) -> set[int]:
        h = _house_of(pid)
        if h is None:
            return set()
        return {p for p in group if p2h.get(p) == h and p != pid}

    def _lord_of_sign(sign: int) -> int:
        return _SIGN_LORD[sign]

    def _relation_to_lord(pid: int, sign: int) -> str:
        """friend / adhi-mitra (mutual friend) / neutral / enemy, based on permanent relations."""
        lord = _lord_of_sign(sign)
        if lord == pid:
            return "own"
        pf = _FRIENDS.get(pid, set())
        pn = _NEUTRALS.get(pid, set())
        if lord in pf and pid in _FRIENDS.get(lord, set()):
            return "adhi-mitra"
        if lord in pf:
            return "friend"
        if lord in pn:
            return "neutral"
        return "enemy"

    def _deeptadi_for(pid: int) -> list[tuple[str, list[str], str]]:
        """Return [(avastha_name, evidence_lines, effects_para), ...] for Deeptādi."""
        out = []
        sign = _planet_sign(pid)
        hidx = _house_of(pid)
        if sign is None or hidx is None:
            return out
        name = _planet_name(pid)
        lord = _lord_of_sign(sign)
        rel = _relation_to_lord(pid, sign)

        # 1 Deepta – exaltation or moolatrikona
        if sign == _EXALTS.get(pid) or sign == _MOOL.get(pid):
            ev = [f"{name} is {'exalted' if sign == _EXALTS.get(pid) else 'in moolatrikona'} in {_sign_name(sign)}"]
            out.append(("Deepta (luminous)", ev,
                        "Tends to confer high status, courage, wealth and comforts—including vehicles and official favours—during its periods."))

        # 2 Swastha – own sign
        if _SIGN_LORD[sign] == pid:
            out.append(("Swastha (stable)", [f"{name} is in its own sign ({_sign_name(sign)})"],
                        "Supports health, learning and reputation; often brings property, spousal support, patronage and a turn toward dharma."))

        # 3 Mudita – in sign of an adhi-mitra (great friend)
        if rel == "adhi-mitra":
            out.append(("Mudita (delighted)", [f"{name} is in the sign of a mutual friend (adhi-mitra): lord {_planet_name(lord)}"],
                        "Promises money, fine clothes and fragrances, vehicles and ornaments, alongside genuine interest in religious practice."))

        # 4 Shanta – in friend’s sign
        if rel == "friend":
            out.append(("Shanta (quiescent)", [f"{name} is in the sign of a permanent friend: lord {_planet_name(lord)}"],
                        "Brings favour from authority, abundant comforts and lands, and a calmer mind suited to scripture and meditation."))

        # 5 Deena – neutral’s sign
        if rel == "neutral":
            out.append(("Deena (deficient)", [f"{name} is in a neutral’s sign: lord {_planet_name(lord)}"],
                        "Points to changes of role or residence, friction with close ones, humiliation and bouts of ill-health."))

        # 6 Dukhi – enemy’s sign
        if rel == "enemy":
            out.append(("Dukhi (tormented)", [f"{name} is in an enemy’s sign: lord {_planet_name(lord)}"],
                        "Suggests displacement/foreign stay, separations, and fears tied to theft, fire or official censure."))

        # 7 Vikala – associated with a malefic (conjunction)
        mal_conj = _conj_with(pid, NAT_MALEFICS)
        if mal_conj:
            out.append(("Vikala (grief-stricken)",
                        [f"{name} is conjoined with malefic(s): " + ", ".join(_planet_name(p) for p in sorted(mal_conj))],
                        "Mental strain, separations from friends, and troubles involving spouse/children or theft can color its periods."))

        # 8 Khala – in the sign of a malefic (sign lord = natural malefic)
        if _lord_of_sign(sign) in NAT_MALEFICS:
            out.append(("Khala (wicked)", [f"Sign lord is a natural malefic: {_planet_name(_lord_of_sign(sign))}"],
                        "Inclines toward quarrels, paternal estrangement and loss of lands/wealth, with humiliation from one’s own circle."))

        # 9 Kruddha – associated with Sun (combust)
        if pid != const._SUN:
            combust = pid in _combust_set
            sun_conj = const._SUN in _conj_with(pid, {const._SUN})
            if combust or sun_conj:
                reason = "combust by Sun" if combust else "conjoined with Sun"
                out.append(("Kruddha (angered)", [f"{name} is {reason} in {_house_name(hidx)}"],
                            "Leads to rash, unwholesome choices; losses involving money and family; and vulnerability to eye issues."))
        return out

    def _lajjitadi_for(pid: int) -> list[tuple[str, list[str], str]]:
        """Return [(avastha_name, evidence_lines, effects_para), ...] for Lajjitādi."""
        out = []
        name = _planet_name(pid)
        sign = _planet_sign(pid)
        hidx = _house_of(pid)
        if sign is None or hidx is None:
            return out
        lord = _lord_of_sign(sign)
        rel = _relation_to_lord(pid, sign)
        KETU = getattr(const, "_KETU", -2)
        RAHU = getattr(const, "_RAHU", -1)

        # 1 Lajjita – in 5th with Sun/Mars/Saturn/Rahu/Ketu
        mal_set = {const._SUN, const._MARS, const._SATURN, RAHU, KETU}
        if hidx == 4:  # 5th house
            offenders = _conj_with(pid, mal_set)
            if offenders:
                out.append(("Lajjita (abashed)",
                            [f"{name} in 5th conjoined with: " + ", ".join(_planet_name(p) for p in sorted(offenders))],
                            "Leads to rreligious drift, poor judgement, quarrels and wandering; also strain or illness tied to children."))

        # 2 Garvita – exaltation or moolatrikona
        if sign == _EXALTS.get(pid) or sign == _MOOL.get(pid):
            out.append(("Garvita (conceited)",
                        [f"{name} is {'exalted' if sign == _EXALTS.get(pid) else 'in moolatrikona'} ({_sign_name(sign)})"],
                        "Leads to rise in rank and recognition, learning and wealth, new property and an upswing in business/comforts."))

        # 3 Kshudhita – in enemy sign AND aspected/associated by an enemy (esp. Saturn)
        enemies = {p for p in (const._SUN, const._MOON, const._MARS, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN)
                   if p not in _FRIENDS.get(pid, set()) and p not in _NEUTRALS.get(pid, set()) and p != pid}
        sat_hit = False
        if _relation_to_lord(pid, sign) == "enemy":
            enemy_conj = _conj_with(pid, enemies)
            enemy_asp = {p for p in enemies if _aspects(p, hidx)}
            if enemy_conj or enemy_asp:
                if const._SATURN in (enemy_conj | enemy_asp):
                    sat_hit = True
                ev = [f"{name} in enemy's sign ({_planet_name(lord)})"]
                if enemy_conj:
                    ev.append("Conjoined with enemy: " + ", ".join(_planet_name(p) for p in sorted(enemy_conj)))
                if enemy_asp:
                    ev.append("Aspected by enemy: " + ", ".join(_planet_name(p) for p in sorted(enemy_asp)))
                out.append(("Kshudhita (hungry)", ev,
                            "Leads to sorrow, mental agitation, trouble from opponents and money drain; reasoning feels off and energy flags"
                            + ("—notably harsher with Saturn involved." if sat_hit else ".")))

        # 4 Trishita – watery sign + malefic aspect + no benefic aspect
        watery = {3, 7, 11}  # Cancer, Scorpio, Pisces
        if sign in watery:
            mal_asp = _any_aspect_from(NAT_MALEFICS, hidx)
            ben_asp = _any_aspect_from(NAT_BENEFICS, hidx)
            if mal_asp and not ben_asp:
                out.append(("Trishita (thirsty)",
                            [f"{name} in watery sign ({_sign_name(sign)})", "Aspected by malefic(s) and not by benefics"],
                            "Leads to ailments via indulgence/relationships, a slide toward questionable acts, loss of wealth and humiliation."))

        # 5 Mudita – friend’s sign AND friendly association/aspect AND Jupiter support
        if rel in {"friend", "adhi-mitra"}:
            friendly = set(_FRIENDS.get(pid, set()))
            support = (_conj_with(pid, friendly) or _any_aspect_from(friendly, hidx)) and \
                      (_aspects(const._JUPITER, hidx) or const._JUPITER in _conj_with(pid, {const._JUPITER}))
            if support:
                details = [f"{name} in friend’s sign ({_planet_name(lord)})",
                           "Supported by friendly grahas",
                           ("Jupiter aspects/conjoins the planet")]
                out.append(("Mudita (delighted)",
                            details,
                            "Expect fine garments/ornaments, a roomy residence, pleasures and lands, victory over foes and progress in learning."))

        # 6 Kshobhita – associated with Sun (combust) AND aspected by malefics/enemies
        if pid != const._SUN:
            combust = pid in _combust_set
            sun_conj = const._SUN in _conj_with(pid, {const._SUN})
            bad_asp = _any_aspect_from(NAT_MALEFICS, hidx) or _any_aspect_from(enemies, hidx)
            if (combust or sun_conj) and bad_asp:
                reason = "combust" if combust else "conjoined with Sun"
                out.append(("Kshobhita (agitated)",
                            [f"{name} is {reason}", "Also aspected by malefics/enemies"],
                            "Leads to penury, confused logic and repeated troubles; losses of wealth, foot ailments, and setbacks through official disfavour."))
        return out

    def _weak_note_for(pid: int) -> str:
        # the four-fold weakness check already used elsewhere
        v = _extract_shadbala_val(_get_shadbala_result() if ' _get_shadbala_result' in globals() else sb_res, pid)
        sb_weak = (pid in SHAD_THRESH) and (v is not None) and (v < SHAD_THRESH[pid])
        weak = (pid in avs.get("bala", set())) or (pid in avs.get("mrita", set())) or (pid in avs.get("sushupti", set())) or sb_weak
        return (
                f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
                f"{int((v/1020)*100)}%</p>"
            )

    def _render_avastha_block() -> str:
        planets = [const._SUN, const._MOON, const._MARS, const._MERCURY, const._JUPITER, const._VENUS, const._SATURN]
        parts = ["<div class='mt-4'><h3 class='h6 text-center'>Readings based on Deeptādi & Lajjitādi Avasthas</h3>"]
        any_match = False
        for pid in planets:
            dp = _deeptadi_for(pid)
            lj = _lajjitadi_for(pid)
            if not dp and not lj:
                continue
            any_match = True
            parts.append(f"<h4 class='h6 text-center mt-3'>{_planet_name(pid)}</h4>")
            # group evidence + effects per avastha
            for nm, ev, fx in (dp + lj):
                parts.append(f"<p class='text-left mb-1'><em>{nm}</em></p>")
                if ev:
                    for e in ev:
                        parts.append(f"<p class='text-left mb-1'>• {e}</p>")
                parts.append(f"<p class='text-left mb-2'><strong>Predicted effects:</strong> {fx}</p>")
            # mahadasha note (once per planet)
            md = _md_period_for(pid)
            #if md:
            #    s, e = md
            #    parts.append(f"<p class='text-left mt-1'><strong>The above effects would be more prominent in the mahadasha of {_planet_name(pid)}:</strong> {s:%Y-%m-%d} – {e:%Y-%m-%d}</p>")
            # weakness note (if any)
            #parts.append(_weak_note_for(pid))
        if not any_match:
            parts.append("<p class='text-left'>No Deeptādi/Lajjitādi avasthas matched the strict rules for visible planets.</p>")
        parts.append("</div>")
        return "".join(parts)

    avasthas_html = _render_avastha_block()
    
        # ───────────────────────────────────────────────────────────────────────
    # Mahadasha timeline table (chronological; predictions per planet)
    # ───────────────────────────────────────────────────────────────────────

    # 1) build the full Vimshottari sequence from (slightly before) birth
    KETU  = getattr(const, "_KETU", -2)
    RAHU  = getattr(const, "_RAHU", -1)
    _ORDER = [KETU, const._VENUS, const._SUN, const._MOON, const._MARS, RAHU, const._JUPITER, const._SATURN, const._MERCURY]
    _YEARS = {KETU:7, const._VENUS:20, const._SUN:6, const._MOON:10, const._MARS:7, RAHU:18,
              const._JUPITER:16, const._SATURN:19, const._MERCURY:17}
    _DAYS_PER_YEAR = 365.2425

    def _md_sequence_from_birth():
        moon_lon = _get_lon(const._MOON)
        seg = 360.0 / 27.0
        nak_idx = int(moon_lon // seg) % 27
        start_lord = _ORDER[nak_idx % 9]
        frac = (moon_lon % seg) / seg           # elapsed within birth-nakshatra
        elapsed_yrs = frac * _YEARS[start_lord]
        remain_yrs  = _YEARS[start_lord] - elapsed_yrs
        start_dt = dob - timedelta(days=elapsed_yrs * _DAYS_PER_YEAR)
        end_dt   = dob + timedelta(days=remain_yrs  * _DAYS_PER_YEAR)

        seq = [(start_lord, start_dt, end_dt)]
        idx = (_ORDER.index(start_lord) + 1) % 9
        cur_start, total_yrs = end_dt, remain_yrs
        # cover ~120 years
        while total_yrs < 121:
            lord = _ORDER[idx]
            dur  = _YEARS[lord]
            cur_end = cur_start + timedelta(days=dur * _DAYS_PER_YEAR)
            seq.append((lord, cur_start, cur_end))
            cur_start, total_yrs = cur_end, total_yrs + dur
            idx = (idx + 1) % 9
        # keep only MDs that overlap life-span window from birth onward
        return [(p, s, e) for (p, s, e) in seq if e > dob]

    # 2) tiny helpers used by the prediction engine
    _GOOD_HOUSES = {1,4,5,7,9,10,11,2}           # 1-based
    _BAD_HOUSES  = {6,8,12}                      # 1-based

    _ASPECT_DELTAS = {
        const._SUN:     {6},             # 7th
        const._MOON:    {6},
        const._MERCURY: {6},
        const._VENUS:   {6},
        const._MARS:    {3,6,7},         # 4th, 7th, 8th
        const._JUPITER: {4,6,8},         # 5th, 7th, 9th
        const._SATURN:  {2,6,9},         # 3rd, 7th, 10th
        RAHU:           {4,6,8},
        KETU:           {4,6,8},
    }

    def _house_of(pid: int) -> int:
        # 0-based house index (whole sign); reliable even if p2h lacks an entry
        try:
            sidx = int(natal_pp[pid + 1][1][0])
        except Exception:
            sidx = _sign_of_longitude(_get_lon(pid))
        return (sidx - asc_sign) % 12

    def _sign_of(pid: int) -> int:
        try:
            return int(natal_pp[pid + 1][1][0])
        except Exception:
            return _sign_of_longitude(_get_lon(pid))

    def _dignity(pid: int) -> int:
        return _dignity_level(pid, _sign_of(pid))  # +3 exalt … –2 debil

    def _conj(a: int, b: int) -> bool:
        return _house_of(a) == _house_of(b)

    def _aspects(from_pid: int, to_house_idx0: int) -> bool:
        h_from = _house_of(from_pid)
        delta  = (to_house_idx0 - h_from) % 12
        return delta in _ASPECT_DELTAS.get(from_pid, {6})

    def _touches(a: int, target_house_idx0: int) -> bool:
        return (_house_of(a) == target_house_idx0) or _aspects(a, target_house_idx0)

    def _drekkana_timing(pid: int) -> str:
        """Return 'early/middle/late' for the MD, reversed if retrograde."""
        deg_in_sign = _get_lon(pid) % 30.0
        band = "early" if deg_in_sign < 10 else "middle" if deg_in_sign < 20 else "late"
        if pid in _retro_set:
            band = {"early":"late", "middle":"middle", "late":"early"}[band]
        return band

    def _weak_note(pid: int) -> str:
        # Avasthas + Shadbala threshold
        sb_val = _extract_shadbala_val(sb_res, pid)
        sb_bad = (pid in SHAD_THRESH and sb_val is not None and sb_val < SHAD_THRESH[pid])
        weak   = (pid in avs["bala"]) or (pid in avs["mrita"]) or (pid in avs["sushupti"]) or sb_bad
        return (
                f"<p class='text-left mt-2'><strong>Prediction Strength:</strong> "
                f"{int((sb_val/1020)*100)}%</p>"
            )

    # for rule (1) “associated with the 9th or 10th lord”
    lord9 = _SIGN_LORD[(asc_sign + 8) % 12]
    lord10 = _SIGN_LORD[(asc_sign + 9) % 12]

    # 3) planet-specific prediction builder, respecting your clauses verbatim (re-worded)
    def _md_prediction(pid: int) -> str:
        name = PLANET_NAMES.get(pid, str(pid))
        h0   = _house_of(pid)               # 0-based
        h    = h0 + 1                       # 1-based
        tier = _dignity(pid)
        strong_flags = [
            tier >= 2,                                          # exalt/own
            tier == 1,                                          # friend
            h in _GOOD_HOUSES,
            any(_touches(b, h0) for b in {const._JUPITER, const._VENUS, const._MERCURY, const._MOON}),   # benefic touch
            _touches(lord9,  h0) or _touches(lord10, h0),       # association/aspect with 9L/10L
        ]
        weak_flags = [
            tier <= -1,                                         # enemy/debil
            pid in _combust_set,
            h in _BAD_HOUSES,
            any(_touches(m, h0) for m in {const._SUN, const._MARS, const._SATURN, RAHU, KETU}),          # malefic touch
        ]
        favourable = sum(bool(x) for x in strong_flags) >= sum(bool(x) for x in weak_flags)

        # short helpers used by some conditional clauses
        def _is_kendra_or_3rd():
            return h in {1,4,7,10,3}
        def _owns_house(pid_test: int, house_no: int) -> bool:
            return _SIGN_LORD[(asc_sign + (house_no-1)) % 12] == pid_test

        # “When favourable / when adverse” per planet
        text = []
        if pid == const._SUN:
            if favourable:
                text.append("Wealth builds up, comforts rise, and authority notices you; status climbs.")
                # extra conditions
                if _touches(_SIGN_LORD[(asc_sign+4)%12], _house_of(pid)):  # 5th lord touches Sun
                    text.append("Childbirth is likely because the Sun connects with the 5th lord.")
                if _touches(_SIGN_LORD[(asc_sign+1)%12], _house_of(pid)) or _touches(_SIGN_LORD[(asc_sign+3)%12], _house_of(pid)):
                    text.append("Vehicles/comforts through links to the 2nd/4th lords.")
            else:
                text.append("Losses, royal disfavour, displacement, damaged status, and strain with the father are likely.")
        elif pid == const._MOON:
            if favourable:
                base = "Renown grows; prosperity and auspicious events at home; support from authorities; plans complete; status improves."
                if h == 2:
                    base += " The Moon placed in the 2nd tends to be especially fruitful."
                text.append(base)
            else:
                text.append("Wealth ebbs with physical and mental strain, issues with servants, and worries tied to the mother or authority.")
        elif pid == const._MARS:
            if favourable:
                text.append("Rank improves; gains from land, vehicles and clothing; foreign gains; generally positive for siblings.")
                if _is_kendra_or_3rd():
                    text.append("With Mars in a kendra or the 3rd: strong wins and comforts early in the daśā, tapering later.")
            else:
                text.append("Face takes a hit; opponents dominate; accidents or illnesses are a risk.")
        elif pid == RAHU:
            if favourable:
                text.append("Comforts and prosperity increase; religion or philosophy attracts; ceremonies and honours in foreign settings are likely.")
            else:
                text.append("Displacement, mental unrest, and risks to spouse or child may occur, with losses and unclean environments.")
            text.append("Rahu tends to be more comfortable in the middle stretch of its daśā.")
        elif pid == const._JUPITER:
            if favourable:
                text.append("Status and comforts expand; vehicles, worship, and support from spouse and children show up; auspicious results multiply.")
            else:
                text.append("Early setbacks with travel/pilgrimage or loss of cattle, then the period improves as it progresses.")
        elif pid == const._SATURN:
            if favourable:
                text.append("Favours from authority, study and wealth, rise in status and physical comforts.")
                if (_touches(const._JUPITER, h0) or _touches(const._VENUS, h0)) or (h in {1,4,5,7,9,10,11}) or (_sign_of(pid) in {8,11}):  # Jupiter signs Dhanu/Meena = 8/11 (0-based)
                    text.append("Saturn is especially constructive here when joined/aspected by benefics, in kendra/trine/11th, or in Jupiter’s signs.")
            else:
                text.append("Displacement, fear, losses to parents, illness to spouse/child, inauspicious events, even confinement can occur.")
        elif pid == const._MERCURY:
            if favourable:
                text.append("Comforts and wealth rise; reputation and learning improve; business pays; health and diet stabilize.")
            else:
                text.append("Wrath of authority, anxiety, disputes with relatives, travel under constraint, urinary issues and theft/fire risks.")
            text.append("With Mercury the start is generally smoother, royal favour peaks mid-period, and results decline toward the end.")
        elif pid == KETU:
            if favourable:
                text.append("Desired objects arrive; leadership of a locality; foreign travel and varied comforts.")
            else:
                text.append("Confinement, loss of dear ones, displacement, illness and painful company.")
            # house-timing pattern for Ketu
            if h in {3,6,11}:
                text.append("When Ketu is in the 3rd/6th/11th: rise comes early, fears surface mid-period, and distant travel tends to close the daśā.")
        elif pid == const._VENUS:
            if favourable:
                text.append("Royal polish: vehicles, clothing, ornaments, home, prosperity, marriage, military preferment and gains from many sides.")
            else:
                text.append("Pushback from family, issues through women, professional reversals or separations.")
            # illness clause if Venus owns 2nd or 7th
            if _owns_house(const._VENUS, 2) or _owns_house(const._VENUS, 7):
                text.append("Because Venus owns the 2nd or 7th here, health issues during the Venus daśā are a classical caution.")
        else:
            # safety net for any non-standard ids
            text.append("Results follow the planet’s strength, placement and associations.")
        
        # “early/middle/late” timing from Drekkana
        #text.append(f"Expect the key results to surface in the <b>{_drekkana_timing(pid)}</b> part of this daśā.")
        # embed weakness note if applicable
        return " ".join(text)

    # 4) build the rows & render as a compact table
    md_rows = []
    for lord, start_dt, end_dt in _md_sequence_from_birth():
        md_rows.append({
            "Period": f"{start_dt:%Y-%m-%d} – {end_dt:%Y-%m-%d}",
            "Planet": PLANET_NAMES.get(lord, str(lord)),
            "Predictions": _md_prediction(lord),
        })

    md_df = pd.DataFrame(md_rows, columns=["Period", "Planet", "Predictions"])
    md_table_html = md_df.to_html(index=False, escape=False,
                                  classes="table table-sm table-striped")

    mahadasha_html = (
        "<div class='mt-4'>"
        "<h3 class='h6 text-center'>Mahadasha Timeline & Predictive Reading</h3>"
        f"{md_table_html}"
        "</div>"
    )
    # ⬇️ make sure you concatenate/append `mahadasha_html` to whatever HTML you already return/render
    # e.g., if you collect blocks in `extra_html`, do:  extra_html += mahadasha_html

    html_out = f"""
<div class=\"container\"> 
  <h2 class=\"h5 mb-3 text-center\">Planetary Chart Details</h2>
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
  {yogas_html}
  {avasthas_html}
  {mahadasha_html}
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
