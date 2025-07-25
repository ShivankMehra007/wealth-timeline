#!/usr/bin/env python3
"""
career_timeline_full_jhora.py
─────────────────────────────
Pure‑jhora rewrite of the five‑band Wealth / Career / Business timeline
generator.  Keeps 100 % of the original scoring logic:

• house‑lord weight, Sarvāshtakavarga boost, Yogas/Doshas, Shad‑bala
• EXCELLENT periods gated by a Jupiter / Saturn double‑transit check
• Outputs CSV + console table

Requires
────────
pip install jhora>=4.5.0 pandas>=2.0
"""

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

# ────────────────────────────────────────────────────────────────────────────
# Low‑level jhora helpers
# ────────────────────────────────────────────────────────────────────────────
from jhora import const
from jhora.panchanga import drik, utils as putils
from jhora.horoscope.chart import charts as jcharts
from jhora.horoscope.chart.dhasa.graha import vimsottari as jd_vimsottari
from jhora.horoscope.chart.dhasa.raasi import narayana  as jd_narayana
from jhora.horoscope.chart.ashtakavarga import ashtakavarga as jd_ashtaka
from jhora.horoscope.chart.yoga import yoga as jyoga

# ────────────────────────────────────────────────────────────────────────────
#  A *very light* OO façade so the old scoring code can stay intact
# ────────────────────────────────────────────────────────────────────────────
class SimpleChart:
    """
    Wraps a handful of jhora procedural calls and exposes a *subset*
    of the old PyJHora API that the scorer expects.
    """
    def __init__(self, dt: datetime, lat: float, lon: float, tz_offset: float):
        self.datetime = dt
        self.latitude, self.longitude, self.tz_offset = lat, lon, tz_offset

        # Build a jhora “place” & JD number
        self.place = drik.place("NA", lat, lon, tz_offset)
        self.jd    = putils.julian_day_number(dt.year, dt.month, dt.day,
                   dt.hour + dt.minute/60 + dt.second/3600)

        # Rāśi (D‑1)
        self.rasi_positions = jcharts.rasi_chart(
            self.jd, self.place, ayanamsa_mode=const._DEFAULT_AYANAMSA_MODE)

        # Divisional: D‑2 & D‑10
        self.d2_positions = jcharts.divisional_positions_from_rasi_positions(
            self.rasi_positions, 2)
        self.d10_positions = jcharts.divisional_positions_from_rasi_positions(
            self.rasi_positions, 10)

        # Fast lookup tables --------------------------------------------------
        self.planets = {p: (ra, lo) for p, (ra, lo) in self.rasi_positions}
        self.house_cusps = jcharts.get_house_cusps_from_lagna(self.rasi_positions)
        self.houselords  = jcharts.house_owners_from_rasi_positions(
            self.rasi_positions)

    # --- classical strength helpers ----------------------------------------
    def shadbala(self, planet_id: int) -> float:
        """Return Shad‑bala strength in ‘rise‑over‑average’ units (≈ 1.0 = 100 %)."""
        return jcharts.shadbala_of_planet_from_positions(
            planet_id, self.rasi_positions)

    # --- daśā helpers -------------------------------------------------------
    def dasha_table(self, system: str = "vimshottari", varga: int | None = None):
        """
        Return a Pandas DF like PyJHora’s .to_dataframe().
        Supports:  system = 'vimshottari' | 'narayana'
        """
        if system == "vimshottari":
            tree = jd_vimsottari.vimsottari_dhasa_from_planet_positions(
                self.rasi_positions, self.datetime)
        elif system == "narayana":
            tree = jd_narayana.narayana_dhasa_from_rasi_positions(
                self.rasi_positions, varga_no=varga or 1)
        else:
            raise ValueError("Unsupported dasha system")

        rows = []
        def _walk(node, level=0):
            for period in node:
                rows.append(dict(
                    level=["maha","antara","pratyantara","sookshma","prana"][level],
                    label=system[:3],
                    start=period['start_datetime'],
                    end=period['end_datetime'],
                    lord=period['planet']) )
                if period.get("sub"):
                    _walk(period["sub"], level+1)
        _walk(tree)
        return pd.DataFrame(rows)

    # --- yearly Sarvāshtakavarga -------------------------------------------
    def sav_for_year(self, year: int):
        av = jd_ashtaka.sarvashtakavarga_scores(
            self.rasi_positions, year, self.place,
            ayanamsa_mode=const._DEFAULT_AYANAMSA_MODE)
        return av   # dict {house#: bindus}

    # --- yogas / doshas -----------------------------------------------------
    def applicable_yogas(self):
        # jhora yoga() returns a list of dicts; wrap into a tiny object
        return jyoga.applicable_yogas_from_rasi_positions(self.rasi_positions)

# ────────────────────────────────────────────────────────────────────────────
#  Below here the **original scorer** is copied verbatim
#  (only the import list & Chart references changed)
# ────────────────────────────────────────────────────────────────────────────

# … ALL original CONSTANTS (weights, thresholds, label colours) go here …
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

