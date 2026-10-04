#!/usr/bin/env python3
"""Pull every Garmin Connect activity (plus GPS tracks) into /config/www/garmin/activities.json.

Runs inside the Home Assistant core container (command_line sensor in packages/activities.yaml):

    python3 /config/bin/garmin_activities.py sync      # fetch new activities + tracks, print summary
    python3 /config/bin/garmin_activities.py summary   # print summary from the cache only (fast)

Auth: reuses the DI bearer token the Garmin Connect (HACS) integration keeps in
/config/.storage/core.config_entries. The token is only read, never refreshed and never
printed - refreshing would rotate the refresh token and lock the integration out. When the
token is about to expire the run just serves the cache; the integration refreshes it on
its own schedule and the next run picks the new one up.

Output (stdout) is one JSON document for the sensor: state = activity count, attributes =
this week / month / year totals, 12 weekly buckets, last 8 activities, type counts.
The full list with tracks goes to activities.json for the map page (www/garmin/map.html).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

CONFIG_ENTRIES = "/config/.storage/core.config_entries"
OUT_DIR = "/config/www/garmin"
CACHE = os.path.join(OUT_DIR, "activities.json")
LOG = "/config/garmin_activities.log"

API = "https://connectapi.garmin.com"
LIST_URL = f"{API}/activitylist-service/activities/search/activities"
DETAILS_URL = f"{API}/activity-service/activity/{{id}}/details"

PAGE = 100            # activities per list page
MAX_PAGES = 30        # hard stop (3000 activities)
MAX_DETAILS = 40      # GPS tracks fetched per run (the rest come next run)
POLY_POINTS = 600     # server-side downsample of the track
TIMEOUT = 20

# Fields kept per activity (small: the file is downloaded by the browser every time the map opens).
KEEP = {
    "activityId": "id", "activityName": "name", "startTimeLocal": "start", "startTimeGMT": "start_gmt",
    "distance": "dist", "duration": "dur", "movingDuration": "moving", "averageSpeed": "speed",
    "maxSpeed": "max_speed", "averageHR": "hr", "maxHR": "max_hr", "calories": "cal",
    "elevationGain": "elev", "steps": "steps", "locationName": "loc", "startLatitude": "lat",
    "startLongitude": "lon", "hasPolyline": "has_poly", "aerobicTrainingEffect": "te",
    "averageRunningCadenceInStepsPerMinute": "cadence", "avgPower": "power",
    "trainingEffectLabel": "te_label", "moderateIntensityMinutes": "im_mod",
    "vigorousIntensityMinutes": "im_vig", "lapCount": "laps",
}


def log(msg: str) -> None:
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:  # keep the log small
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def headers() -> dict:
    """Same native-app headers ha-garmin sends; the bearer token comes from the integration."""
    try:
        from ha_garmin.auth import NATIVE_API_USER_AGENT, NATIVE_X_GARMIN_USER_AGENT  # type: ignore
    except Exception:  # library layout changed: use the last known values
        NATIVE_API_USER_AGENT = "GCM-Android-5.23"
        NATIVE_X_GARMIN_USER_AGENT = ("com.garmin.android.apps.connectmobile/5.23; ; "
                                      "Google/sdk_gphone64_arm64/google; Android/33; Dalvik/2.1.0")
    return {
        "User-Agent": NATIVE_API_USER_AGENT,
        "X-Garmin-User-Agent": NATIVE_X_GARMIN_USER_AGENT,
        "X-Garmin-Paired-App-Version": "10861",
        "X-Garmin-Client-Platform": "Android",
        "X-App-Ver": "10861",
        "X-Lang": "en",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json",
    }


def read_token() -> str | None:
    """Return the integration's DI token if it is valid for at least 2 more minutes."""
    try:
        with open(CONFIG_ENTRIES, encoding="utf-8") as f:
            entries = json.load(f)["data"]["entries"]
    except (OSError, ValueError, KeyError) as e:
        log(f"config entries unreadable: {e}")
        return None
    for e in entries:
        if e.get("domain") != "garmin_connect":
            continue
        tok = (e.get("data") or {}).get("token")
        if not tok:
            continue
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
        except Exception:
            exp = 0
        if exp and exp - time.time() < 120:
            log("token expires soon; serving cache and waiting for the integration to refresh it")
            return None
        return tok
    log("no garmin_connect config entry with a token")
    return None


def load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("activities"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"updated": None, "activities": []}


def save_cache(d: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, CACHE)


def trim(a: dict) -> dict:
    out = {}
    for k, v in KEEP.items():
        val = a.get(k)
        if val is None:
            continue
        if isinstance(val, float):
            val = round(val, 5 if v in ("lat", "lon") else 2)
        out[v] = val
    t = a.get("activityType") or {}
    out["type"] = t.get("typeKey", "other") if isinstance(t, dict) else str(t)
    if "start" in out:
        out["start"] = str(out["start"]).replace(" ", "T")[:19]
    return out


class RateLimited(Exception):
    pass


def get(session, url: str, params: dict) -> dict | list | None:
    r = session.get(url, params=params, timeout=TIMEOUT)
    if r.status_code == 429:
        raise RateLimited()
    if r.status_code in (401, 403):
        log(f"auth rejected ({r.status_code}) for {url.split('/')[-1]}")
        return None
    if r.status_code in (204, 404):
        return {}
    r.raise_for_status()
    return r.json()


