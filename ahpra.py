"""AHPRA register lookup logic, browser-based (Playwright / headless Chromium).

A real browser loads the register page, so the site's own JavaScript (F5 / Cloudflare
checks) runs like it would for a normal visitor. Then, per registration number:
  1. In-page POST /api/Search/GetPredictions -> confirms the number exists, gives name + profession
  2. The browser submits the same form the site's "click the name" step submits
     -> profile HTML, parsed for date of first registration, conditions, suburb, state.

No stealth plugins, fingerprint spoofing or challenge-defeating tricks. If a CAPTCHA or
hard block appears, the lookup stops and reports it.

All Playwright objects live on ONE dedicated worker thread (BrowserSession). Streamlit
runs each script rerun on a different thread, and Playwright's sync API can't be used
across threads, so everything is sent to the worker through a queue.
"""
import json
import queue
import random
import re
import subprocess
import sys
import threading
import time

from bs4 import BeautifulSoup

BASE = "https://www.ahpra.gov.au"
REGISTER_PATH = "/Registration/Registers-of-Practitioners"
REGISTER_URL = f"{BASE}{REGISTER_PATH}"

COLUMNS = [
    "Registration number",
    "Result",
    "Name",
    "Profession",
    "Registration status",
    "Date of first registration",
    "Suburb",
    "State",
    "Has conditions",
    "Notes",
]

TITLES = {
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor",
    "a/prof", "assoc", "associate", "sir", "dame", "rev",
}

# Needed inside a container (no user namespaces / small /dev/shm). Not anti-detection.
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# Page text that means "this is the real site, not a challenge page"
REAL_PAGE_MARKERS = ("name-reg", "field-title", "section-row")
CF_MARKERS = (
    "just a moment",
    "cf-chl",
    "attention required",
    "enable javascript and cookies to continue",
    "the requested url was rejected",
)
CAPTCHA_MARKERS = (
    "g-recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "challenges.cloudflare.com/turnstile",
    "captcha-delivery",
)


class LookupFailed(Exception):
    def __init__(self, message, blocked=False, hard=False, snippet=""):
        super().__init__(message)
        self.hard = hard                  # CAPTCHA / hard block: stop, don't keep trying
        self.blocked = blocked or hard    # looks like bot protection
        self.snippet = snippet


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------
def ensure_chromium():
    """Download Chromium for Playwright if it isn't there yet (fast no-op afterwards)."""
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise LookupFailed(f"Could not install Chromium: {tail}")


def _first_line(e):
    lines = str(e).strip().splitlines()
    return f"{type(e).__name__}: {lines[0][:250] if lines else ''}"


class BrowserSession:
    """One headless Chromium, owned by one worker thread, reused for every lookup."""

    def __init__(self, start_timeout=120):
        self._jobs = queue.Queue()
        self._ready = threading.Event()
        self._start_error = None
        self.run_lock = threading.Lock()   # so only one run uses the browser at a time
        self._thread = threading.Thread(target=self._main, name="ahpra-browser", daemon=True)
        self._thread.start()
        if not self._ready.wait(start_timeout):
            raise LookupFailed("Browser start timed out")
        if self._start_error:
            raise LookupFailed(f"Browser failed to start: {self._start_error}")

    # ---- worker thread ----------------------------------------------------
    def _main(self):
        pw = browser = context = page = None

        def launch():
            b = pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
            c = b.new_context(locale="en-AU", viewport={"width": 1280, "height": 900})
            c.set_default_timeout(30000)
            return b, c, c.new_page()

        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser, context, page = launch()
        except Exception as e:  # noqa: BLE001
            self._start_error = _first_line(e)
            self._ready.set()
            try:
                if pw:
                    pw.stop()
            except Exception:  # noqa: BLE001
                pass
            return
        self._ready.set()

        fresh_page = False
        while True:
            job = self._jobs.get()
            if job is None:
                break
            fn, args, out = job
            try:
                if not browser.is_connected():
                    browser, context, page = launch()
                    fresh_page = False
                elif fresh_page or page.is_closed():
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
                    page = context.new_page()
                    fresh_page = False
                out.put((True, fn(page, *args)))
            except LookupFailed as e:
                out.put((False, e))
            except Exception as e:  # noqa: BLE001
                fresh_page = True  # page may be broken; start clean next time
                out.put((False, LookupFailed(f"Browser error: {_first_line(e)}")))

        for obj in (context, browser):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---- called from any thread -------------------------------------------
    def call(self, fn, *args, timeout=150):
        if not self.alive():
            raise LookupFailed("Browser is not running")
        out = queue.Queue(1)
        self._jobs.put((fn, args, out))
        try:
            ok, val = out.get(timeout=timeout)
        except queue.Empty:
            raise LookupFailed("Browser did not respond in time")
        if ok:
            return val
        raise val

    def alive(self):
        return self._thread.is_alive()

    def close(self):
        self._jobs.put(None)