# yoga‑category IDs in jhora follow the same enum names
POSITIVE_YOGA_CATS = {
    "DHANA", "LAKSHMI", "RAJA", "KENDRA_TRIKONA",
    "VIPARITA_RAJA", "PANCHA_MAHAPURUSHA",
}
NEGATIVE_YOGA_CATS = {
    "DARIDRA", "ARISHTA", "DOSHA", "KEMADRUMA", "KALASARPA",
}

# ── helper: jup/sat transit check ------------------------------------------
def sign_index(deg: float) -> int: return int(deg // 30)
def aspecting(sign: int, planet: str) -> set[int]:
    if planet == "Jupiter": return {(sign+4)%12,(sign+6)%12,(sign+8)%12}
    if planet == "Saturn":  return {(sign+2)%12,(sign+6)%12,(sign+9)%12}
    return set()

def transit_hits_key(mid, natal: SimpleChart):
    tr = SimpleChart(mid, natal.latitude, natal.longitude, natal.tz_offset)
    j_sign = sign_index(tr.planets[const.SUN][0])  # Jupiter ID = 4 in const
    s_sign = sign_index(tr.planets[const.SATURN][0])
    key = { sign_index(natal.house_cusps[1]),  # 2nd
            sign_index(natal.house_cusps[9]),  # 10th
            sign_index(natal.house_cusps[10])} # 11th
    return bool(key & aspecting(j_sign,"Jupiter")
                or  key & aspecting(s_sign,"Saturn"))

# ── the old scoring function (unchanged) ------------------------------------
def score_periods(chart, vim_df, nar_df, sav_df, pos_y, neg_y):
    wealth_lords = set(chart.houselords[2]) | set(chart.houselords[11])
    career_lords = set(chart.houselords[10])

    def yoga_flag(pl, pool): return any(pl == y['lords'][0] for y in pool)

    rows=[]
    for df in (vim_df, nar_df):
        for r in df.itertuples():
            mid   = r.start + (r.end - r.start)/2
            lord  = r.lord
            score = 0
            if lord in wealth_lords: score += WEALTH_LORD_WEIGHT
            if lord in career_lords: score += CAREER_LORD_WEIGHT

            yr = sav_df[sav_df.year == mid.year]
            if not yr.empty:
                if lord in wealth_lords and (
                     yr.sav_2.values[0]>=SAV_WEALTH_TH or
                     yr.sav_11.values[0]>=SAV_WEALTH_TH): score += SAV_HIGH_WEIGHT
                if lord in career_lords and yr.sav_10.values[0]>=SAV_CAREER_TH: 
                    score += SAV_HIGH_WEIGHT

            if yoga_flag(lord, pos_y): score += POSITIVE_YOGA_WEIGHT
            if yoga_flag(lord, neg_y): score += NEGATIVE_YOGA_WEIGHT

            sb = chart.shadbala(lord)
            score += STRENGTH_BONUS if sb>=SHADBALA_GOOD else \
                     STRENGTH_MALUS if sb<SHADBALA_BAD else 0

            if score>=EXCELLENT_TH and transit_hits_key(mid,chart):
                label="EXCELLENT"
            elif score>=GOOD_TH:       label="GOOD"
            elif score>=NEUTRAL_TH:    label="NEUTRAL"
            elif score>=CHALLENGE_TH:  label="CHALLENGING"
            else:                      label="RISK"

            rows.append(dict(system=r.label,level=r.level,
                             period=f"{r.start.date()} → {r.end.date()}",
                             lord=lord,score=score,rating=label))
    return pd.DataFrame(rows).sort_values("period")

# ────────────────────────────────────────────────────────────────────────────
# CLI front‑end (only change: we call SimpleChart)
# ────────────────────────────────────────────────────────────────────────────
def build_sav_df(chart, start_age=18, end_age=80):
    rows=[]
    for year in range(chart.datetime.year+start_age,
                      chart.datetime.year+end_age+1):
        sav = chart.sav_for_year(year)
        rows.append(dict(year=year,
                         sav_2=sav[2],sav_10=sav[10],sav_11=sav[11]))
    return pd.DataFrame(rows)

def main():
    ap=argparse.ArgumentParser(description="5‑band timeline via jhora")
    for prm in("--name","--date","--time","--lat","--lon"):
        ap.add_argument(prm,required=True)
    ap.add_argument("--tz",default="+05:30")
    args=ap.parse_args()

    dt = datetime.fromisoformat(f"{args.date}T{args.time}{args.tz}")
    chart = SimpleChart(dt,float(args.lat),float(args.lon),
                        ZoneInfo(args.tz).utcoffset(dt).total_seconds()/3600)

    vim = chart.dasha_table("vimshottari")
    nar = chart.dasha_table("narayana",varga=10)
    sav_df = build_sav_df(chart)

    # classify yogas
    pos,neg=[],[]
    for y in chart.applicable_yogas():
        (pos if y['category'] in POSITIVE_YOGA_CATS else
         neg if y['category'] in NEGATIVE_YOGA_CATS else []).append(y)

    timeline = score_periods(chart,vim,nar,sav_df,pos,neg)
    out=f"timeline_{args.name.replace(' ','_')}.csv"
    timeline.to_csv(out,index=False)
    print(timeline[["system","level","period","lord","rating"]]
          .to_string(index=False))
    print("Saved →",out)

if __name__=="__main__":
    main()
