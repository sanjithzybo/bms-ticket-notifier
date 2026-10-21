"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted in bms_state.json (committed back by the workflow).

Configure via environment variables or edit the CONFIG below.

BMS_URL can hold several movies, separated by ';' or new lines:
    https://in.bookmyshow.com/movies/.../ET00451914;https://in.bookmyshow.com/movies/.../ET00519195

Each entry may carry its own theatre filter after a '|':
    https://in.bookmyshow.com/movies/.../ET00451914|Aries Plex;https://in.bookmyshow.com/movies/.../ET00519195

An entry without '|' uses BMS_THEATRE (empty = all theatres).
BMS_DATES and BMS_TIME apply to every entry.
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars
# ──────────────────────────────────────────────────────────────────────
DEFAULT_URLS = (
    "https://in.bookmyshow.com/movies/thiruvananthapuram-trivandrum/ramayana/ET00451914"
    ";"
    "https://in.bookmyshow.com/movies/trivandrum/titans/ET00519195"
)

CONFIG = {
    "url": os.getenv("BMS_URL", "") or DEFAULT_URLS,
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = auto-discover
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "") or "onboarding@resend.dev"

STATE_FILE = "bms_state.json"
STATE_VERSION = 2
MAX_DATES_PER_WATCH = 7      # cap on auto-discovered dates per movie
IST = ZoneInfo("Asia/Kolkata")

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

# slug -> (region_code, region_slug, lat, lon, geohash)
# NOTE: the Trivandrum entries are NOT captured from DevTools: the code
# "TRIV" is from memory and the geohash is computed from the coordinates.
# If a run prints "No showtimes found" for a movie that is clearly on sale,
# copy the real x-region-code / x-geohash from the browser's Network tab.
_TVM = ("TRIV", "trivandrum", "8.524", "76.937", "t9w")
REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
    # Trivandrum — BMS URLs use either slug
    "trivandrum":                    _TVM,
    "thiruvananthapuram":            _TVM,
    "thiruvananthapuram-trivandrum": _TVM,
}


# ──────────────────────────────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_watches(raw):
    """'url|theatre;url2' -> [(url, theatre_or_None), ...]"""
    watches = []
    for item in re.split(r"[;\n]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        url, _, theatre = item.partition("|")
        watches.append((url.strip(), theatre.strip() or None))
    return watches


def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    """Returns (region_tuple, known: bool)."""
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key], True
    return (key.upper()[:6], key, "0", "0", ""), False


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "", "lsId": "", "subCode": "",
        "lat": lat, "lon": lon,
    }
    try:
        resp = requests.get(API_URL, headers=headers,
                            params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except (requests.RequestException, ValueError) as e:
        print(f"  Request failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=str(cat.get("availStatus", "")),
                        ))
                    shows.append(show)
    return shows


def count_showtime_widgets(data):
    return len(data.get("data", {}).get("showtimeWidgets", []) or [])


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        # Theatre filter
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue

        # Date filter
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        # Time period filter
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (per event, for change detection between runs)
#
# {"version": 2, "events": {"ET0045...": {"fingerprint": "...",
#                                          "shows": {...}, "dates": {...}}}}
# An old single-movie state file (no "version") is discarded.
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "events": {}}
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        print("  ℹ️  Old/unknown state format — starting fresh.")
        return {"version": STATE_VERSION, "events": {}}
    state.setdefault("events", {})
    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def build_event_state(shows, dates, fingerprint):
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            show_state[key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }

    return {
        "fingerprint": fingerprint,
        "shows": show_state,
        "dates": {d.date_code: d.status for d in dates if d.date_code},
    }


