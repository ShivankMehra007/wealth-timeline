# app.py
from flask import Flask, request, render_template_string, jsonify, Response
from markupsafe import Markup
import json, html

try:
    import pandas as pd  # optional; only used if your function returns a DataFrame
except Exception:  # pragma: no cover
    pd = None

from career_timeline_full import timeline_from_args

app = Flask(__name__)

# Minimal page shell used only when we need to wrap an HTML fragment
BASE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Vedic Output</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <style>
      body { padding: 1rem; }
      pre { white-space: pre-wrap; word-break: break-word; }
      table { font-size: 0.92rem; }
      .container { max-width: 1100px; }
      .help { font-size: .9rem; color: #666; }
    </style>
  </head>
  <body>
    <div class="container">
      <h1 class="h4 mb-3">Navagraha Output</h1>
      <div class="row">
        <div class="col-lg-4">
          <form method="post" action="/timeline" class="card card-body gap-2">
            <div class="help mb-2">
              Enter birth details. Latitude/Longitude preferred; timezone like <code>+05:30</code>.
            </div>
            <div class="row g-2">
              <div class="col-6">
                <label class="form-label">Date</label>
                <input type="date" name="date" class="form-control" required>
              </div>
              <div class="col-6">
                <label class="form-label">Time</label>
                <input type="time" name="time" class="form-control" step="60" required>
              </div>
            </div>
            <div class="row g-2">
              <div class="col-6">
                <label class="form-label">Latitude</label>
                <input type="text" name="lat" class="form-control" placeholder="e.g. 28.6139">
              </div>
              <div class="col-6">
                <label class="form-label">Longitude</label>
                <input type="text" name="lon" class="form-control" placeholder="e.g. 77.2090">
              </div>
            </div>
            <div class="row g-2">
              <div class="col-6">
                <label class="form-label">Timezone</label>
                <input type="text" name="tz" class="form-control" placeholder="+05:30">
              </div>
              <div class="col-6">
                <label class="form-label">Place (optional)</label>
                <input type="text" name="place" class="form-control" placeholder="City, Country">
              </div>
            </div>
            <!-- add hidden inputs if your backend expects more fields -->
            <button type="submit" class="btn btn-primary mt-2">Compute</button>
          </form>
        </div>
        <div class="col-lg-8">
          {{ body|safe }}
        </div>
      </div>
    </div>
  </body>
</html>
"""

def _render_any(value):
    """Render any return type from timeline_from_args into an HTML response."""
    # 1) If it’s an HTML string
    if isinstance(value, str):
        s = value.lstrip()
        low = s.lower()
        # If it's a complete HTML document, send as-is
        if low.startswith("<!doctype") or "<html" in low:
            return Response(s, mimetype="text/html")
        # Otherwise wrap the fragment into the BASE template
        return render_template_string(BASE, body=Markup(s))

    # 2) Pandas DataFrame (fallback, in case the backend returns one)
    if pd is not None and isinstance(value, pd.DataFrame):
        table = value.to_html(index=False, classes="table table-striped table-sm")
        return render_template_string(BASE, body=Markup(table))

    # 3) JSON-like → pretty-print for visibility
    if isinstance(value, (dict, list, tuple)):
        try:
            pretty = json.dumps(value, indent=2, default=str)
        except Exception:
            pretty = str(value)
        return render_template_string(BASE, body=Markup(f"<pre>{html.escape(pretty)}</pre>"))

    # 4) Fallback: plain text
    return render_template_string(BASE, body=Markup(f"<pre>{html.escape(str(value))}</pre>"))

@app.route("/", methods=["GET"])
def home():
    # Show the form and an empty result pane
    return render_template_string(BASE, body=Markup("<div class='help'>Results will appear here after you submit.</div>"))

@app.route("/timeline", methods=["POST"])
def timeline():
    # Accept either JSON or form data, pass all kwargs through
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    try:
        result = timeline_from_args(**data)
    except TypeError:
        # In case your function takes no kwargs
        result = timeline_from_args()

    # If client explicitly requests JSON, convert HTML to a JSON envelope
    wants_json = request.headers.get("Accept", "").lower().startswith("application/json") \
                 or request.args.get("format") == "json"
    if wants_json:
        if isinstance(result, str):
            return jsonify({"html": result})
        if pd is not None and isinstance(result, pd.DataFrame):
            return jsonify({"columns": result.columns.tolist(),
                            "rows": result.to_dict(orient="records")})
        return jsonify({"data": result})

    # Default: return as HTML
    return _render_any(result)

# Gunicorn entrypoint: app:app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)
