import http.server
import json
import math
import os
import re
import socket
import time
import urllib.parse
import urllib.request
import socketserver
from datetime import datetime, timedelta, timezone

PORT = int(os.environ.get("PORT", "8000"))
HOST = "0.0.0.0"
EPHEMERIS_CACHE = {"timestamp": 0, "payload": None}
EPHEMERIS_TTL_SECONDS = 300
AUDIT_CACHE = {}
AUDIT_TTL_SECONDS = 120

EPHEMERIS_BODIES = {
    "sun": {"command": "10", "label": "Sun"},
    "moon": {"command": "301", "label": "Moon"},
    "mars": {"command": "499", "label": "Mars"},
    "jupiter": {"command": "599", "label": "Jupiter"},
}

AUDIT_SITE_COORDS = {
    "goldstone": {"lon": -116.89, "lat": 35.42, "alt_km": 1.0, "label": "Goldstone"},
    "madrid": {"lon": -4.25, "lat": 40.43, "alt_km": 0.7, "label": "Madrid"},
    "canberra": {"lon": 148.98, "lat": -35.40, "alt_km": 0.7, "label": "Canberra"},
}


def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_horizons_vector(command):
    now = datetime.now(timezone.utc)
    later = now + timedelta(minutes=1)
    params = {
        "format": "json",
        "COMMAND": f"'{command}'",
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "VECTORS",
        "CENTER": "'500@399'",
        "START_TIME": f"'{now.strftime('%Y-%m-%d %H:%M')}'",
        "STOP_TIME": f"'{later.strftime('%Y-%m-%d %H:%M')}'",
        "STEP_SIZE": "'1 m'",
        "VEC_TABLE": "'1'",
    }
    url = "https://ssd.jpl.nasa.gov/api/horizons.api?" + urllib.parse.urlencode(params)
    payload = json.loads(fetch_url(url, timeout=20).decode("utf-8"))
    result = payload.get("result", "")
    match = re.search(
        r"X\s*=\s*([+\-0-9.E]+)\s+Y\s*=\s*([+\-0-9.E]+)\s+Z\s*=\s*([+\-0-9.E]+)",
        result
    )
    if not match:
        raise ValueError(f"Could not parse Horizons vector for command {command}")
    x, y, z = (float(match.group(i)) for i in range(1, 4))
    return {"x": x, "y": y, "z": z}


def get_ephemeris_payload():
    now = time.time()
    if EPHEMERIS_CACHE["payload"] and (now - EPHEMERIS_CACHE["timestamp"]) < EPHEMERIS_TTL_SECONDS:
        return EPHEMERIS_CACHE["payload"]

    bodies = {}
    for key, spec in EPHEMERIS_BODIES.items():
        vector = fetch_horizons_vector(spec["command"])
        distance_km = (vector["x"] ** 2 + vector["y"] ** 2 + vector["z"] ** 2) ** 0.5
        bodies[key] = {
            "label": spec["label"],
            "position_km": vector,
            "distance_km": distance_km
        }

    payload = {"generated_utc": datetime.now(timezone.utc).isoformat(), "bodies": bodies}
    EPHEMERIS_CACHE["timestamp"] = now
    EPHEMERIS_CACHE["payload"] = payload
    return payload


def parse_horizons_target_name(result_text):
    match = re.search(r"Target body name:\s*(.+?)\s+\{source:", result_text)
    if match:
        return match.group(1).strip()
    match = re.search(r"Target body name:\s*(.+)", result_text)
    return match.group(1).strip() if match else ""


def parse_observer_azel(result_text):
    match = re.search(r"\$\$SOE\s*(.*?)\s*\$\$EOE", result_text, re.S)
    if not match:
        raise ValueError("Could not locate Horizons observer table rows")
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    if not lines:
        raise ValueError("Horizons observer table contained no rows")
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) < 5:
        raise ValueError("Unexpected Horizons observer row format")
    azimuth_deg = float(parts[3])
    elevation_deg = float(parts[4])
    timestamp_utc = parts[0]
    return {
        "timestamp_utc": timestamp_utc,
        "azimuth_deg": azimuth_deg,
        "elevation_deg": elevation_deg
    }


