# Engineering notes

A working log: decisions I made and why, things that bit me, and things I
got wrong and had to correct. Kept because by the time I come back to this
in two months I won't remember why any of it is the way it is.

---

## Why line status and not arrivals

TfL's `Line/{ids}/Status` returns a severity code and a human-readable
reason for every line in one request. The alternative — deriving actual
delay from the Arrivals endpoint's `timeToStation` predictions — means
matching predicted against realised arrivals per train, which is a lot
more moving parts for an accuracy gain I can't justify yet. If the status
labels turn out too coarse, arrivals-based features are the obvious v2.

## Why I poll by mode instead of a line list

`Line/Mode/tube/Status` returns every current tube line in one request —
the same cost as naming four lines explicitly. I started scoped to the
four lines I actually commute on, then widened once it was clear the extra
lines were free. Adding Overground/DLR/Elizabeth line later is a parameter
change, not a code change.

## Why I don't stop polling overnight

I considered only polling during service hours to avoid logging the
nightly closure. Two reasons not to: it saves nothing meaningful, and
Night Tube (Central, Jubilee, Northern, Piccadilly, Victoria run 24h on
Friday and Saturday) makes any fixed schedule wrong on some nights. The
poll already tells me when a line is closed — severity 20 — so I filter
that at feature-engineering time instead of trying to predict it from an
external timetable.

## Why DynamoDB rather than Timestream

Timestream is the more natural fit for time series, but it has no
permanent free tier — only trial credits. DynamoDB does, and at this
volume (~700 writes/day) I'm far below the threshold either way.

## Why I store every status entry instead of picking the worst one

TfL's severity codes run 0–20 and are *categories, not a ranking*. 10 =
Good Service sits in the middle; 20 = Service Closed is a separate
category, not "worse than 9". My first version picked the "worst" status
with `min(statuses, key=severity)`, which silently discarded the higher-
numbered entry whenever a line reported two at once — so a genuine
closure could lose to a numerically smaller "Severe Delays" reading.

I now store every entry the line reports and add an unambiguous
`is_good_service` flag (true only if every entry is severity 10). No
ranking involved, so it can't be wrong the way picking a winner can.

---

## The historical dataset, and how I got it

I filed a Freedom of Information request with TfL for historical delay
data. They refused it under **Section 21** — information already
reasonably accessible by other means — and the refusal notice named the
dataset I should be using instead.

That turned out better than a grant would have been. TfL publish a
service status message log: **101,719 distinct events, April 2022 to
August 2026**, one row per status change per line, with start/end
timestamps and a free-text reason. Far richer than what I'd asked for.

The lesson I took from it: "I searched and found nothing" is much weaker
evidence than it feels like at the time. I had concluded no usable
historical data existed — see the correction below — and I was wrong. A
Section 21 refusal is itself a discovery mechanism, because the refusal
is obliged to name the source.

### Three things in that data that would have corrupted the model

**Timestamps are Europe/London local, not UTC.** I verified this rather
than assuming it: the nightly closure window sits at 01:16–04:15 in both
BST and GMT months, which is only possible on a local clock. My live
poller writes UTC. Merging the two without converting would have shifted
every summer row by an hour and quietly wrecked the hour-of-day feature,
which is the one the whole thing depends on.

**The log records status *changes* only.** "Good Service" appears 18
times in 101,719 rows — not because the network is always broken, but
because it's the implicit default between events. A model needs a
continuous timeline, so the build materialises every hour in range and
fills the gaps.

**Concurrent events on one line are real** — a part closure on one branch
while another has delays. Summing them gives more than 60 disrupted
minutes in a 60-minute hour. The build takes the union of overlapping
intervals instead, and asserts the result.

### Known limitation: Circle isn't separable

TfL report Circle and Hammersmith & City jointly as `C&H` in this feed,
so the history has no Circle-only signal. I left it combined rather than
invent a split — fabricating attribution to make the schema tidier would
be worse than the limitation. Victoria, Northern and Piccadilly are
clean. The live poller does separate them, so resolution improves going
forward, not backward.

---

## Things I got wrong

**"No public historical dataset exists at useful granularity."** I
checked TfL's performance reports (4-week aggregates) and third-party
dashboards (no export), concluded nothing usable existed, and planned to
spend 4–6 weeks accumulating my own data instead. I'd missed the
published status-message log entirely. Only surfaced via the FOI refusal.

