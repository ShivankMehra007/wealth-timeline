# app.py
from flask import Flask, request, render_template_string, jsonify, Response, send_from_directory
from markupsafe import Markup
import inspect, json, html, math
from pathlib import Path
import csv, unicodedata, re, gzip

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

# --- Affiliate Services Catalog ---
SERVICES = [
    {
        "id": "ask-3q",
        "title": "Consult Our Astrologer: Ask up to 3 Questions",
        "url": "https://www.astroved.com/ask-astrologer-3-questions-consult-our-astrologer-ask-up-to-3-questions-P122.aspx?affId=chartreader",
        "category": "general",
        "benefit": "Get precise answers for 3 burning questions",
        "badge": "Popular"
    },
    {
        "id": "match",
        "title": "Horoscope Matching Report",
        "url": "https://www.astroved.com/customized-reports-horoscope-matching-report-P117.aspx?affId=chartreader",
        "category": "relationships",
        "benefit": "Traditional compatibility with clear dosha guidance",
        "badge": "Shaadi"
    },
    {
        "id": "money-1y",
        "title": "One-Year Money & Prosperity Report",
        "url": "https://www.astroved.com/customized-reports-one-year-detailed-money-and-prosperity-report-P118.aspx?affId=chartreader",
        "category": "money",
        "benefit": "12-month cash-flow outlook + key dates"
    },
    {
        "id": "career-1y",
        "title": "One-Year Career Report",
        "url": "https://www.astroved.com/customized-reports-one-year-detailed-career-report-P119.aspx?affId=chartreader",
        "category": "career",
        "benefit": "Opportunities, promotions, and switch timing",
        "badge": "Career"
    },
    {
        "id": "health-1y",
        "title": "One-Year Health & Well-Being Report",
        "url": "https://www.astroved.com/customized-reports-one-year-detailed-health-and-well-being-report-P120.aspx?affId=chartreader",
        "category": "health",
        "benefit": "Risk windows + lifestyle recommendations"
    },
    {
        "id": "remedy",
        "title": "Astrologer-Prescribed Remedy",
        "url": "https://www.astroved.com/customized-reports-astrologer-prescribed-remedy-for-your-problem-P125.aspx?affId=chartreader",
        "category": "remedies",
        "benefit": "Personalized puja/mantra/gem guidance",
        "badge": "Remedy"
    },
    {
        "id": "love-360",
        "title": "Detailed Love & Relationships Report",
        "url": "https://www.astroved.com/customized-reports-360-degree-love-profile-P132.aspx?affId=chartreader",
        "category": "relationships",
        "benefit": "Attraction patterns & timing for love"
    },
    {
        "id": "business",
        "title": "Business Prospect Report",
        "url": "https://www.astroved.com/customized-reports-business-prospect-report-P397.aspx?affId=chartreader",
        "category": "business",
        "benefit": "Expansion timing & risk assessment"
    },
    {
        "id": "karmic",
        "title": "Karmic Astrology (Past-Life Influence) Report",
        "url": "https://www.astroved.com/customized-reports-karmic-astrology-report-past-life-influence-report--P399.aspx?affId=chartreader",
        "category": "karmic",
        "benefit": "Past-life themes affecting present"
    },
    {
        "id": "nadi-agastya",
        "title": "Essential Nadi Package (Agastya)",
        "url": "https://www.astroved.com/nadi-astrology-agastya-nadi-essential-package-P62903.aspx?affId=chartreader",
        "category": "nadi",
        "benefit": "Classical leaf reading experience"
    },
    {
        "id": "nadi-thuliya",
        "title": "Essential Nadi Package (Thuliya)",
        "url": "https://www.astroved.com/nadi-astrology-essential-thuliya-nadi-package-P48065.aspx?affId=chartreader",
        "category": "nadi",
        "benefit": "Focused Nadi insights & remedies"
    },
    {
        "id": "fame",
        "title": "Entertainment Fame Report",
        "url": "https://www.astroved.com/customized-reports-entertainment-fame-report-P580.aspx?affId=chartreader",
        "category": "fame",
        "benefit": "Brand, recognition & visibility path"
    },
    {
        "id": "sports",
        "title": "Sports Astrology Report",
        "url": "https://www.astroved.com/customized-reports-sports-astrology-report-P579.aspx?affId=chartreader",
        "category": "sports",
        "benefit": "Peak performance periods & training focus"
    },
]