# --------------------------------------------------------------------------
# Page-level steps (these run ON the browser worker thread, given the page)
# --------------------------------------------------------------------------
PREDICT_JS = """
async (term) => {
  try {
    const r = await fetch('/api/Search/GetPredictions', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'X-Security-Request': 'required'
      },
      body: new URLSearchParams({term: term}).toString()
    });
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: 0, text: '', error: String(e)};
  }
}
"""

SUBMIT_JS = """
(fields) => {
  const f = document.createElement('form');
  f.method = 'POST';
  f.action = '%s';
  f.style.display = 'none';
  for (const [k, v] of Object.entries(fields)) {
    const i = document.createElement('input');
    i.type = 'hidden'; i.name = k; i.value = v;
    f.appendChild(i);
  }
  document.body.appendChild(f);
  f.submit();
}
""" % REGISTER_PATH


def classify(html):
    """'ok' (real site content), 'challenge' (bot-check page) or 'captcha' (needs a human)."""
    low = (html or "").lower()
    if any(m in low for m in REAL_PAGE_MARKERS):
        return "ok"
    if any(m in low for m in CAPTCHA_MARKERS):
        return "captcha"
    if (
        not low.strip()
        or any(m in low[:5000] for m in CF_MARKERS)
        or (("bobcmn" in low or "/tspd/" in low) and len(low) < 20000)
    ):
        return "challenge"
    return "ok"


def _content(page):
    try:
        return page.content()
    except Exception:  # noqa: BLE001  (e.g. mid-navigation)
        return ""


def _settle(page, require=None, timeout=25):
    """Give the page's own JavaScript time to finish any bot check, then return the HTML.

    Raises LookupFailed if a CAPTCHA appears (hard stop), or the check doesn't clear.
    """
    start = time.time()
    ok_since = None
    html = ""
    while True:
        html = _content(page)
        state = classify(html)
        if state == "captcha":
            raise LookupFailed(
                "A CAPTCHA / human check is being shown. Stopping; this app won't try to bypass it.",
                hard=True, snippet=html[:600],
            )
        if state == "ok" and html:
            if not require or any(m in html.lower() for m in require):
                return html
            ok_since = ok_since or time.time()
            if time.time() - ok_since > 12:
                raise LookupFailed(
                    "Page loaded but the expected content wasn't on it", snippet=html[:600]
                )
        elif time.time() - start > timeout:
            raise LookupFailed(
                "Bot-check page did not clear (the browser was challenged and not let through)",
                blocked=True, snippet=html[:600],
            )
        page.wait_for_timeout(1000)


def _pg_load(page):
    resp = page.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=45000)
    status = resp.status if resp else None
    html = _settle(page)
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    html = _content(page) or html
    low = html.lower()
    if status in (403, 429, 503) and not any(m in low for m in REAL_PAGE_MARKERS):
        raise LookupFailed(
            f"The site refused the request (HTTP {status})", blocked=True, snippet=html[:600]
        )
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    return {
        "status": status,
        "url": page.url,
        "title": title,
        "length": len(html),
        "has_form": "name-reg" in low,
    }