def detect_changes(old, new):
    changes = []

    # Dates opening (works even when there are zero shows)
    old_dates = old.get("dates", {})
    new_dates = new.get("dates", {})
    for dc, status in new_dates.items():
        if (old_dates.get(dc) == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = old.get("shows", {})
    new_shows = new.get("shows", {})

    # New showtimes
    for key in sorted(set(new_shows) - set(old_shows)):
        s = new_shows[key]
        changes.append(
            f"🆕 NEW: {s['venue']} {s['time']} [{s['date']}] "
            f"— {s['cat']} ₹{s['price']}"
        )

    # Sold out → available
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪")
            )
            changes.append(
                f"{ico} BACK: {new_s['venue']} {new_s['time']} "
                f"[{new_s['date']}] — {new_s['cat']} → {lbl}"
            )

    return changes


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def send_email(subject, changes, shows, movie_info, page_url):
    """Returns True if sent (or intentionally skipped), False on failure."""
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️  Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return True

    now_str = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    movie_name = movie_info.get("name", "Movie")

    # Build changes HTML
    changes_html = ""
    if changes:
        rows = "".join(
            f'<li style="padding:3px 0;font-size:14px;">{escape(c)}</li>'
            for c in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
            Changes Detected
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {rows}
        </ul>"""

    # Build shows section grouped by venue
    venue_groups = {}
    for s in shows:
        venue_groups.setdefault(s.venue_name, []).append(s)

    shows_html = ""
    for vname, vshows in venue_groups.items():
        show_rows = ""
        for s in vshows:
            cats = " | ".join(
                f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
            show_rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{escape(s.date_code)} {escape(s.time)}{fmt}</td>'
                f'<td style="padding:5px 8px;border-bottom:1px solid #ddd;'
                f'font-size:13px;vertical-align:top;">'
                f'{cats}</td>'
                f'</tr>'
            )

        shows_html += f"""
        <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
            {escape(vname)}
        </p>
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#f5f5f5;">
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Date / Time</th>
                <th style="padding:5px 8px;text-align:left;border-bottom:1px solid #ddd;
                           font-weight:bold;">Categories</th>
            </tr>
            {show_rows}
        </table>"""

    if not shows_html:
        shows_html = '<p style="font-size:13px;color:#666;">No shows listed yet.</p>'

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:18px;color:#111;">
        BMS Alert: {escape(movie_name)}
    </h2>
    <p style="margin:0 0 4px 0;font-size:13px;color:#666;">
        {escape(now_str)}
    </p>
    <p style="margin:0 0 20px 0;font-size:13px;">
        <a href="{escape(page_url, quote=True)}">Open on BookMyShow</a>
    </p>
    <hr style="border:none;border-top:1px solid #ddd;margin:0 0 20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:15px;font-weight:bold;color:#333;">
        Current Showtimes
    </h3>
    {shows_html}
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    # Build plain-text version with full show details
    plain_lines = [subject, "", f"Checked at: {now_str}", page_url, ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
        plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for vname, vshows in venue_groups.items():
        plain_lines.append(f"\n{vname}")
        for s in vshows:
            cats = " | ".join(
                f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                for c in s.categories
            )
            fmt = f" [{s.screen_attr}]" if s.screen_attr else ""
            plain_lines.append(f"  {s.date_code} {s.time}{fmt} - {cats}")
    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])
    plain = "\n".join(plain_lines)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
            return True
        print(f"  ❌ Resend {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")
    return False


# ──────────────────────────────────────────────────────────────────────
# ONE WATCH (one movie)
# ──────────────────────────────────────────────────────────────────────
def collect_shows(event_code, region, explicit_dates, url_date):
    """
    Fetch showtimes for one movie.
    - explicit_dates given  -> fetch exactly those dates.
    - otherwise             -> fetch the URL's date (or BMS default), then
                               auto-discover the other open dates from
                               the date strip (up to MAX_DATES_PER_WATCH).
    Returns (movie_info, shows, dates, any_fetch_ok).
    """
    region_code, region_slug, lat, lon, geohash = region

    queue = list(explicit_dates) if explicit_dates else [url_date or ""]
    fetched = set()
    shows_by_key = {}
    dates_by_code = {}
    movie_info = {"name": "Unknown", "language": ""}
    any_ok = False

    while queue and len(fetched) < MAX_DATES_PER_WATCH:
        dc = queue.pop(0)
        if dc in fetched:
            continue
        fetched.add(dc)

        data = fetch_bms(event_code, dc, region_code, region_slug,
                         lat, lon, geohash)
        if not data:
            print(f"  ⚠️  No data for date {dc or '(default)'}")
            continue
        any_ok = True

        if movie_info["name"] == "Unknown":
            movie_info = parse_movie_info(data)

        dates = parse_dates(data)
        shows = parse_shows(data)
        print(f"  🔎 date={dc or '(default)'}: keys={list(data.get('data', {}).keys())} "
              f"showtimeWidgets={count_showtime_widgets(data)} "
              f"dates={len(dates)} shows={len(shows)}")

        selected = next((d.date_code for d in dates
                         if d.status == "BOOKABLE"), "")
        for d in dates:
            if d.date_code:
                dates_by_code[d.date_code] = d
        if selected:
            fetched.add(selected)

        fallback_date = dc or selected
        for s in shows:
            if not s.date_code:
                s.date_code = fallback_date
            shows_by_key[(s.venue_code, s.session_id, s.date_code)] = s

        if not explicit_dates:
            for d in dates:
                if (d.status == "AVAILABLE" and d.date_code
                        and d.date_code not in fetched
                        and d.date_code not in queue):
                    queue.append(d.date_code)

    return movie_info, list(shows_by_key.values()), list(dates_by_code.values()), any_ok


def process_watch(url, theatre_override, old_event):
    """
    Returns (event_code, new_event_state_or_None, ok).
    new_event_state None means "keep the old state" (fetch/email failed).
    """
    parsed = parse_bms_url(url)
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code") or ""

    if not event_code or not region_slug:
        print(f"  ❌ Invalid BMS URL, could not extract event/region: {url}")
        return event_code or url, None, False

    region, known = resolve_region(region_slug)
    if not known:
        print(f"  ⚠️  Region '{region_slug}' is not in REGION_MAP — using a guessed "
              f"code '{region[0]}' with lat/lon 0. Expect empty results; add it.")

    raw_dates = CONFIG["dates"].strip()
    explicit_dates = [d.strip() for d in raw_dates.split(",") if d.strip()]
    theatre = theatre_override if theatre_override is not None else CONFIG["theatre"]

    print(f"  Event: {event_code}  Region: {region[0]} ({region[1]})  "
          f"Dates: {explicit_dates or [url_date or '(auto)']}  "
          f"Theatre: {theatre or '(all)'}")

    movie_info, all_shows, all_dates, any_ok = collect_shows(
        event_code, region, explicit_dates, url_date
    )

    if not any_ok:
        print("  ❌ Every request failed — keeping previous state.")
        return event_code, None, False

    print(f"  🎬 {movie_info['name']}  {movie_info['language']}")

    filtered = filter_shows(all_shows, theatre, CONFIG["time_period"],
                            CONFIG["dates"])
    print(f"  📊 {len(all_shows)} showtime(s) found, {len(filtered)} after filters")
    if not all_shows:
        print("  ❌ No showtimes found (not open yet, or wrong region config).")

    fingerprint = f"{theatre}|{CONFIG['time_period']}|{CONFIG['dates']}"
    new_event = build_event_state(filtered, all_dates, fingerprint)

    # Work out what changed
    changes = []
    if old_event is None:
        if filtered:
            changes = [f"🎬 Monitoring started — {len(filtered)} show(s) already listed"]
        else:
            print("  📝 First run for this movie — baseline saved (no shows yet).")
    elif old_event.get("fingerprint") != fingerprint:
        print("  ♻️  Filters changed since last run — resetting baseline silently.")
    else:
        changes = detect_changes(old_event, new_event)

    if changes:
        print(f"\n  ⚡ {len(changes)} change(s) detected:")
        for c in changes:
            print(f"     {c}")
        sent = send_email(
            f"BMS Alert: {movie_info['name']} - {len(changes)} change(s)",
            changes, filtered, movie_info, url,
        )
        if not sent:
            # Don't advance the state, so the next run retries the alert.
            return event_code, None, False
    else:
        print("  ✅ No changes since last check.")

    print(f"\n  Current status ({len(filtered)} shows):")
    for s in filtered:
        cats = ", ".join(
            f"{c.name}=₹{c.price}({_cat_status_label(c.status)})"
            for c in s.categories
        )
        fmt = f"|{s.screen_attr}" if s.screen_attr else ""
        print(f"    {s.venue_name} — {s.time}{fmt} [{s.date_code}] — {cats}")

    return event_code, new_event, True


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{now_str}] BMS Ticket Checker — CI mode")

    watches = parse_watches(CONFIG["url"])
    if not watches:
        print("  ❌ BMS_URL is empty.")
        sys.exit(1)

    state = load_state()
    failures = 0

    for i, (url, theatre_override) in enumerate(watches, 1):
        print(f"\n── Watch {i}/{len(watches)}: {url}")
        old_event = state["events"].get(parse_bms_url(url)["event_code"] or "")
        event_code, new_event, ok = process_watch(url, theatre_override, old_event)
        if new_event is not None:
            state["events"][event_code] = new_event
        if not ok:
            failures += 1

    save_state(state)

    print("\n  Done.")
    if failures:
        print(f"  ⚠️  {failures} watch(es) had errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()