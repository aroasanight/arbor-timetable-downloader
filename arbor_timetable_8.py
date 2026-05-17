"""
Arbor Timetable → ICS Exporter  (v6 — web UI API)

The student app API (v1) was sunset on 2026-04-24.
This version uses the web UI calendar endpoint instead.

Workflow:
  1. Log in and fetch a broad calendar page (null dates = current view)
     to discover all unique event titles across the date range.
  2. Ask the user to name or exclude each title — BEFORE fetching tooltips.
  3. Walk through the date range week by week, collect event IDs for
     included titles only, then fetch tooltip detail for each one.
  4. Write a standard .ics file.
"""

import getpass
import re
import time
import uuid
from datetime import datetime, date, timedelta

import requests

# ── School constants ──────────────────────────────────────────────────────────
SCHOOL_HOST = "anthony-gell-school.uk.arbor.sc"
STUDENT_ID  = 1525   # from your calendar URL — change if logging in as someone else

# Tooltip requests are made one at a time; be polite to the server
TOOLTIP_DELAY = 0.15  # seconds between tooltip calls


# ══════════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════════

def login(username: str, password: str) -> str:
    """Log in via the web endpoint and return the mis session cookie."""
    url = f"https://{SCHOOL_HOST}/auth/login"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Host": SCHOOL_HOST,
        "Origin": f"https://{SCHOOL_HOST}",
        "Referer": f"https://{SCHOOL_HOST}/?/home-ui/index",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:150.0) Gecko/20100101 Firefox/150.0",
    }
    resp = requests.post(
        url,
        json={"items": [{"username": username, "password": password}]},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise ValueError("Login failed — check your username and password.")
    cookie = resp.cookies.get("mis")
    if not cookie:
        raise ValueError("Login succeeded but no session cookie was returned.")
    return cookie


def make_headers(mis_cookie: str) -> dict:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": f"https://{SCHOOL_HOST}",
        "Referer": f"https://{SCHOOL_HOST}/?/students/my-mis-ui/calendar/student-id/{STUDENT_ID}",
        "Host": SCHOOL_HOST,
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:150.0) Gecko/20100101 Firefox/150.0",
        "Cookie": f"mis={mis_cookie}",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Calendar API — fetch a week of HTML pages
# ══════════════════════════════════════════════════════════════════════════════

def fetch_calendar_pages(mis_cookie: str, start_date: date, end_date: date) -> list:
    """
    POST to the web UI calendar endpoint.
    Returns the list of weekly 'pages' from the response.
    The API always returns a multi-week view; we filter to the requested range.
    """
    url = f"https://{SCHOOL_HOST}/calendar-entry/list-static/format/json/"
    payload = {
        "action_params": {
            "view": "multiday",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "filters": [
                {
                    "field_name": "object",
                    "value": {"_objectTypeId": 1, "_objectId": STUDENT_ID},
                }
            ],
        }
    }
    resp = requests.post(url, json=payload, headers=make_headers(mis_cookie))
    resp.raise_for_status()
    data = resp.json()
    return (
        data.get("items", [{}])[0]
            .get("fields", {})
            .get("response", {})
            .get("value", {})
            .get("pages", [])
    )


# ══════════════════════════════════════════════════════════════════════════════
# HTML parsing
# ══════════════════════════════════════════════════════════════════════════════


def parse_events_from_page(page: dict) -> list:
    """
    Extract (event_id, date_str, start_datetime, end_datetime, title) from
    one weekly HTML page.  Returns [] for pages with no HTML (e.g. holidays).

    The calendar grid always has 5 columns (Mon–Fri), with a leading time-gutter
    column.  The <thead> <td> elements carry data-date attributes; the <tbody>
    <td> elements (same column order) contain the events.

    Rather than relying on a hardcoded positional offset into a flat split of
    the whole document, we:
      1. Parse the <thead> to build a list of 5 dates in column order.
      2. Parse the <tbody> to get the 6 <td> chunks (gutter + 5 days).
      3. Zip them together — tbody td[1] → col_dates[0], etc.
    """
    html = page.get("html", "")
    if not html:
        return []

    # ── Step 1: dates from thead, in column order ────────────────────────────
    thead_match = re.search(r"<thead.*?</thead>", html, re.DOTALL)
    if not thead_match:
        return []
    col_dates = re.findall(r'data-date="(\d{4}-\d{2}-\d{2})"', thead_match.group(0))
    if not col_dates:
        return []

    # ── Step 2: body <td> chunks ─────────────────────────────────────────────
    tbody_match = re.search(r"<tbody.*?</tbody>", html, re.DOTALL)
    if not tbody_match:
        return []
    # Split on <td …> — first chunk is pre-first-td noise, then one per td
    body_tds = re.split(r"<td[^>]*>", tbody_match.group(0))[1:]
    # body_tds[0] = time gutter, body_tds[1..5] = Mon..Fri
    # (always 6 entries because the grid is always 5 days + gutter)

    events = []
    seen_ids = set()

    for day_idx, date_str in enumerate(col_dates):
        td = body_tds[day_idx + 1] if (day_idx + 1) < len(body_tds) else ""
        if not td:
            continue

        matches = re.findall(
            r'ajax-link="/students/calendar-entry/tooltip/id/(\d+)"'
            r'.*?<span[^>]*>(\d{2}:\d{2})-(\d{2}:\d{2})<'
            r'.*?<b class="title"[^>]*>([^<]+)<',
            td,
            re.DOTALL,
        )
        for ev_id, start_t, end_t, title in matches:
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)
            events.append({
                "id":    ev_id,
                "date":  date_str,
                "start": f"{date_str} {start_t}:00",
                "end":   f"{date_str} {end_t}:00",
                "title": title.strip(),
            })

    return events