@app.route("/ads.txt")
def ads_txt():
    fp = Path(app.root_path) / "ads.txt"
    if not fp.exists():
        # fallback: if you keep it in /static, you can 301 redirect
        # return redirect(url_for('static', filename='ads.txt'), code=301)
        return Response("ads.txt not found", status=404, mimetype="text/plain")
    # Serve with sensible caching
    resp = send_from_directory(app.root_path, "ads.txt", mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=86400"  # 24h
    return resp

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

def _append_utms(url: str, source="chartapp", medium="referral", campaign="services_panel"):
    """Preserve existing affId, add UTM tags safely."""
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    # ensure affId remains if already present; otherwise you can set default
    q.setdefault("affId", "chartreader")
    q.update({
        "utm_source": source,
        "utm_medium": medium,
        "utm_campaign": campaign
    })
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))


def _render_services_panel(top_categories=("career", "money", "relationships")):
    """Return HTML for a compact, high-conversion services section."""
    # choose top picks (prioritize popular categories)
    top = [s for s in SERVICES if s["category"] in top_categories][:3]
    rest = [s for s in SERVICES if s not in top]

    def card_html(s):
        url = _append_utms(s["url"])
        badge = f"<span class='badge bg-success ms-2'>{s.get('badge','')}</span>" if s.get("badge") else ""
        return f"""
        <div class="col-md-4 col-sm-6 mb-3">
          <a href="{html.escape(url)}" class="text-decoration-none" target="_blank" rel="nofollow sponsored noopener"
             onclick="try{{navigator.sendBeacon && navigator.sendBeacon('/track?id={s['id']}')}}catch(e){{}};">
            <div class="card h-100 shadow-sm">
              <div class="card-body">
                <h6 class="card-title mb-1">{html.escape(s['title'])}{badge}</h6>
                <p class="card-text small text-muted">{html.escape(s['benefit'])}</p>
                <div class="d-flex align-items-center">
                  <span class="btn btn-sm btn-primary">Learn more</span>
                </div>
              </div>
            </div>
          </a>
        </div>"""

    top_html = "".join(card_html(s) for s in top)
    rest_html = "".join(card_html(s) for s in rest)

    return f"""
    <section class="my-4">
      <div class="d-flex align-items-baseline mb-2">
        <h5 class="me-2 mb-0">Recommended Services</h5>
        <span class="text-muted small">Hand-picked based on common needs</span>
      </div>
      <div class="row">{top_html}</div>

      <details class="mt-2">
        <summary class="small text-primary" style="cursor:pointer;">See all services</summary>
        <div class="row mt-2">{rest_html}</div>
      </details>
    </section>
    """

