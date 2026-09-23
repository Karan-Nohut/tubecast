"""
Scheduled Lambda: polls TfL's Line Status endpoint, by mode, and appends
one row per line per poll to DynamoDB. This is the data collection step --
it produces the forward record that model predictions get scored against,
which is the one dataset here that cannot be rebuilt after the fact: TfL's
live feed is not historically queryable at this resolution.

Why Line Status rather than computing delay from Arrivals predictions:
Line Status gives TfL's own severity code (0-20 categories, 10 = Good
Service -- not an ordinal scale, see the comment in handler()) plus
a human-readable reason, updated close to real time, in a single request
for every line of a given mode. Computing "actual" delay from Arrivals
timeToStation data requires matching predicted vs realised arrivals per
train -- a lot more moving parts for a marginal accuracy gain at this
stage. If v1 turns out too coarse, Arrivals-based features are the
natural v2 addition.

Why query by mode rather than a hardcoded line list: /Line/Mode/{modes}/Status
returns every current line for the given mode(s) in one call, at the same
cost as querying a handful of named lines -- so collection isn't scoped
down to just the lines used personally, keeping the option open to widen
or narrow the evaluation later without having missed weeks of data for
lines that weren't on an original hardcoded list. MODES defaults to
"tube" (all 11 Underground lines); adding "dlr", "overground" or
"elizabeth-line" later is a config change (redeploy with a new MODES
value), not a code change.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
APP_KEY = os.environ.get("TFL_APP_KEY", "")
MODES = os.environ.get("MODES", "tube")

TFL_STATUS_URL = "https://api.tfl.gov.uk/Line/Mode/{modes}/Status"
METRIC_NAMESPACE = "TubeCast"

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)


def emit_metric(name: str, value, unit: str = "Count") -> None:
    """Publish a CloudWatch metric by printing it in Embedded Metric Format.

    EMF means CloudWatch extracts the metric straight from the log line --
    no PutMetricData call, so no extra latency, no extra IAM permission and
    no per-call cost. The `LinesWritten` metric exists to catch the one
    failure the Errors and Invocations alarms both miss: the function runs,
    succeeds, and writes nothing (e.g. TfL answers 200 with an empty body).
    """
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
    }))


def fetch_line_status() -> list:
    url = TFL_STATUS_URL.format(modes=MODES)
    if APP_KEY:
        url += "?" + urllib.parse.urlencode({"app_key": APP_KEY})

    req = urllib.request.Request(url, headers={"User-Agent": "tubecast/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A 429/400 here usually means a bad/missing app_key or a malformed
        # request -- surface it loudly rather than silently dropping data
        # points, since gaps quietly corrupt the training set. Raising also
        # lets the Errors alarm see it; swallowing would hide the failure.
        print(f"HTTP {exc.code} from TfL: {exc.read().decode('utf-8', 'ignore')}")
        raise


def handler(event, context):
    now = datetime.now(timezone.utc)
    ts_iso = now.isoformat(timespec="seconds")

    lines_data = fetch_line_status()

    written = 0
    with _table.batch_writer() as batch:
        for line in lines_data:
            line_id = line.get("id")
            raw_statuses = line.get("lineStatuses", [])

            # Keep every status entry a line reports, rather than picking
            # a single "worst" one. TfL's severity codes (0-20) are
            # categories, not a best-to-worst ranking -- e.g. 6 (Severe
            # Delays) and 20 (Service Closed) aren't comparable by
            # "smaller number wins", so collapsing to one entry via
            # min()/max() silently discards real information whenever a
            # line reports more than one simultaneous status (e.g. part
            # closure + delays elsewhere). Deciding what "bad" means for
            # modelling happens later, at feature-engineering time, with
            # the full picture available.
            statuses = [
                {
                    "severity": s.get("statusSeverity", 10),
                    "description": s.get("statusSeverityDescription", "Unknown"),
                    "reason": s.get("reason", ""),
                }
                for s in raw_statuses
            ] or [{"severity": 10, "description": "Unknown", "reason": ""}]

            # Unambiguous convenience flag for quick queries: True only if
            # EVERY reported status is Good Service (10). Any single
            # non-10 entry, or multiple simultaneous entries, counts as
            # not-good-service -- no ranking involved, so this can't be
            # wrong the way picking "the worst" by number can be.
            is_good_service = all(s["severity"] == 10 for s in statuses)

            item = {
                "line_id": line_id,
                "timestamp": ts_iso,
                "statuses": statuses,
                "is_good_service": is_good_service,
                "day_of_week": now.strftime("%A"),
                "hour": now.hour,
                "minute": now.minute,
                "is_weekend": now.weekday() >= 5,
            }
            batch.put_item(Item=item)
            written += 1
            summary = ", ".join(f"{s['severity']}={s['description']}" for s in statuses)
            print(f"{line_id}: {summary}")

    # Emitted unconditionally, including zero -- a zero is the signal the
    # empty-writes alarm exists to catch. If the fetch raised above we never
    # reach here, and the metric's absence trips the same alarm via
    # TreatMissingData: breaching.
    emit_metric("LinesWritten", written)

    return {"statusCode": 200, "linesLogged": written}
