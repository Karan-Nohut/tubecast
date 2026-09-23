"""
Turn TfL's published service-status-message log into a model-ready dataset.

Input : data/raw-service-status-messages.csv
        TfL's published disruption log -- one row per status-CHANGE event,
        per line, with London-local start/end timestamps and free text.

Output: data/events_clean.csv   1:1 cleaned events (UTC, severity codes, flags)
        data/hourly.csv         reconstructed continuous hourly grid

Why a reconstruction step is needed at all: the source only records events
when status CHANGES, so "Good Service" is implicit -- it appears 18 times in
115k rows because it is the default state between events, not a logged one.
A model needs a continuous timeline ("at hour H, was line L disrupted?"), so
every hour in range is materialised and gaps are filled as Good Service.

TIMEZONE (the subtle one): source timestamps are Europe/London LOCAL. Verified
empirically -- the nightly closure window sits at 01:16-04:15 in both BST and
GMT months, which is only possible on a local clock. TubeCast's live DynamoDB
rows are UTC. Everything here is converted to UTC so the two sources can be
joined without a silent one-hour summer skew in hour-of-day features.
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

RAW = "data/raw-service-status-messages.csv"
OUT_EVENTS = "data/events_clean.csv"
OUT_HOURLY = "data/hourly.csv"

# TfL's own severity codes, from the Line/Meta/Severity endpoint. NOTE these
# are CATEGORY codes, not a best-to-worst ranking (10 = Good Service sits in
# the middle; 20 = Service Closed is a separate category, not "worse than 9").
# Kept here so this dataset lines up with the codes TubeCast's live poller
# stores, rather than inventing a second scheme.
STATUS_TO_SEVERITY = {
    "Special Service": 0,
    "Closed": 1,
    "Suspended": 2,
    "Part Suspended": 3,
    "Planned Closure": 4,
    "Part Closure": 5,
    "Severe Delays": 6,
    "Reduced Service": 7,
    "Bus Service": 8,
    "Minor Delays": 9,
    "Good Service": 10,
    "Part Closed": 11,
    "Exit Only": 12,
    "No Step Free Access": 13,
    "Change of frequency": 14,
    "Diverted": 15,
    "Not Running": 16,
    "Issues Reported": 17,
    "No Issues": 18,
    "Information": 19,
    "Service Closed": 20,
}

# Source line names -> TfL API line ids, so this joins to TubeCast's live data.
# C&H is deliberately NOT split: TfL reports Circle and Hammersmith & City
# jointly in this feed, and inventing a split would fabricate attribution.
LINE_TO_ID = {
    "Bakerloo": "bakerloo",
    "Central": "central",
    "C&H": "c-and-h",           # Circle + Hammersmith & City, combined at source
    "District": "district",
    "Jubilee": "jubilee",
    "Metropolitan": "metropolitan",
    "Northern": "northern",
    "Piccadilly": "piccadilly",
    "Victoria": "victoria",
    "Waterloo & City": "waterloo-city",
}

# Scheduled overnight closure -- not a disruption, and trivially predictable.
CLOSED_STATUSES = {"Service Closed", "Closed"}

# Everything else non-good counts as disruption, UNLESS flagged planned below.
PLANNED_STATUSES = {"Planned Closure"}
PLANNED_TEXT = ("planned engineering work", "planned closure", "planned work")


def parse_london(s):
    """Parse 'DD/MM/YYYY HH:MM:SS' as Europe/London local -> aware UTC datetime.

    Explicit format string on purpose: a guessing parser silently swaps day
    and month for the first 12 days of every month.
    """
    try:
        naive = datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    # fold=0 resolves the repeated hour at the autumn DST switch to the first
    # (BST) occurrence. Both occurrences fall inside the nightly closure window,
    # so the choice cannot affect daytime disruption features.
    return naive.replace(tzinfo=LONDON, fold=0).astimezone(UTC)


def is_planned(status, detail):
    if status in PLANNED_STATUSES:
        return True
    d = (detail or "").lower()
    return any(p in d for p in PLANNED_TEXT)


def load_events():
    events, skipped = [], defaultdict(int)
    seen = set()
    with open(RAW, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            # TfL's published file contains 13,469 byte-identical duplicate
            # rows. One event reported twice is still one event. Union
            # aggregation already absorbed these for the minute counts, but
            # they would corrupt any per-event statistic (counts, durations).
            key = tuple(row.get(c) for c in
                        ("Line", "Start Date/Time", "End Date/Time",
                         "Service Status", "Status Message Details"))
            if key in seen:
                skipped["exact_duplicate"] += 1
                continue
            seen.add(key)

            name = (row.get("Line") or "").strip()
            line_id = LINE_TO_ID.get(name)
            if not line_id:
                skipped["unknown_line"] += 1
                continue

            start = parse_london(row.get("Start Date/Time"))
            end = parse_london(row.get("End Date/Time"))
            if start is None or end is None:
                skipped["unparseable_time"] += 1
                continue
            if end < start:
                skipped["end_before_start"] += 1
                continue

            status = (row.get("Service Status") or "").strip()
            detail = (row.get("Status Message Details") or "").strip()

            events.append({
                "line_id": line_id,
                "start_utc": start,
                "end_utc": end,
                "duration_min": round((end - start).total_seconds() / 60, 2),
                "status": status,
                "severity": STATUS_TO_SEVERITY.get(status, ""),
                "planned": is_planned(status, detail),
                "closed": status in CLOSED_STATUSES,
                "detail": detail,
            })
    return events, skipped


def write_events(events):
    with open(OUT_EVENTS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["line_id", "start_utc", "end_utc", "duration_min",
                    "status", "severity", "planned", "closed", "detail"])
        for e in events:
            w.writerow([
                e["line_id"],
                e["start_utc"].isoformat(),
                e["end_utc"].isoformat(),
                e["duration_min"],
                e["status"],
                e["severity"],
                int(e["planned"]),
                int(e["closed"]),
                e["detail"],
            ])


def overlap_intervals(events):
    """Bucket each event's time span into the UTC hours it covers.

    Returns {(line_id, hour_start_utc): {kind: [(sec_from, sec_to), ...]}}
    Intervals are kept rather than summed because concurrent events on one
    line are real (a part closure on one branch plus delays on another), and
    naive summing would report more than 60 disrupted minutes in an hour.
    """
    buckets = defaultdict(lambda: defaultdict(list))
    for e in events:
        if e["closed"]:
            kind = "closed"
        elif e["planned"]:
            kind = "planned"
        else:
            kind = "disrupted"

        cur = e["start_utc"].replace(minute=0, second=0, microsecond=0)
        while cur < e["end_utc"]:
            nxt = cur + timedelta(hours=1)
            lo = max(e["start_utc"], cur)
            hi = min(e["end_utc"], nxt)
            if hi > lo:
                a = int((lo - cur).total_seconds())
                b = int((hi - cur).total_seconds())
                buckets[(e["line_id"], cur)][kind].append((a, b))
                # Severity sub-buckets are recorded ONLY for events that count
                # as unplanned disruption. Recording them unconditionally let a
                # "Severe Delays" event whose text mentions planned engineering
                # work land in `planned` yet still inflate severe_min -- which
                # broke the invariant that severe/minor are subsets of
                # disrupted, and contaminated the severity target with
                # pre-announced works. Asserted in validate_outputs().
                if kind == "disrupted":
                    if e["status"] == "Severe Delays":
                        buckets[(e["line_id"], cur)]["severe"].append((a, b))
                    elif e["status"] == "Minor Delays":
                        buckets[(e["line_id"], cur)]["minor"].append((a, b))
            cur = nxt
    return buckets


def union_seconds(intervals):
    """Total seconds covered by a set of possibly-overlapping intervals."""
    if not intervals:
        return 0
    merged, (cs, ce) = [], sorted(intervals)[0]
    for s, e in sorted(intervals)[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            merged.append((cs, ce))
            cs, ce = s, e
    merged.append((cs, ce))
    return sum(e - s for s, e in merged)


def write_hourly(events, buckets):
    start = min(e["start_utc"] for e in events).replace(minute=0, second=0, microsecond=0)
    end = max(e["end_utc"] for e in events).replace(minute=0, second=0, microsecond=0)
    line_ids = sorted(LINE_TO_ID.values())

    rows = 0
    with open(OUT_HOURLY, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "line_id", "hour_utc", "date", "hour", "day_of_week", "is_weekend",
            "disrupted_min", "severe_min", "minor_min", "planned_min", "closed_min",
            "any_disruption",
        ])
        cur = start
        while cur <= end:
            dow = cur.strftime("%A")
            weekend = int(cur.weekday() >= 5)
            for lid in line_ids:
                b = buckets.get((lid, cur), {})
                dis = union_seconds(b.get("disrupted", [])) / 60
                sev = union_seconds(b.get("severe", [])) / 60
                mnr = union_seconds(b.get("minor", [])) / 60
                pln = union_seconds(b.get("planned", [])) / 60
                cld = union_seconds(b.get("closed", [])) / 60
                w.writerow([
                    lid, cur.isoformat(), cur.date().isoformat(), cur.hour,
                    dow, weekend,
                    round(dis, 2), round(sev, 2), round(mnr, 2),
                    round(pln, 2), round(cld, 2),
                    int(dis > 0),
                ])
                rows += 1
            cur += timedelta(hours=1)
    return rows, start, end


def validate_outputs():
    """Assert the invariants that must hold, so a regression cannot pass silently."""
    import csv as _csv
    problems = defaultdict(int)
    with open(OUT_HOURLY, newline="") as f:
        for r in _csv.DictReader(f):
            dis = float(r["disrupted_min"]); sev = float(r["severe_min"])
            mnr = float(r["minor_min"]); pln = float(r["planned_min"])
            cld = float(r["closed_min"])
            for name, v in (("disrupted", dis), ("severe", sev), ("minor", mnr),
                            ("planned", pln), ("closed", cld)):
                if v > 60.01:
                    problems[f"{name}>60"] += 1
            # severe and minor are unplanned-disruption subsets by construction
            if sev > dis + 0.01:
                problems["severe>disrupted"] += 1
            if mnr > dis + 0.01:
                problems["minor>disrupted"] += 1
            if (dis > 0) != (int(r["any_disruption"]) == 1):
                problems["flag_mismatch"] += 1
    return problems


def main():
    events, skipped = load_events()
    if not events:
        sys.exit("No usable events parsed -- check the input file.")

    write_events(events)
    buckets = overlap_intervals(events)
    rows, start, end = write_hourly(events, buckets)

    print(f"events kept      : {len(events):,}")
    for k, v in skipped.items():
        print(f"  skipped ({k}): {v:,}")
    print(f"range (UTC)      : {start.isoformat()} -> {end.isoformat()}")
    print(f"hourly grid rows : {rows:,}")
    print(f"wrote            : {OUT_EVENTS}, {OUT_HOURLY}")

    problems = validate_outputs()
    if problems:
        print("VALIDATION FAILED:", dict(problems))
        sys.exit(1)
    print("validation       : all invariants hold")


if __name__ == "__main__":
    main()