BASE = """<!doctype html>
<html lang=\"en\">
<head>
  <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-2200267347781082"
     crossorigin="anonymous"></script>
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
            Enter birth details. You may select the birth place from the suggestions drop-down when you start typing. Latitude/Longitude and timezone would be automatically selected for you.
          </div>
          <div class=\"row g-2\">
            <div class=\"col-12\">
              <label class=\"form-label\">Name (Optional)</label>
              <input type=\"text\" name=\"name\" class=\"form-control\" placeholder=\"Anonymous\">
            </div>
          </div>
          <div class=\"row g-2\">
            <div class=\"col-6\">
              <label class=\"form-label\">Date (Month/Date/Year)</label>
              <input type=\"date\" name=\"date\" class=\"form-control\" required>
            </div>
            <div class=\"col-6\">
              <label class=\"form-label\">Time (Hours:Minutes AM/PM)</label>
              <input type=\"time\" name=\"time\" class=\"form-control\" step=\"60\" required>
            </div>
          </div>
          <div class=\"col-6 typeahead-wrap\">
              <label class=\"form-label\">Place (select from drop down)</label>
              <input type=\"text\" name=\"place\" id=\"place\" class=\"form-control\" placeholder=\"City, Country\" autocomplete=\"off\">
              <div id=\"place-list\" class=\"typeahead-list d-none\"></div>
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
            This tool uses classical Jyotiṣa rules to assemble a readable report. It summarises placements, houses, strengths/avasthas, yogas/doshas, and dashā timelines—then rewrites the results in plain language. Treat the output as guidance, not certainty.
          </p>
          <p class=\"mb-2\">
            Some predictions are clearly connected to specific stages of life. Others, which are not tied to a life stage, are more likely to show up strongly during the mahadashas and antardashas of the planets involved. If two predictions seem to contradict each other, they may cancel each other out, meaning neither happens, or they may occur at different times. Remember: every planet gives its results most powerfully in its own mahadasha, and next strongest during its antardashas.
          </p>
          <ul class=\"mb-0 small\">
            <li>You may select the birth place from the suggestions drop-down when you start typing. Latitude/Longitude and timezone would be automatically selected for you.</li>
            <li>Use decimal latitude/longitude when possible for accuracy.</li>
            <li>Timezone accepts <code>+HH:MM</code>, a number (e.g. <code>5.5</code>), or an IANA zone.</li>
            <li>Results appear below this section as tables and narrative blocks you can copy or print.</li>
          </ul>
        </div>
      </div>
    </div>

    <!-- Recommended Services (after results) -->
    <div class="row mt-4">
      <div class="col-12" id="services-below">
        {{ services_below|safe }}
      </div>
    </div>

    <!-- Footer with legal links -->
    <footer class="mt-4 pt-3 border-top small text-center text-muted">
      <p class="mb-1">
        &copy; 2025 Graha Guidance. All rights reserved.
      </p>
      <p class="mb-0">
        <a href="/privacy-policy" class="text-decoration-none me-2">Privacy Policy</a>
        <a href="/terms" class="text-decoration-none me-2">Terms &amp; Conditions</a>
        <a href="/disclaimer" class="text-decoration-none me-2">Disclaimer</a>
        <a href="/contact" class="text-decoration-none">Contact / Grievance</a>
      </p>
    </footer>
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

    // Client-side offline fallback (shows suggestions even if /places fails)
    const OFFLINE_PLACES = [
      {name:'New Delhi', display_name:'New Delhi, Delhi, India', lat:28.6139, lon:77.2090, tz:'Asia/Kolkata'},
      {name:'Delhi', display_name:'Delhi, India', lat:28.6517, lon:77.2219, tz:'Asia/Kolkata'},
      {name:'Mumbai', display_name:'Mumbai, Maharashtra, India', lat:19.0760, lon:72.8777, tz:'Asia/Kolkata'},
      {name:'Kolkata', display_name:'Kolkata, West Bengal, India', lat:22.5726, lon:88.3639, tz:'Asia/Kolkata'},
      {name:'Chennai', display_name:'Chennai, Tamil Nadu, India', lat:13.0827, lon:80.2707, tz:'Asia/Kolkata'},
      {name:'Bengaluru', display_name:'Bengaluru, Karnataka, India', lat:12.9716, lon:77.5946, tz:'Asia/Kolkata'},
      {name:'Hyderabad', display_name:'Hyderabad, Telangana, India', lat:17.3850, lon:78.4867, tz:'Asia/Kolkata'},
      {name:'Pune', display_name:'Pune, Maharashtra, India', lat:18.5204, lon:73.8567, tz:'Asia/Kolkata'}
    ];
    function localSearch(q){
      const s = (q||'').toLowerCase();
      if(!s||s.length<2) return [];
      return OFFLINE_PLACES.filter(p=>p.name.toLowerCase().includes(s)||p.display_name.toLowerCase().includes(s)).slice(0,8);
    }

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
      const map = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
      return String(s).replace(/[&<>"']/g, ch => map[ch]);
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
          if (!r.ok) throw new Error('fetch failed: ' + r.status);
          const data = await r.json();
          if (data && Array.isArray(data.results) && data.results.length) {
            renderList(data.results);
          } else {
            const fb = localSearch(q);
            if (fb.length) renderList(fb); else clearList();
          }
        } catch (err) {
          console.error('Typeahead fetch failed', err);
          const fb = localSearch((placeInput.value||'').trim());
          if (fb.length) renderList(fb); else clearList();
        }
      }, 120);
    });
  })();
  </script>
</body>
</html>
"""

