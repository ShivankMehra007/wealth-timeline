"""
career_timeline_full.py
A standalone module that exposes ONE public function:
    timeline_from_args(name, date, time, tz, lat, lon)
It returns the timeline as a list of dicts (JSON‑ready) so Flask can pass it
straight back to the browser.
"""
from datetime import datetime, timedelta
import pandas as pd
from pyjhora.chart import Chart
from pyjhora.dasha import VimsottariDasha, NarayanaDasha
from pyjhora.ashtakavarga import YearlySarvashtakavarga
from pyjhora.utils import Ayanamsa
from pyjhora.yoga import YogaCategory
import argparse

# ── Weights & static thresholds ──────────────────────────────────────────────
WEALTH_LORD_WEIGHT   = 20
CAREER_LORD_WEIGHT   = 20
SAV_HIGH_WEIGHT      = 10
POSITIVE_YOGA_WEIGHT = 30
NEGATIVE_YOGA_WEIGHT = -40
STRENGTH_BONUS       = 10
STRENGTH_MALUS       = -10

SAV_WEALTH_TH        = 22   # bindus for houses 2 & 11
SAV_CAREER_TH        = 22   # bindus for house 10
SHADBALA_GOOD        = 1.00
SHADBALA_BAD         = 0.80

# Label thresholds (static)
EXCELLENT_TH = 60   # + transit gate
GOOD_TH      = 40
NEUTRAL_TH   = 15
CHALLENGE_TH = 1    # 1‑14   (0 or less = RISK)

LABEL_COLORS = {      # optional: colour codes for UI export
    "EXCELLENT":  "#0a7d2c",
    "GOOD":       "#6cc04a",
    "NEUTRAL":    "#bfbfbf",
    "CHALLENGING":"#ffa500",
    "RISK":       "#d62728",
}

# Yoga buckets
POSITIVE_YOGA_CATS = {
    YogaCategory.DHANA, YogaCategory.LAKSHMI, YogaCategory.RAJA,
    YogaCategory.KENDRA_TRIKONA, YogaCategory.VIPARITA_RAJA,
    YogaCategory.PANCHA_MAHAPURUSHA,
}
NEGATIVE_YOGA_CATS = {
    YogaCategory.DARIDRA, YogaCategory.ARISHTA, YogaCategory.DOSHA,
    YogaCategory.KEMADRUMA, YogaCategory.KALASARPA,
}

# ── Helper builders ──────────────────────────────────────────────────────────
def build_chart(args) -> Chart:
    birth = datetime.fromisoformat(f"{args.date}T{args.time}{args.tz}")
    ch = Chart(birth, args.lat, args.lon, ayanamsa=Ayanamsa.PUSHYA_PAKSHA)
    ch.compute_divisional_chart(2)   # Hora
    ch.compute_divisional_chart(10)  # Daśāṁśa
    return ch