# ══════════════════════════════════════════════════════════════════════════════
# Tooltip fetch — location + staff
# ══════════════════════════════════════════════════════════════════════════════

def fetch_tooltip(event_id: str, mis_cookie: str) -> tuple[str, str]:
    """
    Fetch the hover tooltip for one event.
    Returns (location, staff) — either may be an empty string.
    """
    url = (
        f"https://{SCHOOL_HOST}/students/calendar-entry/tooltip/id/{event_id}"
        f"?_dc={int(time.time() * 1000)}"
    )
    # Tooltip is a plain GET — same session headers but no Content-Type/body
    headers = make_headers(mis_cookie)
    headers.pop("Content-Type", None)

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    html = resp.text

    location = re.search(r"<b>Location</b>:<span>(.*?)</span>", html)
    staff     = re.search(r"<b>Staff</b>:<span>(.*?)</span>", html)

    loc = location.group(1).strip().replace("Anthony Gell School: ", "") if location else ""
    stf = staff.group(1).strip() if staff else ""
    return loc, stf


# ══════════════════════════════════════════════════════════════════════════════
# Date range helpers
# ══════════════════════════════════════════════════════════════════════════════

def week_ranges(start: date, end: date):
    """Yield (week_start, week_end) Monday-based ranges covering [start, end]."""
    # Step back to Monday of the start week
    cursor = start - timedelta(days=start.weekday())
    while cursor <= end:
        week_end = cursor + timedelta(days=6)
        yield cursor, min(week_end, end)
        cursor += timedelta(days=7)


def in_range(date_str: str, start: date, end: date) -> bool:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return start <= d <= end


# ══════════════════════════════════════════════════════════════════════════════
# ICS helpers
# ══════════════════════════════════════════════════════════════════════════════

def to_ics_dt(dt_str: str) -> str:
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").strftime("%Y%m%dT%H%M%S")


def escape_ics(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
    )


