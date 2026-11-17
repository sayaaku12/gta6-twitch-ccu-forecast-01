"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Entry for make.fun gta6-twitch-ccu-forecast-01. Python 3.13, standard
library only, offline, deterministic.

Idea
----
A launch day's hourly Twitch viewers, in log space, follow a shape that
depends on where hour 1 sits relative to the launch ramp. Games unlock at
midnight local time, region by region, so by 00:00 UTC the game has already
been live for hours in New Zealand and Australia, and the US unlocks later
in the day (05:00 / 08:00 UTC). How high hour 1 already is, compared with
the busiest hour of the previous day, tells us which shape the day takes:

    s = hour-1 viewers / max viewers in the 24 h before the release day

A low s means hour 1 is a night dip after an early peak, and the day then
climbs steeply into the evening (median peak about 10x hour 1). A high s
means hour 1 is already near the peak, and the day stays comparatively
flat (median peak about 1.7x hour 1).

The program fits this at run time from the case's own comparables (past
big launches, each with a full hourly series around its release day):
for every comparable it works out s and the 24 log-ratios
ln(viewers_h / viewers_1), splits the comparables into three groups by s,
and takes, per group and per hour, the median log-ratio and its spread.
It then picks the group whose centre s is closest (in log) to this case's
s and forecasts

    q_tau(h) = p1 * exp(median_h + z_tau * sigma_h)

where p1 is hour 1's print, z_tau the standard normal quantile at each
scored level, and sigma_h = max(0.35 * std_h, 0.20). The 0.35 shrink
and the 0.20 floor were chosen on the kit's four practice cases: the
cross-game spread overstates how far a single game's day wanders from its
group's median, but the floor keeps the band honest for a launch as big
and unprecedented as this one.

Fitting is done from the case file at run time, so the same program works
on the practice cases (38 comparables) and the final (42). If the case
carries too few usable comparables, baked-in group profiles (fitted
offline from the same 38 launches) are used instead.

