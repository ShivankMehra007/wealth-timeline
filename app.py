# app.py  – only the /timeline route is shown

from flask import Flask, request, jsonify
from career_timeline_full import timeline_from_args

app = Flask(__name__)

@app.post("/timeline")
def timeline():
    """
    Accepts JSON like:
      {
        "name": "Alice",
        "date": "1990-05-12",
        "time": "14:30",
        "lat": 28.61,
        "lon": 77.23,
        "tz": "+05:30"
      }
    and returns a list‑of‑dict rows ready for the front‑end.
    """
    data = request.get_json(force=True) or {}
    df = timeline_from_args(**data)        # ← DataFrame
    return jsonify(df.to_dict(orient="records"))  # ⇢ JSON‑serialisable