def _ensure_loaded(page):
    if not page.url.startswith(BASE):
        _pg_load(page)


OVERLAY_JS = """
() => ({
  frames: Array.from(document.querySelectorAll('iframe')).map(f => {
    const r = f.getBoundingClientRect();
    const cs = getComputedStyle(f);
    return {
      src: (f.src || f.getAttribute('src') || '').slice(0, 200),
      w: Math.round(r.width), h: Math.round(r.height),
      visible: r.width > 20 && r.height > 20 &&
               cs.visibility !== 'hidden' && cs.display !== 'none'
    };
  }),
  dialogs: Array.from(document.querySelectorAll('[role=dialog],[aria-modal=true],.modal'))
    .map(e => (e.innerText || '').trim().slice(0, 200)).filter(Boolean)
})
"""


def _pg_overlay_info(page):
    try:
        return page.evaluate(OVERLAY_JS)
    except Exception:  # noqa: BLE001
        return {"frames": [], "dialogs": []}


def _describe_overlay(info):
    parts = []
    for f in info.get("frames", []):
        if f.get("visible"):
            parts.append(f"visible iframe {f['w']}x{f['h']}: {f['src'] or '(no src)'}")
    for d in info.get("dialogs", []):
        parts.append(f"dialog text: {d}")
    return "; ".join(parts) or "no visible iframe or dialog found"


SEARCH_BOX = (
    'input[name="name-reg"]:visible, '
    'input[placeholder*="Registration Number" i]:visible'
)


def _find_search_box(page):
    box = page.locator(SEARCH_BOX).first
    try:
        box.wait_for(state="visible", timeout=8000)
        return box
    except Exception:  # noqa: BLE001
        return None


def _pg_predict(page, number):
    """Type the number into the site's own search box (like a visitor would) and read the
    response to the page's own GetPredictions request."""
    _ensure_loaded(page)
    box = _find_search_box(page)
    if box is None:
        _pg_load(page)
        box = _find_search_box(page)
    if box is None:
        raise LookupFailed("Couldn't find the search box on the register page")

    seen = []

    def on_resp(r):
        try:
            if (
                "/api/search/getpredictions" in r.url.lower()
                and number in (r.request.post_data or "").upper()
            ):
                seen.append(r)
        except Exception:  # noqa: BLE001
            pass

    page.on("response", on_resp)
    status = 0
    try:
        box.click()
        box.fill("")
        box.press_sequentially(number, delay=90)
        # Give the page's own scripts time to finish any bot check, then read the answer.
        deadline = time.time() + 15
        while time.time() < deadline:
            for r in list(seen):
                status = r.status
                try:
                    text = r.text()
                except Exception:  # noqa: BLE001
                    text = ""
                if text.strip():
                    return {"status": status, "text": text}
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", on_resp)

    info = _pg_overlay_info(page)
    desc = _describe_overlay(info)
    for f in info.get("frames", []):
        if f.get("visible") and any(
            m in (f.get("src") or "").lower()
            for m in ("captcha", "turnstile", "challenge", "tspd")
        ):
            raise LookupFailed(
                "A human check (CAPTCHA/challenge) is being shown. Stopping; "
                "this app won't try to bypass it.",
                hard=True, snippet=desc,
            )
    return {
        "status": status or 0,
        "text": "",
        "overlay": desc,
        "error": "" if seen else "page never made the search request",
    }