PRIVACY_PAGE = """<div class="card card-body shadow-sm">
  <h1 class="h4 mb-2">Privacy Policy</h1>
  <p class="small text-muted mb-3">Last updated: 15 November 2025</p>

  <p>
    This Privacy Policy explains how Graha Guidance (“we”, “us”, “our”) collects, uses,
    and protects information when you use this website and the free astrology tools
    hosted on it.
  </p>

  <h2 class="h5 mt-3">1. Information we collect</h2>
  <p>We collect the following types of information that you voluntarily provide:</p>
  <ul>
    <li>Name or alias (if you choose to enter it).</li>
    <li>Birth details used for chart calculation (date, time, place, latitude, longitude, timezone).</li>
    <li>Any information you send to us by email or via the contact / grievance page.</li>
  </ul>
  <p>
    We do <strong>not</strong> knowingly collect sensitive identifiers such as PAN, Aadhaar,
    credit card numbers, or passwords through this site.
  </p>

  <h2 class="h5 mt-3">2. Non-personal &amp; usage data</h2>
  <p>
    Like most websites, we may automatically receive certain technical information,
    such as your IP address, browser type, approximate location, pages visited and
    time spent on the site. Analytics and advertising partners (for example,
    Google AdSense) may also collect anonymised usage data through cookies
    and similar technologies.
  </p>

  <h2 class="h5 mt-3">3. Cookies &amp; third-party services</h2>
  <p>
    This website may use cookies and similar technologies to:
  </p>
  <ul>
    <li>Remember basic preferences.</li>
    <li>Measure anonymous traffic and usage patterns.</li>
    <li>Serve personalised or contextual advertisements.</li>
  </ul>
  <p>
    Third-party services such as Google AdSense and affiliate program partners may
    set and read their own cookies. Their use of data is governed by their own
    privacy policies. You can usually control cookies through your browser settings.
  </p>

  <h2 class="h5 mt-3">4. How we use your information</h2>
  <p>We use the information we collect to:</p>
  <ul>
    <li>Generate astrology charts and textual interpretations for you.</li>
    <li>Improve, debug, and maintain the website and tools.</li>
    <li>Respond to your messages and queries.</li>
    <li>Comply with any legal obligations that may apply.</li>
  </ul>

  <h2 class="h5 mt-3">5. Legal basis &amp; data retention</h2>
  <p>
    We rely primarily on your consent and our legitimate interest in running and
    improving this website. Birth details submitted for calculations may be stored
    in logs or backups for a limited time for troubleshooting and performance
    monitoring, but are not sold or rented to third parties.
  </p>

  <h2 class="h5 mt-3">6. Sharing of information</h2>
  <p>
    We do <strong>not</strong> sell your personal data. We may share limited data:
  </p>
  <ul>
    <li>With service providers who help us host or maintain the site (e.g. hosting providers).</li>
    <li>When required by law, regulation, or a valid legal request from authorities.</li>
  </ul>

  <h2 class="h5 mt-3">7. Your rights</h2>
  <p>
    Subject to applicable law, you may request that we:
  </p>
  <ul>
    <li>Inform you what personal data we hold about you.</li>
    <li>Correct inaccurate information.</li>
    <li>Delete information that is no longer required for the purposes described above.</li>
  </ul>
  <p>
    To exercise these rights, please write to us using the contact / grievance
    details on the Contact page. We may need reasonable proof of identity before
    acting on such requests.
  </p>

  <h2 class="h5 mt-3">8. Children’s privacy</h2>
  <p>
    This website is not specifically directed at children under 18. Parents or
    guardians who use the site on behalf of a minor are responsible for supervising
    and consenting to the sharing of that minor’s details.
  </p>

  <h2 class="h5 mt-3">9. Changes to this policy</h2>
  <p>
    We may update this Privacy Policy from time to time. The “Last updated” date
    at the top of this page will change when we do so. Your continued use of the
    site after such changes will be treated as your acceptance of the updated policy.
  </p>

  <h2 class="h5 mt-3">10. How to contact us</h2>
  <p>
    If you have any questions, concerns or requests about this Privacy Policy,
    please contact us via the details given on the Contact / Grievance page.
  </p>
</div>
"""

