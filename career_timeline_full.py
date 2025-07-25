#!/usr/bin/env python3
"""
career_timeline_full_jhora.py
─────────────────────────────
Five‑band Wealth / Business / Career timeline generator
that relies **solely on the directory structure of `jhora`
you listed in the prompt**.

Key features (unchanged):
• Vimsottari & Narayana daśās
• Yogas, Doshas, Shad‑bala, Sarvāshtakavarga weighting
• Jupiter/Saturn transit check for “EXCELLENT”
• CSV + console table output

Install
───────
pip install jhora>=4.5.0 pandas>=2.0
"""

import argparse
from datetime import datetime, timedelta
import pandas as pd
from zoneinfo import ZoneInfo

# ────────────────────────────────────────────────────────────────────────────
#  Low‑level jhora imports (paths adjusted to the tree you provided)
# ────────────────────────────────────────────────────────────────────────────
from jhora import const                                  # jhora/const.py
from jhora.utils import utils as jutils                  # jhora/utils.py

# Panchanga helpers – sunrise → planet positions
from jhora.panchanga import drik as pdrik

# Core chart and maths helpers
from jhora.horoscope.chart import charts as jcharts      # …/chart/charts.py
from jhora.horoscope.chart import strength as jstrength  # Shad‑bala, etc.
from jhora.horoscope.chart import house as jhouse        # house lords, cusps

# Divisional, Argala, etc. (we need only D‑charts)
# (Already available via jcharts helper functions)

# Daśā engines
from jhora.horoscope.dhasa.graha import vimsottari as jd_vimsottari
from jhora.horoscope.dhasa.raasi import narayana  as jd_narayana

# Ashtakavarga
from jhora.horoscope.chart import ashtavarga as jd_ashtaka  # ashtavarga.py

# Yogas & doshas
from jhora.horoscope.chart.yoga import yoga as jyoga

# ────────────────────────────────────────────────────────────────────────────
#  Very thin OO wrapper → “SimpleChart”
#    Exposes the subset of attributes the scorer needs.
# ────────────────────────────────────────────────────────────────────────────
class SimpleChart:
    def __init__(self, dt: datetime, lat: float, lon: float, tz_str: str):
        self.datetime  = dt
        self.latitude  = lat
        self.longitude = lon
        self.tz_obj    = ZoneInfo(tz_str)
        self.tz_offset = self.tz_obj.utcoffset(dt).total_seconds() / 3600.0

        # Build a "place" object for panchanga helpers
        self.place = pdrik.place("NA", lat, lon, self.tz_offset)

        # Rāśi (D‑1) planetary positions
        self.rasi_positions = jcharts.rasi_chart(
            dt,
            self.place,
            ayanamsa_mode=const._DEFAULT_AYANAMSA_MODE
        )

        # Divisional chart longitudes we care about
        self.d2_positions  = jcharts.divisional_positions_from_rasi_positions(
            self.rasi_positions, 2)
        self.d10_positions = jcharts.divisional_positions_from_rasi_positions(
            self.rasi_positions, 10)

        # House cusps & house lords
        self.house_cusps  = jhouse.get_house_cusps_from_lagna(self.rasi_positions)
        self.houselords   = jhouse.house_owners_from_rasi_positions(self.rasi_positions)

        # Quick planet‑longitude dict:  {planetID: deg}
        self.planets = {pid: pos[0] for pid, pos in self.rasi_positions.items()}

    # ---- Shad‑bala wrapper --------------------------------------------------
    def shadbala(self, planet_id: int) -> float:
        """Return Shad‑bala in ‘times‑the‑average’ units (~1.0 == 100 %)."""
        return jstrength.total_shadbala_of_planet(
            planet_id, self.rasi_positions, self.place, const._DEFAULT_AYANAMSA_MODE
        )

    # ---- Daśā helpers -------------------------------------------------------
    def dasha_df(self, system: str = "vimshottari", varga: int | None = None):
        if system == "vimshottari":
            tree = jd_vimsottari.vimsottari_dhasa_from_planet_positions(
                self.rasi_positions, self.datetime
            )
        elif system == "narayana":
            tree = jd_narayana.narayana_dhasa_from_rasi_positions(
                self.rasi_positions, varga_no=(varga or 1)
            )
        else:
            raise ValueError("Unknown dasha system")

        rows = []
        labels = ["maha", "antara", "pratyantara", "sookshma", "prana"]
        def walk(node, level=0):
            for blk in node:
                rows.append(
                    dict(level=labels[level],
                         label=system[:3],
                         start=blk["start_datetime"],
                         end=blk["end_datetime"],
                         lord=blk["planet"])
                )
                if blk.get("sub"):
                    walk(blk["sub"], level + 1)
        walk(tree)
        return pd.DataFrame(rows)

    # ---- Yearly Sarvāshtakavarga -------------------------------------------
    def sav_year(self, year: int):
        return jd_ashtaka.sarvashtakavarga_scores(
            self.rasi_positions, year, self.place, const._DEFAULT_AYANAMSA_MODE
        )

    # ---- Applicable yogas / doshas -----------------------------------------
    def applicable_yogas(self):
        return jyoga.applicable_yogas_from_rasi_positions(self.rasi_positions)