def _pg_profile(page, number, row_id):
    _ensure_loaded(page)
    fields = {
        "health-profession": "",
        "state": "",
        "suburb": "",
        "postcode": "",
        "name-reg": number,
        "name-reg-detail": number,
        "event-category": "Practitioner Search",
        "event-action": "On Register of Practitioners page",
        "practitioner-row-id": row_id or "",
        "load-more-page-num": "",
        "health-profession-filters": "",
        "sex-filters": "",
        "language-filters": "",
    }
    with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
        page.evaluate(SUBMIT_JS, fields)
    html = _settle(page, require=("field-title", "section-row"))
    try:
        page.wait_for_load_state("load", timeout=10000)
    except Exception:  # noqa: BLE001
        pass
    return _content(page) or html


def _pg_screenshot(page):
    try:
        return page.screenshot(type="png")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Calls from the app side
# --------------------------------------------------------------------------
def warm_up(sess):
    """(Re)load the register page in the browser; returns a small info dict."""
    return sess.call(_pg_load)


def get_predictions(sess, number):
    """In-page GetPredictions call. Returns parsed JSON dict. Retries once after a reload."""
    last, text, overlay = "an empty response", "", ""
    for attempt in range(2):
        res = sess.call(_pg_predict, number)
        status = res.get("status")
        text = (res.get("text") or "").strip()
        overlay = res.get("overlay") or overlay
        if status in (403, 429):
            last = f"HTTP {status}"
        elif status == 200 and text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
            last = "a non-JSON response"
        else:
            last = f"an empty response (HTTP {status})" + (
                f", {res['error']}" if res.get("error") else ""
            )
        if attempt == 0:
            warm_up(sess)
            time.sleep(random.uniform(2.0, 4.0))
    raise LookupFailed(
        f"Search returned {last} (bot check likely not passed)", blocked=True,
        snippet=(f"On the page: {overlay}\n" if overlay else "") + text[:500],
    )


def get_profile_html(sess, number, row_id=""):
    return sess.call(_pg_profile, number, row_id)


# --------------------------------------------------------------------------
# Parsing (unchanged)
# --------------------------------------------------------------------------
def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def tidy_name(raw):
    tokens = clean(raw).split(" ")
    while len(tokens) > 2 and tokens[0].lower().rstrip(".") in TITLES:
        tokens.pop(0)
    return " ".join(tokens)


def _row_pairs(container):
    for row in container.select("div.section-row"):
        title = row.select_one("div.field-title")
        entry = row.select_one("div.field-entry")
        if title and entry:
            yield clean(title.get_text(" ")), clean(entry.get_text(" "))


def parse_profile(html, number):
    """Return a dict of parsed fields, or None if this isn't the expected profile page."""
    soup = BeautifulSoup(html, "html.parser")
    pairs = list(_row_pairs(soup))

    has_reg_row = any(
        t.lower().startswith("registration number") and number in e.upper()
        for t, e in pairs
    )
    if not has_reg_row:
        return None

    out = {}
    notes = []

    # Registration status (first occurrence)
    out["Registration status"] = next(
        (e for t, e in pairs if t.lower() == "registration status"), ""
    )

    # Date of first registration
    date = ""
    for t, e in pairs:
        if t.lower().startswith("date of first registration"):
            m = re.search(r"\d{1,2}/\d{1,2}/\d{4}", e)
            date = m.group(0) if m else e
            break
    out["Date of first registration"] = date
    if not date:
        notes.append("Date of first registration not found on page")

    # Conditions: every Conditions row on the page (summary + each division)
    cond = [e for t, e in pairs if t.lower().startswith("conditions")]
    values = [c for c in cond if c]
    if not values:
        out["Has conditions"] = "Check manually"
        notes.append("No Conditions row found on page")
    elif any(v.lower() != "none" for v in values):
        out["Has conditions"] = "Yes"
    else:
        out["Has conditions"] = "No"

    # Principal place of practice (blank if section missing)
    suburb = state = ""
    for title_div in soup.select("div.section-title"):
        if "principal place of practice" in clean(title_div.get_text()).lower():
            section = title_div.parent
            place = {t.lower(): e for t, e in _row_pairs(section)}
            suburb = place.get("suburb", "")
            state = place.get("state", "")
            break
    out["Suburb"] = suburb
    out["State"] = state

    out["Notes"] = "; ".join(notes)
    return out