**`severe_min` was contaminated with planned engineering works.** An
event with status "Severe Delays" whose text mentioned planned works was
correctly excluded from `disrupted_min` but still counted in
`severe_min` — so severity wasn't a subset of disruption, which is
impossible by definition. 892 rows affected. Found because a reviewer
claimed my interval aggregation summed instead of unioned; that claim was
wrong (every column caps correctly at 60), but checking it turned up this
instead. The build now asserts the invariant and exits non-zero if it
breaks.

**13,469 byte-identical duplicate rows in TfL's published file** — 11.7%
of it. Union aggregation had already absorbed them for the minute counts,
but they'd corrupt any per-event statistic. De-duplicated at load.

---

## AWS setup notes

Things that cost me time, recorded so they don't again:

- `Line/Status/{ids}` is not a real endpoint. It's `Line/{ids}/Status`, or
  `Line/Mode/{modes}/Status`. Getting the order wrong returns a confusing
  "not valid for Int32" error, because it matches a different route.
- TfL returns **HTTP 429 with "Invalid app_key is provided"** for a bad
  key. 429 normally means rate-limited; here it's an auth error.
- **`PowerUserAccess` cannot create IAM roles.** SAM needs `iam:CreateRole`
  to create the Lambda's execution role, so the deploying user needs
  `IAMFullAccess` alongside it. Note the deployed function's own role stays
  narrow — scoped to writes on one table.
- **A CloudFormation rollback can itself fail** if the deploying user lacks
  delete permissions on what it's rolling back. Leaves the stack in
  `ROLLBACK_FAILED` needing a manual delete before a clean redeploy.
- `NoEcho` parameters show a blank prompt in `sam deploy --guided`. That's
  masking, not a broken terminal. `--parameter-overrides` is visible and
  easier to get right.

## Monitoring, and why three alarms

The historical dataset is re-derivable — I can download it again. The live
record isn't: TfL's feed can't be queried backwards at two-minute
resolution, so any collection gap is permanent.

I ran it unmonitored for the first three weeks, which in hindsight was the
riskiest thing in the project — a hobby-grade poller against a third-party
API, with an irreplaceable output and nothing watching it.

Three alarms rather than one, because the failure modes don't overlap:

1. **Errors** — running but failing. Threshold is 10/hour, not 1: losing the
   odd poll to a transient 5xx is harmless at this resolution, and an alarm
   that cries wolf gets muted.
2. **Invocations missing** — not running at all. This is the important one,
   and the one that's easy to leave out: an errors alarm can never fire for a
   function that isn't running. `TreatMissingData: breaching` makes the
   absence of data the signal.
3. **Zero rows written** — running, succeeding, writing nothing. Invisible to
   both of the above. Fed by an EMF metric printed from the handler, so it
   costs nothing.

**A mistake I made setting these up:** my first version of the "has it
stopped" alarm asked *"were there at least 20 invocations in the last
hour?"*. It emailed me twice an hour, forever. CloudWatch evaluates the
**current, partial** period — so an hourly window with a count threshold of
20 is always below it at the top of the hour, then recovers mid-hour. A
long period plus a high count threshold is a flapping alarm by
construction.

The fix is to ask a question a partial window can still answer: *"were
there **any** invocations in the last 15 minutes?"*, requiring 2 dead
windows out of 3 before alarming. Threshold 1 can't be partially
satisfied — a live poller lands one within two minutes — so it only fires
when collection has genuinely stopped, and still detects it in ~30 min.

One deliberate omission: I didn't put a `AWS::Logs::LogGroup` in the template
to set log retention. The group already exists (Lambda created it on first
invocation), and CloudFormation would fail trying to create a resource that's
already there. Set it with the CLI instead:

```bash
aws logs put-retention-policy \
  --log-group-name /aws/lambda/tfl-status-poller \
  --retention-in-days 14
```

## Current state

Ingestion running since 3 September 2026. Historical dataset built and
validated. Modelling in progress — see README for what's been established
so far and what hasn't.

---

## The horizon study (28 Sept 2026)

### What I asked

Not "can I predict disruption" but "how far ahead can I predict it, and does
a model beat just knowing the historical rate for this line at this hour".
The second half is the part most projects skip, and it is the part that
decides whether any of this was worth building.