# ────────────────────────────────────────────────────────────────────────────
#  Constants for weighting / thresholds  (identical to earlier versions)
# ────────────────────────────────────────────────────────────────────────────
WEALTH_LORD_WEIGHT   = 20
CAREER_LORD_WEIGHT   = 20
SAV_HIGH_WEIGHT      = 10
POSITIVE_YOGA_WEIGHT = 30
NEGATIVE_YOGA_WEIGHT = -40
STRENGTH_BONUS       = 10
STRENGTH_MALUS       = -10

SAV_WEALTH_TH  = 22
SAV_CAREER_TH  = 22
SHADBALA_GOOD  = 1.00
SHADBALA_BAD   = 0.80

EXCELLENT_TH = 60
GOOD_TH      = 40
NEUTRAL_TH   = 15
CHALLENGE_TH = 1

POSITIVE_YOGA_CATS = {
    "DHANA", "LAKSHMI", "RAJA", "KENDRA_TRIKONA",
    "VIPARITA_RAJA", "PANCHA_MAHAPURUSHA",
}
NEGATIVE_YOGA_CATS = {
    "DARIDRA", "ARISHTA", "DOSHA", "KEMADRUMA", "KALASARPA",
}

# ────────────────────────────────────────────────────────────────────────────
#  Helper: Jupiter / Saturn transit check
# ────────────────────────────────────────────────────────────────────────────
def sign_idx(deg): return int(deg // 30)

def aspecting_signs(sign, planet):
    if planet == "Jupiter":
        return {(sign+4)%12, (sign+6)%12, (sign+8)%12}
    if planet == "Saturn":
        return {(sign+2)%12, (sign+6)%12, (sign+9)%12}
    return set()

def transit_gate(mid_dt, natal: SimpleChart):
    """Returns True if Jup/Sat aspect natal 2 / 10 / 11 at mid‑date."""
    tr = SimpleChart(
        mid_dt, natal.latitude, natal.longitude,
        natal.tz_obj.key
    )
    j_sign = sign_idx(tr.planets[const.JUPITER])
    s_sign = sign_idx(tr.planets[const.SATURN])
    target = { sign_idx(natal.house_cusps[1]),   # 2nd house cusp
               sign_idx(natal.house_cusps[9]),   # 10th
               sign_idx(natal.house_cusps[10]) } # 11th
    return bool(target & aspecting_signs(j_sign, "Jupiter")
                or target & aspecting_signs(s_sign, "Saturn"))

# ────────────────────────────────────────────────────────────────────────────
#  Scoring engine (unchanged)
# ────────────────────────────────────────────────────────────────────────────
def score_periods(chart, vim_df, nar_df, sav_df, pos_y, neg_y):
    wealth_lords = set(chart.houselords[2]) | set(chart.houselords[11])
    career_lords = set(chart.houselords[10])

    def yoga_flag(pl, pool):
        return any(pl == y["lords"][0] for y in pool)

    rows = []
    for df in (vim_df, nar_df):
        for r in df.itertuples():
            mid   = r.start + (r.end - r.start) / 2
            lord  = r.lord
            score = 0

            if lord in wealth_lords: score += WEALTH_LORD_WEIGHT
            if lord in career_lords: score += CAREER_LORD_WEIGHT

            yr = sav_df[sav_df.year == mid.year]
            if not yr.empty:
                if lord in wealth_lords and (
                    yr.sav_2.values[0] >= SAV_WEALTH_TH or
                    yr.sav_11.values[0] >= SAV_WEALTH_TH):
                    score += SAV_HIGH_WEIGHT
                if lord in career_lords and (
                    yr.sav_10.values[0] >= SAV_CAREER_TH):
                    score += SAV_HIGH_WEIGHT

            if yoga_flag(lord, pos_y): score += POSITIVE_YOGA_WEIGHT
            if yoga_flag(lord, neg_y): score += NEGATIVE_YOGA_WEIGHT

            sb = chart.shadbala(lord)
            score += STRENGTH_BONUS if sb >= SHADBALA_GOOD else \
                     STRENGTH_MALUS if sb < SHADBALA_BAD else 0

            if score >= EXCELLENT_TH and transit_gate(mid, chart):
                label = "EXCELLENT"
            elif score >= GOOD_TH:
                label = "GOOD"
            elif score >= NEUTRAL_TH:
                label = "NEUTRAL"
            elif score >= CHALLENGE_TH:
                label = "CHALLENGING"
            else:
                label = "RISK"

            rows.append(dict(system=r.label, level=r.level,
                             period=f"{r.start.date()} → {r.end.date()}",
                             lord=lord, score=score, rating=label))
    return pd.DataFrame(rows).sort_values("period")

# ────────────────────────────────────────────────────────────────────────────
#  Helper builders
# ────────────────────────────────────────────────────────────────────────────
def build_sav_df(chart, start_age=18, end_age=80):
    rows=[]
    for yr in range(chart.datetime.year + start_age,
                    chart.datetime.year + end_age + 1):
        sav = chart.sav_year(yr)
        rows.append(dict(year=yr, sav_2=sav[2], sav_10=sav[10], sav_11=sav[11]))
    return pd.DataFrame(rows)

# ────────────────────────────────────────────────────────────────────────────
#  CLI
# ────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Pure‑jhora 5‑band timeline")
    for prm in ("--name", "--date", "--time", "--lat", "--lon"):
        ap.add_argument(prm, required=True)
    ap.add_argument("--tz", default="+05:30")
    args = ap.parse_args()

    dt = datetime.fromisoformat(f"{args.date}T{args.time}{args.tz}")
    chart = SimpleChart(dt, float(args.lat), float(args.lon), args.tz)

    vim_df = chart.dasha_df("vimshottari")
    nar_df = chart.dasha_df("narayana", varga=10)
    sav_df = build_sav_df(chart)

    pos_y, neg_y = [], []
    for y in chart.applicable_yogas():
        (pos_y if y["category"] in POSITIVE_YOGA_CATS else
         neg_y if y["category"] in NEGATIVE_YOGA_CATS else []).append(y)

    timeline = score_periods(chart, vim_df, nar_df, sav_df, pos_y, neg_y)
    out = f"timeline_{args.name.replace(' ', '_')}.csv"
    timeline.to_csv(out, index=False)

    print(timeline[["system", "level", "period", "lord", "rating"]]
          .to_string(index=False))
    print("Saved →", out)

if __name__ == "__main__":
    main()
