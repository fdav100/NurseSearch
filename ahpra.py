"""AHPRA register lookup logic (no Streamlit imports, so it can be tested alone).

Per registration number:
  1. POST /api/Search/GetPredictions  -> confirms the number exists, gives name + profession
  2. POST /Registration/Registers-of-Practitioners (form) -> profile HTML, parsed for
     date of first registration, conditions, suburb, state.
"""
import random
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ahpra.gov.au"
REGISTER_URL = f"{BASE}/Registration/Registers-of-Practitioners"
PREDICT_URL = f"{BASE}/api/Search/GetPredictions"
TIMEOUT = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

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

BLOCK_MARKERS = (
    "just a moment",
    "cf-chl",
    "attention required",
    "the requested url was rejected",
    "enable javascript and cookies to continue",
)


class LookupFailed(Exception):
    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-AU,en;q=0.9"})
    return s


_NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
}


def _send(fn, tries=3):
    """Run a request, retrying network errors and transient 5xx with backoff."""
    for attempt in range(tries):
        try:
            r = fn()
        except requests.RequestException as e:
            if attempt == tries - 1:
                raise LookupFailed(f"Network error: {e}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (500, 502, 504) and attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
            continue
        return r


def looks_blocked(r):
    if r.status_code in (403, 429, 503):
        return True
    head = (r.text or "")[:3000].lower()
    return any(m in head for m in BLOCK_MARKERS)


def check_response(r):
    if looks_blocked(r):
        raise LookupFailed(
            f"Blocked by bot protection (HTTP {r.status_code}). "
            "Try again later, slow down, or run the app locally.",
            blocked=True,
        )
    if r.status_code >= 400:
        raise LookupFailed(f"HTTP {r.status_code}")


def warm_up(s):
    r = _send(lambda: s.get(REGISTER_URL, headers=_NAV_HEADERS, timeout=TIMEOUT))
    check_response(r)
    return r


def get_predictions(s, number):
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE,
        "Referer": REGISTER_URL,
        "X-Requested-With": "XMLHttpRequest",
        "X-Security-Request": "required",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    return _send(
        lambda: s.post(PREDICT_URL, data={"term": number}, headers=headers, timeout=TIMEOUT)
    )


def get_profile(s, number):
    form = {
        "health-profession": "",
        "state": "",
        "suburb": "",
        "postcode": "",
        "name-reg": number,
        "name-reg-detail": number,
        "event-category": "Practitioner Search",
        "event-action": "On Register of Practitioners page",
        "practitioner-row-id": "",
        "load-more-page-num": "",
        "health-profession-filters": "",
        "sex-filters": "",
        "language-filters": "",
    }
    headers = dict(_NAV_HEADERS)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE,
        "Referer": REGISTER_URL,
        "Cache-Control": "max-age=0",
        "Sec-Fetch-Site": "same-origin",
    })
    return _send(lambda: s.post(REGISTER_URL, data=form, headers=headers, timeout=TIMEOUT))


# --------------------------------------------------------------------------
# Parsing
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
def lookup(s, number, pause=(1.0, 2.0)):
    """Look up one number. Returns (row_dict, blocked_flag)."""
    row = {c: "" for c in COLUMNS}
    row["Registration number"] = number
    try:
        r = get_predictions(s, number)
        check_response(r)
        try:
            data = r.json()
        except ValueError:
            raise LookupFailed(
                "Search returned a non-JSON response (possible bot challenge)", blocked=True
            )

        results = data.get("Results") or [] if isinstance(data, dict) else []
        match = next(
            (x for x in results if str(x.get("id", "")).strip().upper() == number), None
        )
        if match is None:
            row["Result"] = "Not found"
            return row, False

        row["Name"] = tidy_name(match.get("name"))
        row["Profession"] = clean(match.get("profession"))

        time.sleep(random.uniform(*pause))

        r = get_profile(s, number)
        check_response(r)
        parsed = parse_profile(r.text, number)
        if parsed is None:
            raise LookupFailed("Profile page did not contain the expected registration details")

        row.update(parsed)
        row["Result"] = "Found"
        return row, False

    except LookupFailed as e:
        row["Result"] = "Error"
        row["Notes"] = str(e)
        return row, e.blocked


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