### Target

`major_disruption` — at least 15 minutes of unplanned **Severe Delays /
Part Suspended / Suspended** in the hour. 8.2% of service hours.

Deliberately not "any disruption": that fires on 19% of hours and is
dominated by Minor Delays, which is not a reason to leave the house early.
Deliberately not duration-thresholded either — I checked, and the median
disrupted hour is *entirely* disrupted, so cutting on minutes barely moves
the base rate. Severity is the axis that does the work, not duration.

Planned engineering works are excluded: they're published weeks ahead, so
predicting them is trivial and proves nothing.

### The off-by-one that nearly ruined it

My first run reported a "1 hour ahead" PR-AUC of 0.83. It was wrong — not
leaking in the obvious sense, but mislabelled.

To forecast the hour starting 08:00 with one hour of warning, I issue the
prediction at 07:00. At that moment the most recently *completed* hour is
06:00–07:00 — so the newest usable lag is `shift(2)`, not `shift(1)`.
`shift(1)` means reading the outcome of 07:00–08:00, which hasn't finished
yet when the forecast is issued.

Using `shift(h)` gave every horizon one extra hour of information it
wouldn't have. At the short end that is enormous, because disruption comes
in runs and the immediately preceding hour is by far the strongest single
predictor. The fix is one character; finding it was the whole difference
between a real result and a flattering one.

`lead = 0` is now kept deliberately as a reference point — the forecast
issued at the instant the hour begins. That's the "is the line broken right
now" question the TfL app already answers, and it's the ceiling every real
forecast gets measured against.

### Result

PR-AUC on the held-out test period (May–Aug 2026), service hours only:

| lead | model | lookup table | uplift | significant? |
|---|---|---|---|---|
| 0h (nowcast) | 0.827 | 0.148 | +0.675 | yes |
| 1h | 0.625 | 0.148 | +0.472 | yes |
| 2h | 0.496 | 0.148 | +0.343 | yes |
| 3h | 0.408 | 0.148 | +0.256 | yes |
| 6h | 0.275 | 0.148 | +0.126 | yes |
| 12h | 0.197 | 0.148 | +0.051 | yes |
| **24h** | **0.164** | **0.149** | **+0.016** | **no — CI [-0.001, +0.033]** |

Skill roughly halves every two hours of lead, and by a day ahead the model
is statistically indistinguishable from a lookup table of historical rates.

The mechanism is visible in the calendar-only model, which sits flat at
~0.161 at every horizon: at lead 0 the recency features carry almost
everything (0.827 vs 0.161), and by 24h they carry nothing — the full model
is *worse* than calendar-only on ROC and on recall-at-budget. A day ahead,
recency isn't just uninformative, it's noise the model overfits.

### The product answer

Weekday 07:00–09:00, my four lines, firing on ~10% of mornings:

| lead | precision | base rate |
|---|---|---|
| 0h | 0.80 | 0.107 |
| 3h | 0.28 | 0.107 |
| 12h | 0.10 | 0.107 |
| 24h | 0.05 | 0.107 |

A day-ahead alert is **worse than firing at random**. So the morning alert
is cancelled — not because I ran out of time, but because I measured it and
it doesn't work.

What does work is a nowcast, and I should be honest about what that is: at
zero lead time it's answering the same question TfL's own status API
answers. Its contribution over a two-line persistence rule is real but
modest. Calling it a forecast would be overselling it.

### Statistics notes

- **Bootstrap resamples whole days, not rows.** Disruption clusters — one
  signal failure spans several hours, and weather or strikes correlate
  across lines. Row-level resampling treats those as independent and gives
  confidence intervals several times too narrow.
- **Service hours only.** ~16% of the grid is the overnight closure where
  major disruption is near-impossible; leaving it in inflates every
  discrimination metric for free.
- **The baseline is shrunk as well as raw.** An empirical-Bayes version
  pulls sparse cells toward the global rate, so a cell with three
  observations can't claim a rate of 1.0. It makes the bar harder to clear,
  which is the point of a baseline.
- **Base rate drifts**: 7–8% in train, 11.1% in test. Part of that is real,
  part is TfL emitting more, shorter status messages over time. It's why
  calibration isn't the headline metric here — a calibration curve on this
  split measures the drift more than the model.
