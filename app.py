from flask import Flask, request, jsonify, render_template
from career_timeline_full import timeline_from_args

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

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
	
if __name__ == "__main__":
    app.run(debug=True)
