#!/usr/bin/env python3
"""What is above the house right now: planes (ADS-B) and satellites (SGP4 from CelesTrak TLEs).

Runs inside the HA core container from packages/overhead.yaml (command_line sensors):

    python3 /config/bin/overhead.py planes LAT LON      -> sensor.planes_overhead
    python3 /config/bin/overhead.py sats   LAT LON      -> sensor.satellites_overhead

Prints one JSON object; the sensor takes `state` and the listed attributes.

planes
  Sources (merged by ICAO hex, both keyless): adsb.lol (community feeders, every run) and OpenSky
  Network. Anonymous OpenSky allows 400 calls/day, so the script only asks it every 5 min on its own
  and reuses the last OpenSky planes in between; with a free API client
  ({"opensky_client_id": ..., "opensky_client_secret": ...} in /config/overhead_secrets.json)
  it asks OpenSky on every run.
  state = planes within RADIUS_KM of home (a jet 100 km out is still a dot you can see); "close"
  counts the ones within CLOSE_KM, i.e. really above the head. Attributes list the nearest ones.

sats
  CelesTrak "active" group (~16k objects, refreshed once a day into /config/.overhead/; CelesTrak
  403-blocks an IP that re-fetches the same file within 2 h, so never poll it faster), propagated
  with the vendored pure-python sgp4 (bin/sgp4/). state = low-orbit satellites (< HIGH_KM) higher
  than MIN_EL degrees above the horizon (a 2*(90-MIN_EL) degree cone around the zenith); these
  are the ones that move. "high" counts the parked ones (GPS/NavIC/Beidou, geostationary TV and
  weather satellites) in the same cone. Attributes: Starlink / navigation / ISRO counts, the ISS
  position and its next pass, the highest few.

When a source fails the last good answer is printed again with "stale": true, so the sensors
never flip to unknown because of one bad fetch.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

STATE_DIR = os.environ.get("OVERHEAD_DIR", "/config/.overhead")
SECRETS = os.environ.get("OVERHEAD_SECRETS", "/config/overhead_secrets.json")
UA = {"User-Agent": "homebase-overhead/1.0 (home dashboard; single household)"}

RADIUS_KM = 100.0    # the sensor counts planes inside this circle
CLOSE_KM = 25.0      # "right above you": a cruising jet within 25 km is ~25 degrees up or more
MAX_PLANES = 6

MIN_EL = 30.0        # satellites this high above the horizon count as "overhead"
SKY_EL = 10.0        # ... and this high count as "in the sky"
HIGH_KM = 2000.0     # orbits above this (MEO, GEO) barely move across the sky
TLE_MAX_AGE_H = 24   # CelesTrak blocks an IP that re-fetches the same file within 2 h; once a day is plenty
TLE_RETRY_H = 3      # after a failed download wait this long before trying again
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass(bearing):
    return COMPASS[int(((bearing % 360) + 22.5) // 45) % 8]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def finish(kind, result):
    """Print the answer and remember it as the fallback for the next failed run."""
    result["stale"] = False
    result["updated"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    save_json(os.path.join(STATE_DIR, kind + ".json"), result)
    print(json.dumps(result))


def fail(kind, error):
    last = load_json(os.path.join(STATE_DIR, kind + ".json"))
    if last:
        last["stale"] = True
        last["error"] = str(error)[:200]
        print(json.dumps(last))
    else:
        print(json.dumps({"state": "unknown", "stale": True, "error": str(error)[:200]}))
    sys.exit(0)


# ---------------------------------------------------------------------------------- planes

# ICAO airline designators -> names (Indian carriers first, then common international ones; extend for your region).
AIRLINES = {
    "AIC": "Air India", "IGO": "IndiGo", "AXB": "Air India Express", "AKJ": "Akasa Air", "SEJ": "SpiceJet",
    "VTI": "Vistara", "LLR": "Alliance Air", "SDG": "Star Air", "FBG": "flybig", "IAD": "IndiaOne Air",
    "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad", "FDB": "flydubai", "ABY": "Air Arabia",
    "SVA": "Saudia", "GFA": "Gulf Air", "OMA": "Oman Air", "KAC": "Kuwait Airways", "JZR": "Jazeera",
    "BAW": "British Airways", "VIR": "Virgin Atlantic", "DLH": "Lufthansa", "AFR": "Air France", "KLM": "KLM",
    "SWR": "Swiss", "AUA": "Austrian", "FIN": "Finnair", "THY": "Turkish Airlines", "ETH": "Ethiopian",
    "SIA": "Singapore Airlines", "THA": "Thai Airways", "MAS": "Malaysia Airlines", "CPA": "Cathay Pacific",
    "ANA": "ANA", "JAL": "Japan Airlines", "KAL": "Korean Air", "CCA": "Air China", "CES": "China Eastern",
    "CSN": "China Southern", "QFA": "Qantas", "UAL": "United", "AAL": "American", "DAL": "Delta",
    "ACA": "Air Canada", "UZB": "Uzbekistan Airways", "RNA": "Nepal Airlines", "BBC": "Biman", "UBG": "US-Bangla",
    "ALK": "SriLankan", "PIA": "PIA", "AFL": "Aeroflot", "FDX": "FedEx", "UPS": "UPS", "GEC": "Lufthansa Cargo",
    "CLX": "Cargolux", "GTI": "Atlas Air", "BOX": "AeroLogic", "BCS": "DHL", "QAF": "Qatar Amiri Flight",
    "IFC": "Indian Air Force",
}


def airline(callsign):
    cs = (callsign or "").strip().upper()
    if len(cs) >= 4 and cs[:3].isalpha() and cs[3:4].isdigit():
        return AIRLINES.get(cs[:3], "")
    return ""


def opensky_token():
    sec = load_json(SECRETS, {}) or {}
    cid, csec = sec.get("opensky_client_id"), sec.get("opensky_client_secret")
    if not cid or not csec:
        return None
    cache = load_json(os.path.join(STATE_DIR, "opensky_token.json"), {}) or {}
    if cache.get("expires", 0) > time.time() + 60:
        return cache["token"]
    r = requests.post("https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
                      data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
                      headers=UA, timeout=15)
    r.raise_for_status()
    j = r.json()
    save_json(os.path.join(STATE_DIR, "opensky_token.json"), {"token": j["access_token"], "expires": time.time() + int(j.get("expires_in", 1800))})
    return j["access_token"]


ANON_GAP_S = 300     # anonymous OpenSky: 400 credits/day -> one call per 5 min at most


def fetch_opensky(lat, lon):
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(0.2, math.cos(math.radians(lat))))
    headers = dict(UA)
    tok = None
    try:
        tok = opensky_token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
    except Exception:
        pass  # anonymous is fine, just slower
    if not tok:
        # The sensor may poll every minute; without an API client only every 5th call may hit OpenSky.
        stamp = os.path.join(STATE_DIR, "opensky_last")
        last = os.path.getmtime(stamp) if os.path.exists(stamp) else 0
        if time.time() - last < ANON_GAP_S:
            raise RuntimeError("anonymous throttle")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(stamp, "w") as f:
            f.write(str(time.time()))
    r = requests.get("https://opensky-network.org/api/states/all",
                     params={"lamin": lat - dlat, "lomin": lon - dlon, "lamax": lat + dlat, "lomax": lon + dlon},
                     headers=headers, timeout=20)
    r.raise_for_status()
    out = {}
    for s in (r.json().get("states") or []):
        if s[5] is None or s[6] is None or s[8]:
            continue  # no position / on the ground
        out[s[0].lower()] = {
            "hex": s[0].lower(), "callsign": (s[1] or "").strip(), "country": s[2] or "",
            "lat": s[6], "lon": s[5], "alt_m": s[7] if s[7] is not None else s[13],
            "speed_kmh": round((s[9] or 0) * 3.6), "heading": s[10], "vrate": s[11] or 0,
            "type": "", "reg": "", "source": "opensky",
        }
    return out


def fetch_adsblol(lat, lon):
    nm = RADIUS_KM / 1.852
    r = requests.get(f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{nm:.0f}", headers=UA, timeout=20)
    r.raise_for_status()
    out = {}
    for a in r.json().get("ac", []):
        if a.get("lat") is None or a.get("alt_baro") == "ground":
            continue
        alt = a.get("alt_baro") if isinstance(a.get("alt_baro"), (int, float)) else a.get("alt_geom")
        out[str(a.get("hex", "")).lower()] = {
            "hex": str(a.get("hex", "")).lower(), "callsign": (a.get("flight") or "").strip(), "country": "",
            "lat": a["lat"], "lon": a["lon"], "alt_m": alt * 0.3048 if isinstance(alt, (int, float)) else None,
            "speed_kmh": round((a.get("gs") or 0) * 1.852), "heading": a.get("track"), "vrate": (a.get("baro_rate") or 0) * 0.00508,
            "type": a.get("t") or "", "reg": a.get("r") or "", "desc": a.get("desc") or "", "source": "adsb.lol",
        }
    return out


def planes(lat, lon):
    merged, sources, errors = {}, [], []
    os_cache = os.path.join(STATE_DIR, "opensky_planes.json")
    for name, fn in (("adsb.lol", fetch_adsblol), ("opensky", fetch_opensky)):
        try:
            got = fn(lat, lon)
            sources.append(name)
            if name == "opensky":
                save_json(os_cache, {"at": time.time(), "planes": got})
            for hx, p in got.items():
                if hx in merged:  # keep the richer record, fill gaps
                    for k, v in p.items():
                        if not merged[hx].get(k) and v:
                            merged[hx][k] = v
                else:
                    merged[hx] = p
        except Exception as e:
            if name == "opensky":
                # throttled or failed: reuse OpenSky's last answer if it is under 6 min old (planes move ~15 km/min)
                cached = load_json(os_cache, {}) or {}
                if time.time() - cached.get("at", 0) < 360:
                    sources.append("opensky (cached)")
                    for hx, p in cached.get("planes", {}).items():
                        merged.setdefault(hx, p)
                    continue
            errors.append(f"{name}: {e}")
    if not sources:
        fail("planes", "; ".join(errors))

    rows = []
    for p in merged.values():
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d > RADIUS_KM:
            continue
        b = bearing_deg(lat, lon, p["lat"], p["lon"])
        alt_m = p.get("alt_m")
        hdg = p.get("heading")
        rows.append({
            "callsign": p["callsign"] or p.get("reg") or p["hex"].upper(),
            "airline": airline(p["callsign"]),
            "hex": p["hex"],
            "alt_m": round(alt_m) if alt_m is not None else None,
            "alt_ft": round(alt_m / 0.3048 / 100) * 100 if alt_m is not None else None,
            "dist_km": round(d, 1),
            "dir": compass(b),
            "heading": round(hdg) if hdg is not None else None,
            "heading_dir": compass(hdg) if hdg is not None else "",
            "speed_kmh": p.get("speed_kmh") or 0,
            "climb": 1 if (p.get("vrate") or 0) > 1.5 else (-1 if (p.get("vrate") or 0) < -1.5 else 0),
            "type": p.get("type", ""),
            "desc": p.get("desc", ""),
            "reg": p.get("reg", ""),
            "country": p.get("country", ""),
            "lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
            "source": p["source"],
        })
    rows.sort(key=lambda r: r["dist_km"])
    finish("planes", {
        "state": len(rows),
        "close": sum(1 for r in rows if r["dist_km"] <= CLOSE_KM),
        "radius_km": RADIUS_KM,
        "close_km": CLOSE_KM,
        "planes": rows[:MAX_PLANES],
        "closest": rows[0] if rows else None,
        "sources": sources,
        "errors": errors,
    })


# ---------------------------------------------------------------------------------- satellites

def tle_lines():
    path = os.path.join(STATE_DIR, "active.tle")
    stamp = os.path.join(STATE_DIR, "tle_attempt")
    age_h = (time.time() - os.path.getmtime(path)) / 3600 if os.path.exists(path) else 1e9
    since_try_h = (time.time() - os.path.getmtime(stamp)) / 3600 if os.path.exists(stamp) else 1e9
    if age_h > TLE_MAX_AGE_H and since_try_h > TLE_RETRY_H:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(stamp, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        try:
            r = requests.get(TLE_URL, headers=UA, timeout=60)
            r.raise_for_status()
            if r.text.count("\n") > 300:  # sanity: CelesTrak answers with HTML on errors
                with open(path + ".tmp", "w") as f:
                    f.write(r.text)
                os.replace(path + ".tmp", path)
                age_h = 0
        except Exception:
            if not os.path.exists(path):
                raise
    with open(path) as f:
        return f.read().splitlines(), age_h


def gmst_rad(jd):
    t = (jd - 2451545.0) / 36525.0
    sec = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return math.radians((sec % 86400) / 240.0)


def sun_teme_unit(jd):
    """Low-precision sun direction (Astronomical Almanac), good to ~0.01 deg: plenty for a shadow test."""
    n = jd - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    return (math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam))


def kind_of(name):
    n = name.upper()
    if n.startswith("STARLINK"):
        return "starlink"
    if n.startswith("ONEWEB"):
        return "oneweb"
    if n.startswith("ISS (ZARYA)"):
        return "iss"
    if n.startswith("CSS (") or n.startswith("TIANHE"):
        return "css"
    if any(k in n for k in ("NAVSTAR", "GPS ", "GLONASS", "GALILEO", "BEIDOU", "IRNSS", "NVS-", "QZS", "NAVIC")):
        return "nav"
    if any(k in n for k in ("GSAT", "INSAT", "CARTOSAT", "RISAT", "RESOURCESAT", "OCEANSAT", "EOS-", "NISAR", "ASTROSAT",
                            "ADITYA", "IRNSS", "NVS-", "CMS-", "GISAT", "SCATSAT", "HYSIS", "EMISAT", "XPOSAT", "MICROSAT")):
        return "isro"
    if n.startswith("IRIDIUM"):
        return "iridium"
    if n.startswith("HST"):
        return "hubble"
    return "other"


PRETTY = {"iss": "ISS", "css": "Tiangong", "hubble": "Hubble"}


class Observer:
    def __init__(self, lat, lon, alt_km=0.0):
        self.lat, self.lon = math.radians(lat), math.radians(lon)
        a, f = 6378.137, 1 / 298.257223563
        e2 = f * (2 - f)
        sl, cl = math.sin(self.lat), math.cos(self.lat)
        N = a / math.sqrt(1 - e2 * sl * sl)
        self.pos = ((N + alt_km) * cl * math.cos(self.lon), (N + alt_km) * cl * math.sin(self.lon), (N * (1 - e2) + alt_km) * sl)
        self.sl, self.cl, self.slon, self.clon = sl, cl, math.sin(self.lon), math.cos(self.lon)

    def look(self, r_teme, theta):
        """TEME position (km) + GMST -> (elevation deg, azimuth deg, range km)."""
        ct, st = math.cos(theta), math.sin(theta)
        x = r_teme[0] * ct + r_teme[1] * st
        y = -r_teme[0] * st + r_teme[1] * ct
        z = r_teme[2]
        dx, dy, dz = x - self.pos[0], y - self.pos[1], z - self.pos[2]
        e = -self.slon * dx + self.clon * dy
        n = -self.sl * self.clon * dx - self.sl * self.slon * dy + self.cl * dz
        u = self.cl * self.clon * dx + self.cl * self.slon * dy + self.sl * dz
        rng = math.sqrt(dx * dx + dy * dy + dz * dz)
        return math.degrees(math.asin(u / rng)), (math.degrees(math.atan2(e, n)) + 360) % 360, rng


def sunlit(r, s):
    """Cylindrical Earth-shadow test: r = satellite TEME km, s = unit sun vector."""
    d = r[0] * s[0] + r[1] * s[1] + r[2] * s[2]
    if d > 0:
        return True
    px, py, pz = r[0] - d * s[0], r[1] - d * s[1], r[2] - d * s[2]
    return math.sqrt(px * px + py * py + pz * pz) > 6371.0


def sats(lat, lon):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sgp4.earth_gravity import wgs72
    from sgp4.ext import jday
    from sgp4.io import twoline2rv

    try:
        lines, age_h = tle_lines()
    except Exception as e:
        fail("sats", f"tle: {e}")

    obs = Observer(lat, lon)
    now = datetime.now(timezone.utc)
    jd = jday(now.year, now.month, now.day, now.hour, now.minute, now.second + now.microsecond / 1e6)
    theta = gmst_rad(jd)
    sun = sun_teme_unit(jd)
    sun_el = obs.look(tuple(c * 1.5e8 for c in sun), theta)[0]
    night = sun_el < -6.0

    overhead, sky_count, total, bad = [], 0, 0, 0
    iss_sat = None
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            continue
        try:
            sat = twoline2rv(l1, l2, wgs72)
            r, _ = sat.propagate(now.year, now.month, now.day, now.hour, now.minute, now.second)
        except Exception:
            bad += 1
            continue
        if sat.error or r[0] != r[0]:
            bad += 1
            continue
        total += 1
        if name.startswith("ISS (ZARYA)"):
            iss_sat = (sat, r)
        el, az, rng = obs.look(r, theta)
        if el < SKY_EL:
            continue
        sky_count += 1
        if el >= MIN_EL:
            overhead.append({"name": name, "kind": kind_of(name), "el": round(el), "az": round(az), "dir": compass(az),
                             "range_km": round(rng), "lit": sunlit(r, sun), "alt_km": round(math.sqrt(sum(c * c for c in r)) - 6371)})

    overhead.sort(key=lambda s: -s["el"])
    kinds = {}
    for s in overhead:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    low = [s for s in overhead if s["alt_km"] < HIGH_KM]

    def pretty(s):
        n = PRETTY.get(s["kind"]) or (s["name"].title().replace("Starlink-", "Starlink ") if s["kind"] in ("starlink", "oneweb") else s["name"])
        return {**s, "name": n}

    # the ISS now and its next pass (>10 deg) within 24 h, sampled every 30 s
    iss = None
    if iss_sat:
        sat, r = iss_sat
        el, az, rng = obs.look(r, theta)
        iss = {"el": round(el), "az": round(az), "dir": compass(az), "range_km": round(rng), "above": el > 0, "lit": sunlit(r, sun)}
        t = now
        in_pass, best, start = el > SKY_EL, None, None
        for step in range(0, 24 * 120):
            t = now + timedelta(seconds=30 * step)
            jd2 = jday(t.year, t.month, t.day, t.hour, t.minute, t.second)
            r2, _ = sat.propagate(t.year, t.month, t.day, t.hour, t.minute, t.second)
            e2 = obs.look(r2, gmst_rad(jd2))[0]
            if e2 > SKY_EL:
                if not in_pass:
                    in_pass, start, best = True, t, e2
                elif best is None or e2 > best:
                    best = e2
            elif in_pass:
                if start is not None:  # skip a pass that already started before now
                    iss["next_pass"] = {"start": start.astimezone().isoformat(timespec="minutes"), "max_el": round(best),
                                        "duration_s": int((t - start).total_seconds()), "starts_in_min": int((start - now).total_seconds() // 60)}
                    break
                in_pass = False

    finish("sats", {
        "state": len(low),
        "high": len(overhead) - len(low),
        "all": len(overhead),
        "min_elevation": MIN_EL,
        "high_km": HIGH_KM,
        "in_sky": sky_count,
        "sky_elevation": SKY_EL,
        "starlink": kinds.get("starlink", 0),
        "nav": kinds.get("nav", 0),
        "isro": kinds.get("isro", 0),
        "kinds": kinds,
        "lit": sum(1 for s in low if s["lit"]),
        "night": night,
        "sun_el": round(sun_el),
        "highest": [pretty(s) for s in low[:6]],
        "notable": [pretty(s) for s in overhead if s["kind"] in ("iss", "css", "hubble", "isro", "nav")][:6],
        "iss": iss,
        "tracked": total,
        "skipped": bad,
        "tle_age_h": round(age_h, 1),
    })


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] not in ("planes", "sats"):
        print(__doc__)
        sys.exit(2)
    mode, lat, lon = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    try:
        planes(lat, lon) if mode == "planes" else sats(lat, lon)
    except SystemExit:
        raise
    except Exception as e:
        fail(mode, e)