TERMS_PAGE = """<div class="card card-body shadow-sm">
  <h1 class="h4 mb-2">Terms &amp; Conditions</h1>
  <p class="small text-muted mb-3">Last updated: 15 November 2025</p>

  <p>
    By accessing or using this website (“Site”) and the astrology tools made
    available here, you agree to these Terms &amp; Conditions. If you do not
    agree, please do not use the Site.
  </p>

  <h2 class="h5 mt-3">1. Nature of service</h2>
  <p>
    This Site provides free astrology-based chart calculations and interpretive
    text for informational and entertainment purposes only. No paid consultation
    or personalised professional service is offered on this Site itself.
  </p>

  <h2 class="h5 mt-3">2. No professional advice</h2>
  <p>
    Astrology interpretations are inherently subjective and speculative. Nothing
    provided on this Site should be treated as a substitute for professional
    advice in areas such as medicine, psychology, finance, investment, taxation,
    legal matters, or career counselling. You are solely responsible for any
    decisions you take based on the content generated here.
  </p>

  <h2 class="h5 mt-3">3. User responsibilities</h2>
  <ul>
    <li>You agree to provide accurate birth details to the best of your knowledge.</li>
    <li>You agree not to misuse the Site, attempt to break security, or disrupt service.</li>
    <li>You agree not to use the Site for any unlawful or harmful purpose.</li>
  </ul>

  <h2 class="h5 mt-3">4. Intellectual property</h2>
  <p>
    The underlying code, layout, and text content (except for any third-party
    trademarks or logos) are the intellectual property of the site owner and
    are protected by applicable copyright and other IP laws. You may not copy,
    reproduce, or republish substantial portions of the Site without prior
    written permission, except for personal, non-commercial use.
  </p>

  <h2 class="h5 mt-3">5. Third-party links &amp; affiliates</h2>
  <p>
    This Site may contain links to third-party websites, reports, or services,
    some of which are affiliate links. We do not own or control these third-party
    sites and are not responsible for their content, terms, or policies. Your
    interactions with any third-party are solely between you and that party.
  </p>

  <h2 class="h5 mt-3">6. Limitation of liability</h2>
  <p>
    To the fullest extent permitted by law, the Site owner shall not be liable
    for any direct, indirect, incidental, consequential or special loss or damage
    arising out of or in connection with the use of, or inability to use, this
    Site or any content provided through it.
  </p>

  <h2 class="h5 mt-3">7. Indemnity</h2>
  <p>
    You agree to indemnify and hold harmless the Site owner from any claims,
    losses, liabilities, damages, costs or expenses (including reasonable legal
    fees) arising out of your use of the Site or breach of these Terms.
  </p>

  <h2 class="h5 mt-3">8. Changes to the Site</h2>
  <p>
    We may modify, suspend or discontinue any part of the Site at any time,
    including adding or removing features or content, without prior notice.
  </p>

  <h2 class="h5 mt-3">9. Changes to these Terms</h2>
  <p>
    We may revise these Terms &amp; Conditions from time to time. The “Last
    updated” date will reflect the latest version. Continued use of the Site
    after a change implies your acceptance of the revised Terms.
  </p>

  <h2 class="h5 mt-3">10. Governing law</h2>
  <p>
    These Terms are governed by, and shall be construed in accordance with,
    the laws of India. Any disputes shall be subject to the exclusive jurisdiction
    of the courts of New Delhi, India, to the extent permitted by law.
  </p>
</div>
"""