def fetch_horizons_observer_azel(command, site_key, timestamp_utc):
    site = AUDIT_SITE_COORDS.get(site_key)
    if not site:
        raise ValueError(f"Unsupported site key: {site_key}")

    dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    later = dt + timedelta(minutes=1)
    params = {
        "format": "json",
        "COMMAND": f"'{command}'",
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "'coord@399'",
        "COORD_TYPE": "GEODETIC",
        "SITE_COORD": f"'{site['lon']},{site['lat']},{site['alt_km']}'",
        "START_TIME": f"'{dt.strftime('%Y-%m-%d %H:%M:%S')}'",
        "STOP_TIME": f"'{later.strftime('%Y-%m-%d %H:%M:%S')}'",
        "STEP_SIZE": "'1 m'",
        "TIME_TYPE": "UT",
        "QUANTITIES": "'4'",
        "CSV_FORMAT": "YES",
        "ANG_FORMAT": "DEG",
        "APPARENT": "AIRLESS",
    }
    url = "https://ssd.jpl.nasa.gov/api/horizons.api?" + urllib.parse.urlencode(params)
    payload = json.loads(fetch_url(url, timeout=20).decode("utf-8"))
    result_text = payload.get("result", "")
    if not result_text:
        raise ValueError("Horizons returned an empty observer result")
    parsed = parse_observer_azel(result_text)
    parsed["target_name"] = parse_horizons_target_name(result_text)
    parsed["site_label"] = site["label"]
    return parsed


def angular_error_deg(dsn_az, dsn_el, horizons_az, horizons_el):
    dsn_az_rad = math.radians(dsn_az)
    dsn_el_rad = math.radians(dsn_el)
    hz_az_rad = math.radians(horizons_az)
    hz_el_rad = math.radians(horizons_el)
    cos_error = (
        math.sin(dsn_el_rad) * math.sin(hz_el_rad) +
        math.cos(dsn_el_rad) * math.cos(hz_el_rad) *
        math.cos(dsn_az_rad - hz_az_rad)
    )
    cos_error = max(-1.0, min(1.0, cos_error))
    return math.degrees(math.acos(cos_error))


def get_audit_payload(spacecraft_id, site_key, timestamp_utc, dsn_az, dsn_el):
    cache_key = (spacecraft_id, site_key, timestamp_utc, round(dsn_az, 3), round(dsn_el, 3))
    now = time.time()
    cached = AUDIT_CACHE.get(cache_key)
    if cached and (now - cached["timestamp"]) < AUDIT_TTL_SECONDS:
        return cached["payload"]

    horizons = fetch_horizons_observer_azel(spacecraft_id, site_key, timestamp_utc)
    delta_az = ((dsn_az - horizons["azimuth_deg"] + 540.0) % 360.0) - 180.0
    delta_el = dsn_el - horizons["elevation_deg"]
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "site_key": site_key,
        "spacecraft_id": spacecraft_id,
        "dsn": {
            "azimuth_deg": dsn_az,
            "elevation_deg": dsn_el,
            "timestamp_utc": timestamp_utc
        },
        "horizons": horizons,
        "delta": {
            "azimuth_deg": delta_az,
            "elevation_deg": delta_el,
            "total_offset_deg": angular_error_deg(dsn_az, dsn_el, horizons["azimuth_deg"], horizons["elevation_deg"])
        }
    }
    AUDIT_CACHE[cache_key] = {"timestamp": now, "payload": payload}
    return payload


def get_local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'

        if path == '/':
            self.path = '/dsn_live_viewer.html'

        if path == '/dsn-data':
            try:
                print("Fetching live data from NASA...")
                # NASA DSN XML URL
                url = "https://eyes.nasa.gov/dsn/data/dsn.xml"
                data = fetch_url(url, timeout=15)
                self.send_response(200)
                self.send_header('Content-type', 'text/xml')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
                print("Data sent successfully.")
            except Exception as e:
                print(f"Proxy Error: {e}")
                self.send_error(500, f"Proxy Error: {e}")
        elif path == '/ephemeris-data':
            try:
                print("Fetching ephemeris data from JPL Horizons...")
                payload = json.dumps(get_ephemeris_payload()).encode("utf-8")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                print(f"Proxy Error: {e}")
                self.send_error(500, f"Proxy Error: {e}")
        elif path == '/audit-data':
            try:
                query = urllib.parse.parse_qs(parsed.query)
                spacecraft_id = query.get('spacecraft_id', [''])[0].strip()
                site_key = query.get('site', [''])[0].strip().lower()
                timestamp_utc = query.get('time_utc', [''])[0].strip()
                dsn_az = float(query.get('dsn_az', [''])[0])
                dsn_el = float(query.get('dsn_el', [''])[0])
                if not spacecraft_id or not site_key or not timestamp_utc:
                    raise ValueError("Missing required audit query parameters")

                print(f"Fetching audit data from JPL Horizons for {spacecraft_id} at {site_key}...")
                payload = json.dumps(
                    get_audit_payload(spacecraft_id, site_key, timestamp_utc, dsn_az, dsn_el)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                print(f"Proxy Error: {e}")
                self.send_error(500, f"Proxy Error: {e}")
        else:
            return super().do_GET()

# Allow immediate reuse of the port after restart
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer((HOST, PORT), ProxyHandler) as httpd:
    local_ip = get_local_ip()
    print(f"Server running locally at http://127.0.0.1:{PORT}")
    print(f"Server available on your local network at http://{local_ip}:{PORT}")
    print("Keep this window open. If others cannot connect, allow Python through Windows Firewall.")
    httpd.serve_forever()
