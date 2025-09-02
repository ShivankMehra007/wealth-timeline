# app.py
from flask import Flask, request, render_template_string, jsonify, Response
from markupsafe import Markup
import inspect, json, html

try:
    import pandas as pd
except Exception:
    pd = None

from career_timeline_full import timeline_from_args

app = Flask(__name__)

BASE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Vedic Chart Reader</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <style>
      :root {
        --heading-grad: linear-gradient(135deg,#1f4aa8 0%, #7b2cbf 60%, #b5179e 100%);
      }
      body { padding: 1rem; }
      pre { white-space: pre-wrap; word-break: break-word; }
      table { font-size: 0.92rem; }
      .container { max-width: 1100px; }
      .help { font-size: .9rem; color: #666; }
      .brand {
        background: var(--heading-grad);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
        font-weight: 800;
        letter-spacing: .3px;
      }
      .subtle { color:#555; }
    </style>
  </head>
  <body>
    <div class="container">
      <header class="mb-4 text-center">
        <h1 class="display-6 brand mb-1">Vedic Chart Reader</h1>
        <p class="subtle mb-0">
          Compute your natal placements and get structured readings (lords, grahas, dashas, yogas and more).
          Enter birth details on the left; an overview appears on the right. Results render below.
        </p>
      </header>

      <!-- Inputs (left) + Intro (right) -->
      <div class="row g-4 align-items-stretch">
        <div class="col-lg-5">
          <form method="post" action="/timeline" class="card card-body gap-2 shadow-sm">
            <div class="help mb-2">
              Enter birth details. Latitude/Longitude preferred; timezone like <code>+05:30</code>.
            </div>
            <div class="row g-2">
              <div class="col-12">
                <label class="form-label">Name</label>
                <input type="text" name="name" class="form-control" placeholder="Anonymous">
              </div>
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
            <button type="submit" class="btn btn-primary mt-2">Compute</button>
          </form>
        </div>

        <div class="col-lg-7">
          <div class="card card-body h-100 bg-light border-0 shadow-sm">
            <h2 class="h5 mb-2">What you’ll see</h2>
            <p class="mb-2">
              This tool uses classical Jyotiṣa rules with PyJHora under the hood to assemble a readable report.
              It summarises placements, houses, strengths/avasthas, yogas/doshas, and dashā timelines—then
              rewrites the results in plain language. Treat the output as guidance, not certainty.
            </p>
            <ul class="mb-0 small">
              <li>Use decimal latitude/longitude when possible for accuracy.</li>
              <li>Timezone accepts <code>+HH:MM</code>, a number (e.g. <code>5.5</code>), or an IANA zone.</li>
              <li>Results appear below this section as tables and narrative blocks you can copy or print.</li>
            </ul>
          </div>
        </div>
      </div>

      <!-- Output area -->
      <div class="row mt-4">
        <div class="col-12" id="output">
          {{ body|safe }}
        </div>
      </div>
    </div>
  </body>
</html>
"""

def _render_any(value):
    if isinstance(value, str):
        s = value.lstrip()
        low = s.lower()
        if low.startswith("<!doctype") or "<html" in low:
            return Response(s, mimetype="text/html")
        return render_template_string(BASE, body=Markup(s))
    if pd is not None and isinstance(value, pd.DataFrame):
        table = value.to_html(index=False, classes="table table-striped table-sm")
        return render_template_string(BASE, body=Markup(table))
    if isinstance(value, (dict, list, tuple)):
        try:
            pretty = json.dumps(value, indent=2, default=str)
        except Exception:
            pretty = str(value)
        return render_template_string(BASE, body=Markup(f"<pre>{html.escape(pretty)}</pre>"))
    return render_template_string(BASE, body=Markup(f"<pre>{html.escape(str(value))}</pre>"))

def _sanitize_kwargs(fn, raw: dict) -> dict:
    """Keep only parameters accepted by fn; add defaults and coerce lat/lon."""
    sig = inspect.signature(fn)
    params = sig.parameters
    cleaned = {}

    # copy only accepted keys
    for k, v in (raw or {}).items():
        if k in params:
            cleaned[k] = v

    # provide default name if required but missing/empty
    if "name" in params and (not cleaned.get("name")):
        cleaned["name"] = "Anonymous"

    # coerce lat/lon if present
    for fld in ("lat", "lon"):
        if fld in cleaned:
            try:
                cleaned[fld] = float(str(cleaned[fld]).strip())
            except Exception:
                pass  # let backend handle invalid numeric

    return cleaned

@app.route("/", methods=["GET"])
def home():
    return render_template_string(BASE, body=Markup("<div class='help'>Results will appear here after you submit.</div>"))

@app.route("/timeline", methods=["POST"])
def timeline():
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    kwargs = _sanitize_kwargs(timeline_from_args, data)

    # Call once with sanitized kwargs; if still missing required args, show a friendly message.
    try:
        result = timeline_from_args(**kwargs)
    except TypeError as e:
        # Build an HTML error explaining which args are expected
        expected = ", ".join(inspect.signature(timeline_from_args).parameters.keys())
        msg = f"<div class='alert alert-danger'>Bad input: {html.escape(str(e))}<br>Expected keys: <code>{html.escape(expected)}</code></div>"
        return render_template_string(BASE, body=Markup(msg))

    # JSON mode if requested
    wants_json = request.headers.get("Accept", "").lower().startswith("application/json") \
                 or request.args.get("format") == "json"
    if wants_json:
        if isinstance(result, str):
            return jsonify({"html": result})
        if pd is not None and isinstance(result, pd.DataFrame):
            return jsonify({"columns": result.columns.tolist(),
                            "rows": result.to_dict(orient="records")})
        return jsonify({"data": result})

    return _render_any(result)

# Gunicorn entrypoint: app:app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)