DISCLAIMER_PAGE = """<div class="card card-body shadow-sm">
  <h1 class="h4 mb-2">Disclaimer &amp; Affiliate Disclosure</h1>
  <p class="small text-muted mb-3">Last updated: 15 November 2025</p>

  <h2 class="h5 mt-2">1. General disclaimer</h2>
  <p>
    All content and interpretations provided on this Site are based on traditional
    principles of Vedic astrology and related methods. Astrology is an interpretive
    discipline and not an exact science. While care is taken to keep the
    calculations consistent, <strong>no guarantee is made</strong> that any
    prediction or assessment will prove accurate in real life.
  </p>
  <p>
    You should not rely on the information on this Site as a sole basis for making
    important decisions about your health, finances, relationships, legal matters,
    or career. Please consult qualified professionals for advice specific to your
    situation.
  </p>

  <h2 class="h5 mt-3">2. No professional relationship</h2>
  <p>
    Your use of this Site does not create a doctor–patient, therapist–client,
    lawyer–client, financial advisor–client or any other professional relationship.
    The Site is offered purely for informational and educational purposes, and as
    a free tool for exploration and self-reflection.
  </p>

  <h2 class="h5 mt-3">3. Accuracy of data</h2>
  <p>
    The quality of any chart or interpretation depends heavily on the accuracy of
    the birth data you provide (date, time and place of birth). We cannot be held
    responsible for inaccurate results that arise from incorrect or incomplete
    data supplied by you.
  </p>

  <h2 class="h5 mt-3">4. Affiliate disclosure</h2>
  <p>
    Some pages on this Site contain links to third-party astrological reports,
    products or services. These are affiliate links. This means that if you click
    on such a link and subsequently make a purchase, we may receive a small
    commission at <strong>no additional cost to you</strong>.
  </p>
  <p>
    We only refer to services that we consider relevant to our audience, but we
    do not control or manage any third-party products, websites or support. Any
    issues with purchases, refunds or service quality must be taken up directly
    with the respective third-party provider.
  </p>

  <h2 class="h5 mt-3">5. External links</h2>
  <p>
    Links to external websites are provided for convenience and informational
    purposes. Inclusion of a link does not imply endorsement, approval, or
    guarantee of the content, policies, or services of those websites.
  </p>

  <h2 class="h5 mt-3">6. Limitation of liability</h2>
  <p>
    To the maximum extent permitted by law, the Site owner disclaims all liability
    for any loss, damage, injury or inconvenience arising from the use of, or
    reliance upon, the information and tools provided on this Site or on any
    linked external sites.
  </p>

  <h2 class="h5 mt-3">7. Acceptance of this Disclaimer</h2>
  <p>
    By using this Site, you acknowledge that you have read, understood and agreed
    to this Disclaimer &amp; Affiliate Disclosure.
  </p>
</div>
"""

CONTACT_PAGE = """<div class="card card-body shadow-sm">
  <h1 class="h4 mb-2">Contact &amp; Grievance Redressal</h1>
  <p class="small text-muted mb-3">Last updated: 15 November 2025</p>

  <p>
    If you have any questions, feedback, concerns, or grievances related to this
    website or the content hosted on it, you can reach out using the details below.
  </p>

  <h2 class="h5 mt-3">1. General contact</h2>
  <p>
    For general queries, suggestions, or correction requests, please write to:
  </p>
  <p class="mb-2">
    <strong>Email:</strong> <span class="mono">contact@grahaguidance.com</span>
  </p>
  <p class="small text-muted">
    (If this email ID changes, the latest contact details will be updated on this page.)
  </p>

  <h2 class="h5 mt-3">2. Grievance redressal</h2>
  <p>
    If you believe that any content on this Site is inaccurate, inappropriate,
    violates your privacy, or otherwise causes concern, you may submit a grievance
    by email with the subject line <strong>“Grievance – Graha Guidance”</strong>
    and include:
  </p>
  <ul>
    <li>Your full name and a valid contact email address.</li>
    <li>A clear description of your concern or the content in question.</li>
    <li>Any supporting details or links that help us identify the issue.</li>
  </ul>
  <p>
    We aim to acknowledge genuine grievances within 7 working days and, where
    appropriate, attempt to resolve them within a reasonable timeframe.
  </p>

  <h2 class="h5 mt-3">3. Data &amp; privacy requests</h2>
  <p>
    To request access to, correction of, or deletion of personal data that you
    believe we may be holding about you in connection with this Site, please
    contact us via email and clearly mention that your request relates to
    “Privacy / Data Protection”.
  </p>

  <h2 class="h5 mt-3">4. Responsible &amp; respectful communication</h2>
  <p>
    We request that all communication remain respectful and relevant. Abusive,
    threatening, or spam messages may be ignored or blocked.
  </p>
</div>
"""