Local practice score (pinball loss, log scale; lower is better):
house baseline 0.387, this entry 0.180 (per case p00 0.185, p01 0.197,
p02 0.151, p03 0.189; every case beats the baseline on its own).
"""

import json
import math
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
SPREAD_SHRINK = 0.35
SIGMA_FLOOR = 0.20
MIN_DAY_HOURS = 20
MIN_GROUP_GAMES = 6

# Baked-in group profiles, fitted offline from the kit's 38 comparables
# (median log-ratio to hour 1, and its standard deviation, per hour 1..24,
# for the low-, mid- and high-saturation groups). Used only when a case
# carries too few usable comparables to fit its own.
FALLBACK = [
    {
        "centre": 0.321,
        "median": [
            0.0, -0.19, -0.31, -0.307, -0.286, -0.257,
            -0.131, 0.048, 0.134, 0.127, 0.272, 0.455,
            0.815, 0.998, 1.228, 1.789, 1.852, 1.927,
            2.027, 2.285, 2.253, 2.119, 1.964, 1.755,
        ],
        "std": [
            0.0, 0.197, 0.455, 0.451, 0.566, 0.772,
            0.744, 0.747, 0.868, 1.006, 1.057, 1.176,
            1.354, 1.446, 1.605, 1.942, 2.125, 2.065,
            2.033, 2.025, 2.034, 2.021, 2.013, 1.996,
        ],
    },
    {
        "centre": 0.608,
        "median": [
            0.0, -0.163, -0.279, -0.394, -0.169, -0.113,
            -0.285, -0.324, -0.212, -0.181, -0.135, -0.061,
            0.058, 0.15, 0.285, 0.404, 0.519, 0.558,
            0.648, 0.694, 0.672, 0.702, 0.699, 0.557,
        ],
        "std": [
            0.0, 0.226, 0.282, 0.299, 0.281, 0.345,
            0.485, 0.65, 0.882, 1.333, 1.526, 1.552,
            1.538, 1.515, 1.484, 1.476, 1.464, 1.451,
            1.441, 1.431, 1.407, 1.428, 1.432, 1.433,
        ],
    },
    {
        "centre": 1.053,
        "median": [
            0.0, -0.088, -0.219, -0.288, -0.421, -0.263,
            -0.272, -0.203, -0.186, -0.243, -0.165, -0.076,
            -0.007, 0.042, 0.059, 0.107, 0.206, 0.3,
            0.224, 0.246, 0.374, 0.476, 0.522, 0.173,
        ],
        "std": [
            0.0, 0.409, 1.074, 1.158, 1.177, 1.154,
            1.144, 1.13, 1.287, 1.7, 1.799, 1.853,
            1.988, 2.281, 2.541, 2.523, 2.48, 2.502,
            2.441, 2.44, 2.479, 2.466, 2.425, 2.428,
        ],
    },
]


def parse_ts(text):
    """Parse an ISO-8601 UTC timestamp like 2026-11-19T00:00:00Z."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def comparable_shape(comp):
    """Return (s, ratios) for one comparable, or None if its release day
    is too sparse to use. ratios[h] is ln(viewers at hour h+1 / viewers
    at hour 1) for h = 0..23, None where that hour has no point."""
    day_start = parse_ts(comp["day_start"])
    by_time = {}
    for row in comp.get("hourly") or []:
        viewers = row.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        by_time[parse_ts(row["from"])] = viewers
    day = {}
    for hour in range(1, 25):
        value = by_time.get(day_start + timedelta(hours=hour - 1))
        if value is not None:
            day[hour] = value
    if 1 not in day:
        return None
    if len(day) < MIN_DAY_HOURS:
        return None
    before = [v for t, v in by_time.items()
              if day_start - timedelta(hours=24) <= t < day_start]
    if not before:
        return None
    first = day[1]
    saturation = first / max(before)
    ratios = []
    for hour in range(1, 25):
        value = day.get(hour)
        ratios.append(math.log(value / first) if value else None)
    return (saturation, comp.get("name", ""), ratios)


def group_profiles(features):
    """Split fitted comparables into three saturation groups and return,
    per group, (centre saturation, median log-ratio per hour, std per
    hour). Groups are ordered by saturation, low to high. An hour no
    game in a group has a point for falls back to that hour's median
    over all the fitted games, so no profile ever has a hole."""
    ordered = sorted(features, key=lambda f: (f[0], f[1]))
    count = len(ordered)
    third = count // 3
    groups = [
        ordered[:third],
        ordered[third:2 * third],
        ordered[2 * third:],
    ]
    overall_median = []
    overall_std = []
    for hour in range(24):
        values = sorted(r[hour] for _, _, r in ordered
                        if r[hour] is not None)
        overall_median.append(statistics.median(values))
        spread = statistics.pstdev(values) if len(values) > 1 else 0.3
        overall_std.append(spread)
    profiles = []
    for group in groups:
        centre = statistics.median([s for s, _, _ in group])
        median = []
        std = []
        for hour in range(24):
            values = sorted(r[hour] for _, _, r in group
                            if r[hour] is not None)
            if values:
                median.append(statistics.median(values))
                spread = statistics.pstdev(values) if len(values) > 1 else 0.3
                std.append(spread)
            else:
                median.append(overall_median[hour])
                std.append(overall_std[hour])
        profiles.append({"centre": centre, "median": median, "std": std})
    return profiles


def fit_profiles(case):
    """The group profiles this case forecasts from: fitted from the
    case's own comparables when enough of them have a usable release
    day, else the baked-in profiles."""
    features = []
    for comp in case.get("comparables") or []:
        shape = comparable_shape(comp)
        if shape is not None:
            features.append(shape)
    if len(features) >= MIN_GROUP_GAMES * 3:
        return group_profiles(features)
    return FALLBACK


