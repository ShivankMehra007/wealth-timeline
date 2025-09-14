# app.py
from flask import Flask, request, render_template_string, jsonify, Response
from markupsafe import Markup
import inspect, json, html, math

try:
    import pandas as pd
except Exception:
    pd = None

from career_timeline_full import timeline_from_args
import os

# Optional: external deps for geocoding + timezone
import typing as _t
import requests
from dataclasses import dataclass
from typing import Optional, List
try:
    from timezonefinder import TimezoneFinder  # pip install timezonefinder
except Exception:
    TimezoneFinder = None

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None

app = Flask(__name__)

BASE = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Free Astrology Chart & Predictions</title>
  <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
  <style>
    body { background: #fafafa; }
    header .brand { font-weight: 700; letter-spacing: .2px; }
    .help { font-size: 0.92rem; color: #555; }
    .subtle { color: #6c757d; }
    /* Typeahead dropdown */
    .typeahead-wrap { position: relative; }
    .typeahead-list {
      position: absolute; z-index: 1000; left: 0; right: 0;
      background: #fff; border: 1px solid #dee2e6; border-top: 0;
      max-height: 280px; overflow-y: auto; box-shadow: 0 .25rem .75rem rgba(0,0,0,.05);
    }
    .typeahead-item {
      padding: .5rem .75rem; cursor: pointer; border-top: 1px solid #f1f3f5;
      display: flex; align-items: start; gap: .5rem;
    }
    .typeahead-item:hover, .typeahead-item.active { background: #f8f9fa; }
    .typeahead-badge { font-size: .75rem; border: 1px solid #dee2e6; border-radius: .25rem; padding: .1rem .35rem; color: #495057; background: #f8f9fa; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace; }
    .sticky-top-custom { position: sticky; top: 1rem; }
    pre { white-space: pre-wrap; word-wrap: break-word; }
  </style>
</head>
<body>
  <div class=\"container my-4\">
    <header class=\"mb-3\">
      <h1 class=\"display-6 brand mb-1\">Free Astrology Chart & Predictions</h1>
      <p class=\"subtle mb-0\">
        Compute your natal placements and get structured readings (lords, grahas, dashas, yogas and more).
        Enter birth details on the left; an overview appears on the right. Results render below.
      </p>
    </header>

    <!-- Inputs (left) + Intro (right) -->
    <div class=\"row g-4 align-items-stretch\">
      <div class=\"col-lg-5\">
        <form method=\"post\" action=\"/timeline\" class=\"card card-body gap-2 shadow-sm\">
          <div class=\"help mb-2\">
            Enter birth details. Latitude/Longitude preferred; timezone like <code>+05:30</code> or IANA (e.g., <code>Asia/Kolkata</code>).
          </div>
          <div class=\"row g-2\">
            <div class=\"col-12\">
              <label class=\"form-label\">Name</label>
              <input type=\"text\" name=\"name\" class=\"form-control\" placeholder=\"Anonymous\">
            </div>
          </div>
          <div class=\"row g-2\">
            <div class=\"col-6\">
              <label class=\"form-label\">Date</label>
              <input type=\"date\" name=\"date\" class=\"form-control\" required>
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Time</label>
              <input type=\"time\" name=\"time\" class=\"form-control\" step=\"60\" required>
            </div>
          </div>
          <div class=\"row g-2\">
            <div class=\"col-6\">
              <label class=\"form-label\">Latitude</label>
              <input type=\"text\" name=\"lat\" id=\"lat\" class=\"form-control\" placeholder=\"e.g. 28.6139\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Longitude</label>
              <input type=\"text\" name=\"lon\" id=\"lon\" class=\"form-control\" placeholder=\"e.g. 77.2090\">
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Timezone</label>
              <input type=\"text\" name=\"tz\" id=\"tz\" class=\"form-control\" placeholder=\"+05:30 or Asia/Kolkata\">
            </div>
            <div class=\"col-6 typeahead-wrap\">
              <label class=\"form-label\">Place (optional)</label>
              <input type=\"text\" name=\"place\" id=\"place\" class=\"form-control\" placeholder=\"City, Country\" autocomplete=\"off\">
              <div id=\"place-list\" class=\"typeahead-list d-none\"></div>
            </div>
          </div>
          <button type=\"submit\" class=\"btn btn-primary mt-2\">Compute</button>
          <div class=\"help mt-2\">
            Tip: Start typing a birth city; pick from the dropdown to auto-fill latitude, longitude, and timezone.
          </div>
        </form>
      </div>

      <div class=\"col-lg-7\">
        <div class=\"card card-body h-100 bg-light border-0 shadow-sm sticky-top-custom\">
          <h2 class=\"h5 mb-2\">What you’ll see</h2>
          <p class=\"mb-2\">
            This tool uses classical Jyotiṣa rules with PyJHora under the hood to assemble a readable report.
            It summarises placements, houses, strengths/avasthas, yogas/doshas, and dashā timelines—then
            rewrites the results in plain language. Treat the output as guidance, not certainty.
          </p>
          <ul class=\"mb-0 small\">
            <li>Use decimal latitude/longitude when possible for accuracy.</li>
            <li>Timezone accepts <code>+HH:MM</code>, a number (e.g. <code>5.5</code>), or an IANA zone.</li>
            <li>Results appear below this section as tables and narrative blocks you can copy or print.</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- Output area -->
    <div class=\"row mt-4\">
      <div class=\"col-12\" id=\"output\">
        {{ body|safe }}
      </div>
    </div>
  </div>

  <script>
  (function() {
    const placeInput = document.getElementById('place');
    const list = document.getElementById('place-list');
    const latEl = document.getElementById('lat');
    const lonEl = document.getElementById('lon');
    const tzEl = document.getElementById('tz');

    let timer = null;
    let items = [];
    let activeIndex = -1;

    function clearList() {
      list.innerHTML = '';
      list.classList.add('d-none');
      items = [];
      activeIndex = -1;
    }

    function renderList(results) {
      items = results || [];
      if (!items.length) return clearList();
      list.innerHTML = items.map((r, i) => `
        <div class=\"typeahead-item${i===0?' active':''}\" data-index=\"${i}\">\n          <div class=\"flex-grow-1\">\n            <div><strong>${escapeHtml(r.name)}</strong></div>\n            <div class=\"small text-muted\">${escapeHtml(r.display_name)}</div>\n          </div>\n          <div class=\"text-nowrap\">\n            <span class=\"typeahead-badge mono\">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</span>\n            ${r.tz ? `<span class=\"typeahead-badge mono ms-1\">${escapeHtml(r.tz)}</span>` : ''}\n          </div>\n        </div>`).join('');
      list.classList.remove('d-none');
      activeIndex = 0;
    }

    function setActive(idx) {
      const children = [...list.querySelectorAll('.typeahead-item')];
      children.forEach(el => el.classList.remove('active'));
      if (idx >= 0 && idx < children.length) {
        children[idx].classList.add('active');
        activeIndex = idx;
      }
    }

    function choose(idx) {
      const sel = items[idx];
      if (!sel) return;
      placeInput.value = sel.display_name || sel.name;
      if (latEl) latEl.value = sel.lat;
      if (lonEl) lonEl.value = sel.lon;
      if (tzEl && sel.tz) tzEl.value = sel.tz;
      clearList();
    }

    function escapeHtml(s) {
      return (s || '').replace(/[&<>\"']/g, function (m) {
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;','\'':'&#39;'})[m];
      });
    }

    // Click handlers
    list.addEventListener('click', (e) => {
      const row = e.target.closest('.typeahead-item');
      if (!row) return;
      const idx = parseInt(row.getAttribute('data-index'), 10);
      choose(idx);
    });

    document.addEventListener('click', (e) => {
      if (!list.contains(e.target) && e.target !== placeInput) clearList();
    });

    // Keyboard navigation
    placeInput.addEventListener('keydown', (e) => {
      if (list.classList.contains('d-none')) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(activeIndex+1, items.length-1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(activeIndex-1, 0)); }
      else if (e.key === 'Enter') { e.preventDefault(); choose(activeIndex); }
      else if (e.key === 'Escape') { clearList(); }
    });

    // Debounced fetch
    placeInput.addEventListener('input', () => {
      const q = (placeInput.value || '').trim();
      if (timer) clearTimeout(timer);
      if (!q) { clearList(); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch('/places?q=' + encodeURIComponent(q));
          if (!r.ok) throw new Error('fetch failed');
          const data = await r.json();
          renderList(data.results || []);
        } catch (err) {
          console.error('Typeahead fetch failed', err);
          clearList();
        }
      }, 300);
    });
  })();
  </script>
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
            return render_template_string(BASE, body=Markup(f"<pre>{html.escape(pretty)}</pre>"))
        except Exception:
            pass
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
                # let backend handle invalid numeric
                pass

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


# ---------- Places API (typeahead) ----------
from typing import Optional, List

@dataclass
class Place:
    name: str
    display_name: str
    lat: float
    lon: float
    tz: Optional[str] = None

# Minimal offline fallback for common Indian cities (works if network is blocked)
_FALLBACK_PLACES = [
    Place("New Delhi", "New Delhi, Delhi, India", 28.6139, 77.2090, "Asia/Kolkata"),
    Place("Delhi", "Delhi, India", 28.6517, 77.2219, "Asia/Kolkata"),
    Place("Mumbai", "Mumbai, Maharashtra, India", 19.0760, 72.8777, "Asia/Kolkata"),
    Place("Kolkata", "Kolkata, West Bengal, India", 22.5726, 88.3639, "Asia/Kolkata"),
    Place("Chennai", "Chennai, Tamil Nadu, India", 13.0827, 80.2707, "Asia/Kolkata"),
    Place("Bengaluru", "Bengaluru, Karnataka, India", 12.9716, 77.5946, "Asia/Kolkata"),
    Place("Hyderabad", "Hyderabad, Telangana, India", 17.3850, 78.4867, "Asia/Kolkata"),
    Place("Pune", "Pune, Maharashtra, India", 18.5204, 73.8567, "Asia/Kolkata"),
    Place("Jaipur", "Jaipur, Rajasthan, India", 26.9124, 75.7873, "Asia/Kolkata"),
    Place("Ahmedabad", "Ahmedabad, Gujarat, India", 23.0225, 72.5714, "Asia/Kolkata"),
]

def _search_fallback(q: str) -> List[Place]:
    ql = q.lower()
    return [p for p in _FALLBACK_PLACES if ql in p.name.lower() or ql in p.display_name.lower()]

def _tz_from_latlon(lat: float, lon: float) -> Optional[str]:
    if TimezoneFinder is None:
        return None
    try:
        tf = TimezoneFinder(in_memory=True)
        return tf.timezone_at(lat=lat, lng=lon)
    except Exception:
        return None

def _search_places(q: str, limit: int = 8) -> List[Place]:
    """Query OpenStreetMap Nominatim for place suggestions."""
    url = "https://nominatim.openstreetmap.org/search"
    email = os.getenv("NOMINATIM_EMAIL", "support@example.com")
    params = {
        "q": q,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": str(limit),
        "accept-language": "en",
    }
    if email and "@" in email:
        params["email"] = email
    headers = {"User-Agent": f"vedic-astro-app/1.0 ({email})"}

    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()

    out: List[Place] = []
    for row in r.json():
        try:
            lat = float(row.get("lat"))
            lon = float(row.get("lon"))
        except Exception:
            continue
        name = row.get("name") or row.get("display_name") or "Unknown"
        disp = row.get("display_name") or name
        tz = _tz_from_latlon(lat, lon)
        out.append(Place(name=name, display_name=disp, lat=lat, lon=lon, tz=tz))
    return out

@app.route("/places")
def places():
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    try:
        results = _search_places(q, limit=8)
        payload = [{
            "name": p.name,
            "display_name": p.display_name,
            "lat": p.lat,
            "lon": p.lon,
            "tz": p.tz,
        } for p in results]
        return jsonify({"results": payload})
    except Exception as e:
        # Fallback when Nominatim is unreachable/rate-limited
        fallback = _search_fallback(q)
        payload = [{
            "name": p.name,
            "display_name": p.display_name,
            "lat": p.lat,
            "lon": p.lon,
            "tz": p.tz,
        } for p in fallback]
        status = 200 if payload else 502
        return jsonify({"error": str(e), "results": payload}), status


# Gunicorn entrypoint: app:app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)