def _render_any(value):
    if isinstance(value, str):
        s = value.lstrip()
        low = s.lower()
        if low.startswith("<!doctype") or "<html" in low:
            return Response(s, mimetype="text/html")
        return render_template_string(BASE, body=Markup(s), services=Markup(_render_services_panel()), services_below=Markup(_render_services_panel()))
    if pd is not None and isinstance(value, pd.DataFrame):
        table = value.to_html(index=False, classes="table table-striped table-sm")
        return render_template_string(BASE, body=Markup(table), services=Markup(_render_services_panel()), services_below=Markup(_render_services_panel()))
    if isinstance(value, (dict, list, tuple)):
        try:
            pretty = json.dumps(value, indent=2, default=str)
            return render_template_string(BASE, body=Markup(f"<pre>{html.escape(pretty)}</pre>"), services=Markup(_render_services_panel()), services_below=Markup(_render_services_panel()))
        except Exception:
            pass
    return render_template_string(BASE, body=Markup(f"<pre>{html.escape(str(value))}</pre>"), services=Markup(_render_services_panel()), services_below=Markup(_render_services_panel()))


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
    return render_template_string(BASE, body=Markup("<div class='help'>Results will appear here after you submit.</div>"), services=Markup(_render_services_panel()), services_below=Markup(""))


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
        return render_template_string(BASE, body=Markup(msg), services=Markup(_render_services_panel()), services_below=Markup(""))

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
    # HTML mode
    return _render_any(result)
    
@app.route("/privacy-policy", methods=["GET"])
def privacy_policy():
    return render_template_string(
        BASE,
        body=Markup(PRIVACY_PAGE),
        services=Markup(""),
        services_below=Markup(""),
    )

@app.route("/terms", methods=["GET"])
def terms():
    return render_template_string(
        BASE,
        body=Markup(TERMS_PAGE),
        services=Markup(""),
        services_below=Markup(""),
    )

@app.route("/disclaimer", methods=["GET"])
def disclaimer():
    return render_template_string(
        BASE,
        body=Markup(DISCLAIMER_PAGE),
        services=Markup(""),
        services_below=Markup(""),
    )

@app.route("/contact", methods=["GET"])
def contact():
    return render_template_string(
        BASE,
        body=Markup(CONTACT_PAGE),
        services=Markup(""),
        services_below=Markup(""),
    )


# ---------- Places API (typeahead) ----------
@dataclass
class Place:
    name: str
    display_name: str
    lat: float
    lon: float
    tz: Optional[str] = None
    pop: Optional[int] = None
    admin1: Optional[str] = None
    country: Optional[str] = None