def sign_index(deg: float) -> int:
    return int(deg // 30)

def aspecting(sign: int, planet: str) -> set[int]:
    if planet == "Jupiter":
        return {(sign + 4) % 12, (sign + 6) % 12, (sign + 8) % 12}
    if planet == "Saturn":
        return {(sign + 2) % 12, (sign + 6) % 12, (sign + 9) % 12}
    return set()

def transit_hits_key(mid: datetime, natal: Chart) -> bool:
    tr = Chart(mid, natal.latitude, natal.longitude, ayanamsa=natal.ayanamsa)
    j_sign = sign_index(tr.planets["Jupiter"].longitude)
    s_sign = sign_index(tr.planets["Saturn"].longitude)
    cusp   = natal.house_cusps
    key = {sign_index(cusp[1]), sign_index(cusp[9]), sign_index(cusp[10])}  # 2/10/11
    return bool(key & aspecting(j_sign, "Jupiter") or
                key & aspecting(s_sign, "Saturn"))

# ── Main scorer ──────────────────────────────────────────────────────────────
def score_periods(chart: Chart, vim_df, nar_df, sav_df, pos_y, neg_y):
    wealth_lords = set(chart.houselords[2]) | set(chart.houselords[11])
    career_lords = set(chart.houselords[10])

    def yoga_hit(pl, pool): return any(pl in y.planets for y in pool)

    entries = []
    for df in (vim_df, nar_df):
        for r in df.itertuples():
            mid = r.start + (r.end - r.start) / 2
            lord = r.lord
            score = 0

            # house‑lord relevance
            if lord in wealth_lords: score += WEALTH_LORD_WEIGHT
            if lord in career_lords: score += CAREER_LORD_WEIGHT

            # SAV boost
            yr = sav_df[sav_df.year == mid.year]
            if not yr.empty:
                if lord in wealth_lords and (
                    yr.sav_2.values[0] >= SAV_WEALTH_TH or
                    yr.sav_11.values[0] >= SAV_WEALTH_TH):
                    score += SAV_HIGH_WEIGHT
                if lord in career_lords and (
                    yr.sav_10.values[0] >= SAV_CAREER_TH):
                    score += SAV_HIGH_WEIGHT

            # yogas / doshas
            if yoga_hit(lord, pos_y): score += POSITIVE_YOGA_WEIGHT
            if yoga_hit(lord, neg_y): score += NEGATIVE_YOGA_WEIGHT

            # shad‑bala
            sb = chart.shadbala(lord)
            score += STRENGTH_BONUS if sb >= SHADBALA_GOOD else \
                     STRENGTH_MALUS if sb < SHADBALA_BAD else 0

            # label assignment with transit gate
            if score >= EXCELLENT_TH and transit_hits_key(mid, chart):
                label = "EXCELLENT"
            elif score >= GOOD_TH:
                label = "GOOD"
            elif score >= NEUTRAL_TH:
                label = "NEUTRAL"
            elif score >= CHALLENGE_TH:
                label = "CHALLENGING"
            else:
                label = "RISK"

            entries.append({
                "system": r.label,
                "level":  r.level,
                "period": f"{r.start.date()} → {r.end.date()}",
                "lord":   lord.name,
                "score":  score,
                "rating": label,
            })
    return pd.DataFrame(entries).sort_values("period")

# ── Workflow helper functions ────────────────────────────────────────────────
def make_dashas(ch, start_age=18, end_age=80):
    born = ch.datetime
    win1 = born + timedelta(days=365.25*start_age)
    win2 = born + timedelta(days=365.25*end_age)
    vim = VimsottariDasha(ch).to_dataframe(label="vim")
    nar = NarayanaDasha(ch, varga=10).to_dataframe(label="nar")
    return (vim[(vim.start>=win1)&(vim.start<=win2)],
            nar[(nar.start>=win1)&(nar.start<=win2)])

def yearly_sav(ch, start_y, end_y):
    rows=[]
    for y in range(start_y, end_y+1):
        sv=YearlySarvashtakavarga(ch,y)
        rows.append({"year":y,"sav_2":sv.house_score(2),
                     "sav_10":sv.house_score(10),"sav_11":sv.house_score(11)})
    return pd.DataFrame(rows)

def classify_yogas(ch):
    pos,neg=set(),set()
    for y in ch.applicable_yogas():
        (pos if y.category in POSITIVE_YOGA_CATS else
         neg if y.category in NEGATIVE_YOGA_CATS else set()).add(y)
    return pos,neg

# ── CLI entry point ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Generate 5‑band wealth/career timeline (PyJHora)")
    for prm in ("--name","--date","--time","--lat","--lon"):
        ap.add_argument(prm,required=True)
    ap.add_argument("--tz",default="+00:00")
    args = ap.parse_args()

    chart            = build_chart(args)
    vim,nar          = make_dashas(chart)
    sav_df           = yearly_sav(chart, chart.datetime.year+18,
                                  chart.datetime.year+80)
    pos_y,neg_y      = classify_yogas(chart)
    timeline         = score_periods(chart,vim,nar,sav_df,pos_y,neg_y)

    out = f"timeline_{args.name.replace(' ','_')}.csv"
    timeline.to_csv(out,index=False)

    print("\nWealth / Business / Career Timeline:")
    print(timeline[["system","level","period","lord","rating"]]
          .reset_index(drop=True).to_string(index=False))
    print(f"\nFull CSV saved → {out}")

if __name__ == "__main__":
    main()

# -- (paste here the WEIGHTS, THRESHOLDS, helper functions and score_periods()
#    exactly as in the final script we wrote) --

# ---------------------------------------------------------------------------
def timeline_from_args(name, date, time, tz, lat, lon):
    """Public wrapper used by Flask; returns list[dict]."""
    # build chart
    birth = datetime.fromisoformat(f"{date}T{time}{tz}")
    chart = Chart(birth, float(lat), float(lon),
                  ayanamsa=Ayanamsa.PUSHYA_PAKSHA)
    chart.compute_divisional_chart(2)
    chart.compute_divisional_chart(10)

    # dashas & strength tables
    vim, nar = make_dashas(chart)
    sav_df   = yearly_sav(chart, chart.datetime.year+18, chart.datetime.year+80)
    pos_y, neg_y = classify_yogas(chart)

    df = score_periods(chart, vim, nar, sav_df, pos_y, neg_y)
    return df.to_dict(orient="records")