def sync(cache: dict) -> dict:
    import requests

    token = read_token()
    if not token:
        return cache
    s = requests.Session()
    s.headers.update(headers())
    s.headers["Authorization"] = f"Bearer {token}"

    known = {a["id"]: a for a in cache["activities"]}
    first_run = not known
    new = 0
    try:
        for page in range(MAX_PAGES):
            data = get(s, LIST_URL, {"start": page * PAGE, "limit": PAGE})
            if data is None:
                return cache
            if not isinstance(data, list) or not data:
                break
            page_new = 0
            for raw in data:
                a = trim(raw)
                if "id" not in a:
                    continue
                if a["id"] in known:
                    known[a["id"]].update({k: v for k, v in a.items() if k != "poly"})
                else:
                    known[a["id"]] = a
                    page_new += 1
            new += page_new
            if len(data) < PAGE or (not first_run and page_new == 0):
                break
            time.sleep(0.3)
        log(f"list: {new} new, {len(known)} total")

        # GPS tracks: only for activities that have one and that we have not stored yet.
        todo = [a for a in sorted(known.values(), key=lambda x: x.get("start", ""), reverse=True)
                if a.get("has_poly") and "poly" not in a]
        done = 0
        for a in todo[:MAX_DETAILS]:
            d = get(s, DETAILS_URL.format(id=a["id"]), {"maxChartSize": 1, "maxPolylineSize": POLY_POINTS})
            if d is None:
                break
            pts = ((d or {}).get("geoPolylineDTO") or {}).get("polyline") or []
            a["poly"] = [[round(p["lat"], 5), round(p["lon"], 5)] for p in pts
                         if p.get("lat") is not None and p.get("lon") is not None]
            done += 1
            time.sleep(0.4)
        if todo:
            log(f"tracks: {done} fetched, {len(todo) - done} pending")
    except RateLimited:
        log("rate limited (429); stopping this run")
    except Exception as e:  # network etc. - keep what we have
        log(f"sync error: {type(e).__name__}: {e}")

    cache["activities"] = sorted(known.values(), key=lambda x: x.get("start", ""), reverse=True)
    cache["updated"] = datetime.now().isoformat(timespec="seconds")
    cache["pending"] = sum(1 for a in known.values() if a.get("has_poly") and "poly" not in a)
    save_cache(cache)
    return cache


def summary(cache: dict) -> dict:
    acts = cache["activities"]
    today = date.today()

    def day(a) -> date | None:
        try:
            return date.fromisoformat(a["start"][:10])
        except (KeyError, ValueError):
            return None

    def bucket(items) -> dict:
        n = len(items)
        return {
            "n": n,
            "km": round(sum(a.get("dist", 0) or 0 for a in items) / 1000, 1),
            "min": int(sum(a.get("dur", 0) or 0 for a in items) / 60),
            "kcal": int(sum(a.get("cal", 0) or 0 for a in items)),
        }

    week_start = today - timedelta(days=today.weekday())
    dated = [(day(a), a) for a in acts]
    dated = [(d, a) for d, a in dated if d]
    this_week = [a for d, a in dated if d >= week_start]
    this_month = [a for d, a in dated if d.year == today.year and d.month == today.month]
    this_year = [a for d, a in dated if d.year == today.year]
    last_week_start = week_start - timedelta(days=7)
    last_week = [a for d, a in dated if last_week_start <= d < week_start]

    weeks = []
    for i in range(11, -1, -1):
        ws = week_start - timedelta(days=7 * i)
        we = ws + timedelta(days=7)
        items = [a for d, a in dated if ws <= d < we]
        b = bucket(items)
        weeks.append([ws.strftime("%-d %b"), b["min"], b["km"], b["n"]])

    types: dict[str, int] = {}
    for a in this_year:
        types[a.get("type", "other")] = types.get(a.get("type", "other"), 0) + 1
    types = dict(sorted(types.items(), key=lambda kv: -kv[1])[:8])

    # Weekly streak: consecutive weeks (ending this week or last) with at least one activity.
    weeks_with = {(d - timedelta(days=d.weekday())) for d, _ in dated}
    streak, cursor = 0, week_start
    if cursor not in weeks_with:
        cursor -= timedelta(days=7)
    while cursor in weeks_with:
        streak += 1
        cursor -= timedelta(days=7)
    active_days_30 = len({d for d, _ in dated if d >= today - timedelta(days=29)})

    last = []
    for a in acts[:8]:
        last.append({k: a.get(k) for k in ("id", "name", "type", "start", "dist", "dur", "hr", "cal", "loc", "elev", "speed", "te")
                     if a.get(k) is not None} | {"poly": bool(a.get("poly"))})

    return {
        "state": len(acts),
        "updated": cache.get("updated"),
        "pending": cache.get("pending", 0),
        "week": bucket(this_week),
        "last_week": bucket(last_week),
        "month": bucket(this_month),
        "year": bucket(this_year),
        "all": bucket(acts),
        "tracks": sum(1 for a in acts if a.get("poly")),
        "places": len({a.get("loc") for a in acts if a.get("poly") and a.get("loc")}),
        "types": types,
        "weeks": weeks,
        "streak_weeks": streak,
        "active_days_30": active_days_30,
        "last": last,
    }


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    cache = load_cache()
    if mode == "sync":
        cache = sync(cache)
    print(json.dumps(summary(cache), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