def build_ics(events: list) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ArborTimetable//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for ev in events:
        desc_parts = []
        if ev.get("staff"):
            desc_parts.append(ev["staff"])
        desc_parts.append(ev["title"])

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}",
            f"DTSTART:{to_ics_dt(ev['start'])}",
            f"DTEND:{to_ics_dt(ev['end'])}",
            f"SUMMARY:{escape_ics(ev['display_name'])}",
        ]
        if ev.get("location"):
            lines.append(f"LOCATION:{escape_ics(ev['location'])}")
        if desc_parts:
            lines.append(f"DESCRIPTION:{escape_ics(chr(10).join(desc_parts))}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# User prompts
# ══════════════════════════════════════════════════════════════════════════════

def parse_date_input(prompt: str) -> date:
    while True:
        raw = input(prompt).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  Invalid format — please use YYYY-MM-DD.")


def prompt_name_mapping(titles: list) -> dict:
    """
    For each unique event title, ask for a display name or Enter to exclude.
    Returns {original_title: display_name} for included events only.
    """
    print(f"\n{len(titles)} unique event type(s) found.")
    print("Enter a display name for each, or press Enter to exclude it.\n")
    print("─" * 50)

    mapping = {}
    for i, title in enumerate(titles, 1):
        print(f"\n[{i}/{len(titles)}] {title}")
        custom = input("  Display name (Enter to exclude): ").strip()
        if custom:
            mapping[title] = custom
            print(f"  → Will appear as: '{custom}'")
        else:
            print("  → Excluded")

    included = len(mapping)
    excluded = len(titles) - included
    print(f"\n  {included} included, {excluded} excluded.")
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# Discovery pass — collect all titles without fetching tooltips
# ══════════════════════════════════════════════════════════════════════════════

def discover_titles(mis_cookie: str, start: date, end: date) -> set:
    """
    Fetch calendar pages across the date range and return all unique
    event titles found, without fetching any tooltip detail.
    """
    print(f"\nScanning for event types between {start} and {end}...")
    titles = set()
    weeks = list(week_ranges(start, end))

    for i, (wstart, wend) in enumerate(weeks, 1):
        print(f"  Week {i}/{len(weeks)}: {wstart} → {wend}", end="\r", flush=True)
        try:
            pages = fetch_calendar_pages(mis_cookie, wstart, wend)
        except requests.HTTPError as e:
            print(f"\n  Warning: HTTP {e.response.status_code} for week {wstart} — skipping.")
            continue

        for page in pages:
            for ev in parse_events_from_page(page):
                if in_range(ev["date"], start, end):
                    titles.add(ev["title"])

    print(f"\nFound {len(titles)} unique event type(s).")
    return titles


# ══════════════════════════════════════════════════════════════════════════════
# Main export pass
# ══════════════════════════════════════════════════════════════════════════════

def export_events(mis_cookie: str, start: date, end: date, name_mapping: dict) -> list:
    """
    Walk the date range week by week.
    For each event whose title is in name_mapping, fetch its tooltip for
    location/staff, then build the final event record.
    Skips tooltip fetch entirely for excluded titles.
    """
    included_titles = set(name_mapping.keys())
    weeks = list(week_ranges(start, end))
    all_events = []
    seen_ids = set()
    tooltip_count = 0

    print(f"\nFetching event details for {len(weeks)} week(s)...")

    for i, (wstart, wend) in enumerate(weeks, 1):
        print(f"  Week {i}/{len(weeks)}: {wstart} → {wend}", end="\r", flush=True)
        try:
            pages = fetch_calendar_pages(mis_cookie, wstart, wend)
        except requests.HTTPError as e:
            print(f"\n  Warning: HTTP {e.response.status_code} for week {wstart} — skipping.")
            continue

        for page in pages:
            for ev in parse_events_from_page(page):
                if not in_range(ev["date"], start, end):
                    continue
                if ev["title"] not in included_titles:
                    continue
                if ev["id"] in seen_ids:
                    continue
                seen_ids.add(ev["id"])

                # Fetch tooltip for location + staff
                try:
                    location, staff = fetch_tooltip(ev["id"], mis_cookie)
                    tooltip_count += 1
                    time.sleep(TOOLTIP_DELAY)
                except requests.HTTPError:
                    location, staff = "", ""

                all_events.append({
                    **ev,
                    "display_name": name_mapping[ev["title"]],
                    "location": location,
                    "staff": staff,
                })

    print(f"\n  {len(all_events)} events collected ({tooltip_count} tooltip(s) fetched).")
    return sorted(all_events, key=lambda x: x["start"])


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("\nArbor Timetable → ICS Exporter")
    print("─" * 40)

    username = input("Username / email: ").strip()
    password = getpass.getpass("Password: ")

    print("\nLogging in...")
    try:
        mis_cookie = login(username, password)
    except requests.HTTPError as e:
        print(f"HTTP {e.response.status_code} error during login.")
        return
    except ValueError as e:
        print(f"Login error: {e}")
        return
    print("Logged in successfully.")

    start = parse_date_input("\nStart date (YYYY-MM-DD): ")
    end   = parse_date_input("End date   (YYYY-MM-DD): ")
    if end < start:
        print("End date must be on or after start date.")
        return

    # ── Step 1: discover all event titles in range (no tooltips yet) ──────────
    try:
        titles = discover_titles(mis_cookie, start, end)
    except Exception as e:
        print(f"\nError scanning calendar: {e}")
        return

    if not titles:
        print("No events found in that date range.")
        return

    # ── Step 2: user names / excludes each title ──────────────────────────────
    name_mapping = prompt_name_mapping(sorted(titles))
    if not name_mapping:
        print("All events excluded — nothing to export.")
        return

    # ── Step 3: fetch event details only for included titles ──────────────────
    try:
        events = export_events(mis_cookie, start, end, name_mapping)
    except Exception as e:
        print(f"\nError fetching events: {e}")
        return

    if not events:
        print("No events to export after filtering.")
        return

    # ── Step 4: write ICS ─────────────────────────────────────────────────────
    ics_content = build_ics(events)
    output_file = f"timetable_{start.isoformat()}_to_{end.isoformat()}.ics"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(ics_content)

    print(f"\nSaved {len(events)} event(s) to: {output_file}")
    print("Import into Google Calendar, Apple Calendar, Outlook, etc.")


if __name__ == "__main__":
    main()