# Minimal built-in fallback so it works even with no CSV
_FALLBACK_PLACES: List[Place] = [
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

_GAZ: List[Place] = []
_GAZ_NORM: List[tuple[str, int]] = []  # (normalized_display, index)

_DEF_DATA_PATHS = [
    Path("/mnt/data/gazetteer.csv"),      # upload here on Render
    Path("data/gazetteer.csv"),           # or keep committed in repo
    Path("/mnt/data/gazetteer.jsonl.gz"), # optional JSONL.GZ (one JSON per line)
    Path("data/gazetteer.jsonl.gz"),
]

def _norm(s: str) -> str:
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()

_DEF_TZ = "Asia/Kolkata"

def _make_display(name: str, admin1: Optional[str], country: Optional[str]) -> str:
    parts = [name]
    if admin1: parts.append(admin1)
    if country: parts.append(country)
    return ", ".join(parts)

def _load_gazetteer(paths: Optional[List[Path]] = None) -> None:
    global _GAZ, _GAZ_NORM
    _GAZ.clear(); _GAZ_NORM.clear()
    paths = paths or _DEF_DATA_PATHS

    loaded = False
    for p in paths:
        if not p.exists():
            continue
        try:
            if p.suffix.lower() == ".gz":
                import json as _json
                with gzip.open(p, "rb") as fh:
                    for line in fh:
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        name = str(rec.get("name") or "").strip()
                        if not name: continue
                        lat = float(rec.get("lat")); lon = float(rec.get("lon"))
                        admin1 = (rec.get("admin1") or rec.get("state") or "") or None
                        country = (rec.get("country") or rec.get("country_name") or "") or None
                        tz = (rec.get("tz") or rec.get("timezone") or "") or None
                        pop = rec.get("pop") or rec.get("population") or None
                        try: pop = int(pop) if pop is not None else None
                        except Exception: pop = None
                        disp = rec.get("display_name") or _make_display(name, admin1, country)
                        if not tz: tz = _tz_from_latlon(lat, lon) or _DEF_TZ
                        _GAZ.append(Place(name=name, display_name=disp, lat=lat, lon=lon, tz=tz, pop=pop, admin1=admin1, country=country))
                loaded = True
                break
            else:
                # CSV
                with open(p, "r", encoding="utf-8", newline="") as fh:
                    rdr = csv.DictReader(fh)
                    for row in rdr:
                        try:
                            name = (row.get("name") or row.get("city") or "").strip()
                            if not name: continue
                            lat = float(row.get("lat") or row.get("latitude"))
                            lon = float(row.get("lon") or row.get("longitude"))
                            admin1 = (row.get("admin1") or row.get("state") or "").strip() or None
                            country = (row.get("country") or row.get("country_name") or row.get("iso2") or "").strip() or None
                            tz = (row.get("tz") or row.get("timezone") or "").strip() or None
                            pop = row.get("pop") or row.get("population") or None
                            try: pop = int(pop) if pop not in (None, "") else None
                            except Exception: pop = None
                            disp = row.get("display_name") or _make_display(name, admin1, country)
                            if not tz: tz = _tz_from_latlon(lat, lon) or _DEF_TZ
                            _GAZ.append(Place(name=name, display_name=disp, lat=lat, lon=lon, tz=tz, pop=pop, admin1=admin1, country=country))
                        except Exception:
                            continue
                loaded = True
                break
        except Exception:
            continue

    if not loaded:
        _GAZ.extend(_FALLBACK_PLACES)

    # Build normalized index
    for i, p in enumerate(_GAZ):
        _GAZ_NORM.append((_norm(f"{p.name} {p.display_name}"), i))

def _search_local(q: str, limit: int = 8) -> List[Place]:
    qn = _norm(q)
    if not qn: return []
    tokens = [t for t in re.split(r"\s+", qn) if t]
    scored: List[tuple[float, int]] = []
    for key, idx in _GAZ_NORM:
        if not all(t in key for t in tokens):  # all tokens must appear
            continue
        p = _GAZ[idx]
        starts = key.startswith(qn)
        pop = p.pop or 0
        score = (2.0 if starts else 1.0) + (min(pop, 10_000_000) / 10_000_000.0)
        scored.append((score, idx))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [_GAZ[idx] for (_, idx) in scored[:limit]]
    for p in out:
        if not p.tz:
            p.tz = _tz_from_latlon(p.lat, p.lon) or _DEF_TZ
    return out

# Initialize gazetteer once at startup
_load_gazetteer()

@app.route("/places")
def places():
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = _search_local(q, limit=8)
    payload = [{
        "name": p.name,
        "display_name": p.display_name,
        "lat": p.lat,
        "lon": p.lon,
        "tz": p.tz,
    } for p in results]
    return jsonify({ "results": payload })


# Gunicorn entrypoint: app:app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)

