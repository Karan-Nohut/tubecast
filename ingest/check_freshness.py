#!/usr/bin/env python3
"""
Belt-and-braces check on the collection pipeline. Run it manually, or on a
weekly reminder.

The CloudWatch alarms in template.yaml are the real safety net; this exists
because a first-time alarm setup can itself be misconfigured, and the data
being collected cannot be backfilled. Running this once a week bounds the
worst case to seven days of loss even if every alarm is silently wrong.

Usage:
    python3 check_freshness.py              # freshness only
    python3 check_freshness.py --gaps 7     # also scan 7 days for holes

Exits non-zero if anything looks wrong, so it can be wired into a cron job.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

TABLE = "tfl-line-status-history"
POLL_MINUTES = 2
STALE_AFTER_MINUTES = 15          # ~7 missed polls
GAP_THRESHOLD_MINUTES = 30        # a hole worth knowing about


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", type=int, metavar="DAYS",
                    help="also scan this many days back for gaps in coverage")
    ap.add_argument("--region", default="eu-west-2")
    args = ap.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(TABLE)
    now = datetime.now(timezone.utc)
    problems = []

    # Discover which lines exist by looking at recent partitions. There is no
    # cheap "list partition keys" in DynamoDB, so this is seeded from the
    # tube lines the poller collects.
    lines = ["bakerloo", "central", "circle", "district", "hammersmith-city",
             "jubilee", "metropolitan", "northern", "piccadilly", "victoria",
             "waterloo-city"]

    print(f"checked at {now.isoformat(timespec='seconds')}\n")
    print(f"{'line':<20} {'last written':<22} {'age'}")
    print("-" * 56)

    for line_id in lines:
        resp = table.query(
            KeyConditionExpression=Key("line_id").eq(line_id),
            ScanIndexForward=False,   # newest first
            Limit=1,
        )
        items = resp.get("Items", [])
        if not items:
            print(f"{line_id:<20} {'NO DATA':<22} -")
            problems.append(f"{line_id}: no rows at all")
            continue

        last = datetime.fromisoformat(items[0]["timestamp"])
        age = (now - last).total_seconds() / 60
        flag = "  <-- STALE" if age > STALE_AFTER_MINUTES else ""
        print(f"{line_id:<20} {items[0]['timestamp']:<22} {age:>5.0f} min{flag}")
        if age > STALE_AFTER_MINUTES:
            problems.append(f"{line_id}: last write {age:.0f} min ago")

    if args.gaps:
        print(f"\nscanning {args.gaps} days for gaps > {GAP_THRESHOLD_MINUTES} min...")
        since = (now - timedelta(days=args.gaps)).isoformat(timespec="seconds")
        for line_id in lines:
            stamps = []
            kwargs = dict(
                KeyConditionExpression=Key("line_id").eq(line_id)
                & Key("timestamp").gte(since),
                ProjectionExpression="#t",
                ExpressionAttributeNames={"#t": "timestamp"},
            )
            while True:
                resp = table.query(**kwargs)
                stamps.extend(datetime.fromisoformat(i["timestamp"])
                              for i in resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

            stamps.sort()
            holes = [
                (a, b, (b - a).total_seconds() / 60)
                for a, b in zip(stamps, stamps[1:])
                if (b - a).total_seconds() / 60 > GAP_THRESHOLD_MINUTES
            ]
            if holes:
                worst = max(h[2] for h in holes)
                print(f"  {line_id}: {len(holes)} gap(s), largest {worst:.0f} min")
                problems.append(f"{line_id}: {len(holes)} gaps in last {args.gaps}d")
            else:
                print(f"  {line_id}: clean ({len(stamps)} rows)")

    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("all clear")


if __name__ == "__main__":
    main()