def case_saturation(case):
    """This case's s: hour-1 print over the busiest hour of the day
    before the release day, from the case's own hourly history."""
    day_start = parse_ts(case["day_start"])
    before = []
    for row in case.get("history_hourly") or []:
        viewers = row.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        moment = parse_ts(row["from"])
        if day_start - timedelta(hours=24) <= moment < day_start:
            before.append(viewers)
    first = next((k["viewers"] for k in case.get("known") or []
                  if k.get("hour") == 1), None)
    if not before or not first or first <= 0:
        return None
    return first / max(before)


def pick_profile(profiles, saturation):
    """The group whose centre saturation is closest in log to the case's.
    With no saturation (no usable history), take the middle group."""
    if saturation is None or saturation <= 0:
        return profiles[1]
    target = math.log(saturation)
    return min(profiles,
               key=lambda p: abs(math.log(p["centre"]) - target))


def round2(value):
    """Round to 2 decimals, half up, as the house baseline does."""
    scaled = value * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    rounded = whole / 100
    return int(rounded) if rounded == int(rounded) else rounded


def hour_quantiles(p1, profile, hour):
    """The five forecast quantiles for one hour: the group's median
    log-ratio for that hour, spread by sigma around it."""
    median = profile["median"][hour - 1]
    spread = profile["std"][hour - 1]
    sigma = max(SPREAD_SHRINK * spread, SIGMA_FLOOR)
    quantiles = []
    for z in Z:
        value = p1 * math.exp(median + z * sigma)
        value = min(max(value, 1.0), 1_000_000_000.0)
        quantiles.append(round2(value))
    # Rounding can only keep the order (equal is allowed), but guard it.
    for i in range(1, len(quantiles)):
        if quantiles[i] < quantiles[i - 1]:
            quantiles[i] = quantiles[i - 1]
    return quantiles


def check_output(forecast, name):
    """The contest's shape rules, checked before anything is written:
    exactly one key, 23 rows for hours 2..24, five finite numbers per
    row, each above 0 and at most 1e9, non-decreasing."""
    if set(forecast) != {"hours"}:
        raise ValueError(f"{name} forecast has keys {set(forecast)}")
    rows = forecast["hours"]
    if len(rows) != 23:
        raise ValueError(f"{name} forecast has {len(rows)} rows, not 23")
    for index, row in enumerate(rows):
        if set(row) != {"hour", "q"} or row["hour"] != index + 2:
            raise ValueError(f"{name} row {index} is not hour {index + 2}")
        values = row["q"]
        if len(values) != 5:
            raise ValueError(f"{name} hour {row['hour']} has "
                             f"{len(values)} quantiles, not 5")
        for value in values:
            if not math.isfinite(value) or value <= 0 or value > 1e9:
                raise ValueError(f"{name} hour {row['hour']} has an "
                                 f"out-of-range quantile {value}")
        if any(values[i] < values[i - 1] for i in range(1, 5)):
            raise ValueError(f"{name} hour {row['hour']} quantiles "
                             "are not non-decreasing")


def forecast_case(case, name):
    if case.get("levels") != LEVELS:
        raise ValueError(f"{name} asks for levels {case.get('levels')}")
    first = next((k for k in case.get("known") or []
                  if k.get("hour") == 1), None)
    if first is None or first.get("viewers") is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1, first["viewers"])

    profile = pick_profile(fit_profiles(case), case_saturation(case))

    hours = []
    for row in case.get("forecast") or []:
        hour = row["hour"]
        hours.append({"hour": hour,
                      "q": hour_quantiles(p1, profile, hour)})
    hours.sort(key=lambda row: row["hour"])
    if len(hours) != 23 or hours[0]["hour"] != 2 or hours[-1]["hour"] != 24:
        raise ValueError(f"{name} does not ask for hours 2..24")
    forecast = {"hours": hours}
    check_output(forecast, name)
    return forecast


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / f"{path.stem}.out").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