# --------------------------------------------------------------------------
# One full lookup
# --------------------------------------------------------------------------
def _find_match(data, number):
    results = data.get("Results")
    if not isinstance(results, list):
        results = []
    return next(
        (x for x in results if str(x.get("id", "")).strip().upper() == number), None
    )


def lookup(sess, number, pause=(1.0, 2.0)):
    """Look up one number. Returns (row_dict, blocked_flag, hard_block_flag)."""
    row = {c: "" for c in COLUMNS}
    row["Registration number"] = number
    try:
        match = _find_match(get_predictions(sess, number), number)
        if match is None:
            row["Result"] = "Not found"
            return row, False, False

        row["Name"] = tidy_name(match.get("name"))
        row["Profession"] = clean(match.get("profession"))

        time.sleep(random.uniform(*pause))

        html = get_profile_html(sess, number, str(match.get("id", "")).strip())
        parsed = parse_profile(html, number)
        if parsed is None:
            raise LookupFailed("Profile page did not contain the expected registration details")

        row.update(parsed)
        row["Result"] = "Found"
        return row, False, False

    except LookupFailed as e:
        row["Result"] = "Error"
        row["Notes"] = str(e)
        return row, e.blocked, e.hard


# --------------------------------------------------------------------------
# Diagnostics (generator: yields one dict per step so the UI can show progress)
# --------------------------------------------------------------------------
def diagnose(sess, number):
    def shot():
        try:
            return sess.call(_pg_screenshot, timeout=30)
        except LookupFailed:
            return None

    def fail(label, e):
        return {
            "label": label, "ok": False, "summary": str(e),
            "snippet": e.snippet, "screenshot": shot(), "parsed": None,
        }

    label = "1. Load register page in the browser"
    try:
        info = warm_up(sess)
    except LookupFailed as e:
        yield fail(label, e)
        return
    yield {
        "label": label, "ok": True, "parsed": None, "snippet": "", "screenshot": None,
        "summary": (
            f"loaded (first response HTTP {info['status']}), page title \"{info['title']}\", "
            f"{info['length']:,} bytes, search form "
            f"{'found' if info['has_form'] else 'NOT found'}. Bot check passed."
        ),
    }
    time.sleep(random.uniform(1.0, 2.0))

    label = "2. Search (GetPredictions)"
    try:
        data = get_predictions(sess, number)
    except LookupFailed as e:
        yield fail(label, e)
        return
    match = _find_match(data, number)
    if match is None:
        yield {
            "label": label, "ok": True, "parsed": None, "snippet": "", "screenshot": None,
            "summary": "search worked but returned no match for this number "
                       "(browser is getting through; the number just wasn't found).",
        }
        return
    yield {
        "label": label, "ok": True, "parsed": None, "snippet": "", "screenshot": None,
        "summary": f"match: {tidy_name(match.get('name'))} ({clean(match.get('profession'))})",
    }
    time.sleep(random.uniform(1.0, 2.0))

    label = "3. Profile page"
    try:
        html = get_profile_html(sess, number, str(match.get("id", "")).strip())
    except LookupFailed as e:
        yield fail(label, e)
        return
    parsed = parse_profile(html, number)
    if parsed is None:
        yield {
            "label": label, "ok": False, "parsed": None, "screenshot": shot(),
            "summary": "profile page loaded but the expected fields couldn't be parsed.",
            "snippet": html[:600],
        }
        return
    yield {
        "label": label, "ok": True, "parsed": parsed, "snippet": "", "screenshot": None,
        "summary": "profile loaded and parsed.",
    }


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
def parse_numbers(text):
    seen, out = set(), []
    for tok in re.split(r"[\s,;]+", (text or "").upper()):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def looks_like_reg_number(n):
    return bool(re.fullmatch(r"[A-Z]{3}\d{10}", n))
