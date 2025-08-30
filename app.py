# app.py
from flask import Flask, request, render_template_string, jsonify
from markupsafe import Markup
import json
import html
import pandas as pd  # make sure pandas is installed
from career_timeline_full import timeline_from_args  # your existing entry point

app = Flask(__name__)

BASE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Output</title>
    <link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
    <style>
      body { padding: 1rem; }
      pre { white-space: pre-wrap; }
      table { font-size: 0.9rem; }
      .container { max-width: 1100px; }
    </style>
  </head>
  <body>
    <div class="container">
      <h1 class="h4 mb-3">Result</h1>
      {{ body|safe }}
    </div>
  </body>
</html>
"""

def render_any(value):
    """Render any Python object into an HTML fragment."""
    # 1) Pandas DataFrame → HTML table
    if isinstance(value, pd.DataFrame):
        return Markup(value.to_html(index=False, classes="table table-striped table-sm"))

    # 2) Already-HTML string → trust caller to have produced HTML
    if isinstance(value, str) and value.strip().lower().startswith("<"):
        return Markup(value)

    # 3) JSON-like → pretty JSON inside <pre>
    if isinstance(value, (dict, list, tuple)):
        try:
            return Markup(f"<pre>{html.escape(json.dumps(value, indent=2, default=str))}</pre>")
        except Exception:
            pass

    # 4) Fallback: plain string
    return Markup(f"<pre>{html.escape(str(value))}</pre>")

@app.route("/", methods=["GET"])
def home():
    return render_template_string(BASE, body=Markup("<p>POST to <code>/timeline</code>.</p>"))

@app.route("/timeline", methods=["POST"])
def timeline():
    # Accept either JSON or form data
    data = request.get_json(silent=True)
    if not data:
        data = request.form.to_dict() if request.form else {}

    try:
        result = timeline_from_args(**data)
    except TypeError:
        # If your function expects no kwargs:
        result = timeline_from_args()

    # If the client explicitly wants JSON, return JSON form.
    # Otherwise render HTML.
    wants_json = request.headers.get("Accept", "").lower().startswith("application/json") \
                 or request.args.get("format") == "json"
    if wants_json:
        if isinstance(result, pd.DataFrame):
            return jsonify({
                "columns": result.columns.tolist(),
                "rows": result.to_dict(orient="records")
            })
        return jsonify(result)

    body = render_any(result)
    return render_template_string(BASE, body=body)

if __name__ == "__main__":
    # For local testing only; Render/Gunicorn will import app:app
    app.run(host="0.0.0.0", port=10000, debug=False)
