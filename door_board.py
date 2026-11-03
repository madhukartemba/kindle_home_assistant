#!/usr/bin/env python3
"""Serve a Kindle-sized door board from the Home Assistant sensors."""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HA = "http://192.168.0.207:8123"
TOKEN_FILE = Path("/etc/door-board/token")
PORT = 8787
PAGE_W = 600
PAGE_H = 794
HEADER = 56
NAV_H = 56
NAV_TOP = PAGE_H - NAV_H
SEC_H = 26
WX_H = 88
DOOR_H = 76
IST = timezone(timedelta(hours=5, minutes=30))

DOORS = (
    ("Main Door", "1c63"),
    ("Front Door 1", "9db8"),
    ("Front Door 2", "0263"),
    ("Back Door 1", "3bf0"),
    ("Back Door 2", "c965"),
)

# slug, nav label, page title, (button label, entity id)
ROOMS = (
    (
        "bedroom",
        "Bedroom",
        "Bedroom",
        (
            ("Light", "light.bedroom_light"),
            ("Ambient", "light.bedroom_ambient_light"),
            ("Table fan", "input_boolean.alexa_plug_1"),
            ("AC 1", "switch.bedroom_bedroom_ir_hub_coolix_1"),
            ("AC 2", "switch.bedroom_bedroom_ir_hub_coolix_2"),
            ("Heater", "switch.water_heater"),
        ),
    ),
    (
        "living",
        "Living",
        "Living Room",
        (
            ("Light", "light.living_room_light"),
            ("Light 2", "light.living_room_light_2"),
            ("TV", "media_player.living_room_tv"),
            ("Fan 1", "fan.fan_regulator_3"),
            ("Fan 2", "fan.living_room_fan_2"),
            ("AC 1", "switch.living_room_living_room_ir_hub_voltas_1"),
            ("AC 2", "switch.living_room_living_room_ir_hub_voltas_2"),
        ),
    ),
    (
        "main",
        "Main",
        "Main Bedroom",
        (
            ("Light", "light.main_bedroom_light"),
            ("Light 2", "light.main_bedroom_light_2"),
            ("TV", "media_player.master_bedroom_tv_2"),
            ("Fan", "fan.main_bedroom_fan_regulator"),
            ("AC 1", "switch.main_bedroom_main_bedroom_ir_hub_hitachi_ac1_1"),
            ("AC 2", "switch.main_bedroom_main_bedroom_ir_hub_hitachi_ac1_2"),
            ("Geyser", "switch.bathroom_geyser"),
        ),
    ),
    (
        "kitchen",
        "Kitchen",
        "Kitchen",
        (("Light", "light.kitchen_light"),),
    ),
)

SCRIPTS = (
    ("All off", "all", "script.all_off"),
    ("Bedroom off", "bedroom", "script.bedroom_turn_off_script"),
    ("Living room off", "living", "script.living_room_turn_off_script"),
    ("Main bedroom off", "main", "script.main_bedroom_turn_off_script"),
)

ROOM_BY = {slug: (nav, title, controls) for slug, nav, title, controls in ROOMS}
ALLOWED = {eid for _, _, _, controls in ROOMS for _, eid in controls}
SCRIPT_BY = {key: eid for _label, key, eid in SCRIPTS}

CONDITIONS = {
    "clear-night": "Clear",
    "sunny": "Sunny",
    "partlycloudy": "Partly cloudy",
    "cloudy": "Cloudy",
    "rainy": "Rain",
    "pouring": "Heavy rain",
    "lightning": "Storm",
    "lightning-rainy": "Storm",
    "fog": "Fog",
    "hail": "Hail",
    "snowy": "Snow",
    "snowy-rainy": "Snow",
    "windy": "Windy",
    "windy-variant": "Windy",
}
RAINY = {"rainy", "pouring", "lightning", "lightning-rainy", "hail", "snowy", "snowy-rainy"}

_forecast_at = 0.0
_forecast = None


def request(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def ha_headers(extra=None):
    headers = {"Authorization": "Bearer " + TOKEN_FILE.read_text().strip()}
    if extra:
        headers.update(extra)
    return headers


def fetch_states():
    _, body = request(HA + "/api/states", headers=ha_headers())
    return {item["entity_id"]: item.get("state") for item in json.loads(body)}


def classify(raw):
    raw = str(raw or "").lower()
    if raw in ("on", "open"):
        return "OPEN", "open"
    if raw in ("off", "closed"):
        return "CLOSED", "closed"
    return (raw.upper() or "UNKNOWN"), "other"


def reading(raw, unit):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "--"
    if unit == "C":
        return f"{value:.1f}°C"
    return f"{value:.0f}%"


def on_off(raw):
    raw = str(raw or "").lower()
    if raw == "on":
        return "ON", "on"
    if raw == "off":
        return "OFF", "off"
    return (raw.upper() or "UNKNOWN"), "off"


def toggle_entity(entity_id):
    domain = entity_id.split(".", 1)[0]
    request(
        HA + "/api/services/" + domain + "/toggle",
        data=json.dumps({"entity_id": entity_id}).encode(),
        headers=ha_headers({"Content-Type": "application/json"}),
    )


def run_script(entity_id):
    request(
        HA + "/api/services/script/turn_on",
        data=json.dumps({"entity_id": entity_id}).encode(),
        headers=ha_headers({"Content-Type": "application/json"}),
    )


def hourly_forecast():
    global _forecast_at, _forecast
    now = time.time()
    if _forecast is not None and now - _forecast_at < 600:
        return _forecast
    try:
        _, body = request(
            HA + "/api/services/weather/get_forecasts?return_response",
            data=json.dumps(
                {"entity_id": "weather.forecast_home", "type": "hourly"}
            ).encode(),
            headers=ha_headers({"Content-Type": "application/json"}),
            timeout=15,
        )
        payload = json.loads(body)
        _forecast = payload["service_response"]["weather.forecast_home"]["forecast"]
        _forecast_at = now
    except Exception:
        if _forecast is None:
            return None
    return _forecast


def weather_now():
    try:
        _, body = request(
            HA + "/api/states/weather.forecast_home",
            headers=ha_headers(),
            timeout=15,
        )
        item = json.loads(body)
    except Exception:
        return "Weather", "--"
    raw = str(item.get("state") or "")
    label = CONDITIONS.get(raw, raw.replace("-", " ").title() or "Weather")
    temp = (item.get("attributes") or {}).get("temperature")
    try:
        shown = "%d°C" % round(float(temp))
    except (TypeError, ValueError):
        shown = "--"
    return label, shown


def rain_reminder():
    hours = hourly_forecast()
    if not hours:
        return "No data", False
    now = datetime.now(timezone.utc)
    limit = now + timedelta(hours=24)
    for row in hours:
        raw = row.get("datetime")
        if not raw:
            continue
        when = datetime.fromisoformat(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when > limit:
            break
        try:
            precip = float(row.get("precipitation") or 0)
        except (TypeError, ValueError):
            precip = 0
        cond = str(row.get("condition") or "")
        if precip > 0 or cond in RAINY:
            local = when.astimezone(IST)
            clock = local.strftime("%-I %p") if local.minute == 0 else local.strftime("%-I:%M %p")
            return "Rain %s" % clock, True
    return "No rain", False


def page(title, refresh, body):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={PAGE_W}, height={PAGE_H}, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta http-equiv="refresh" content="20; url={refresh}">
<title>{title}</title>
<style>
html, body {{ margin: 0; padding: 0; width: {PAGE_W}px; height: {PAGE_H}px; overflow: hidden; background: #fff; color: #000; }}
body {{ font-family: Arial, Helvetica, sans-serif; }}
.page {{ position: relative; width: {PAGE_W}px; height: {PAGE_H}px; overflow: hidden; }}
.head {{ position: absolute; top: 0; left: 0; width: {PAGE_W}px; height: {HEADER}px; line-height: {HEADER}px; font-size: 28px; font-weight: bold; padding: 0 0 0 16px; border-bottom: 3px solid #000; box-sizing: border-box; }}
.time {{ position: absolute; right: 32px; top: 0; font-size: 26px; font-weight: bold; }}
a.back {{ position: absolute; right: 0; top: 0; width: 150px; height: {HEADER}px; line-height: {HEADER}px; text-align: center; background: #000; color: #fff; text-decoration: none; font-size: 28px; font-weight: bold; }}
.sec {{ position: absolute; left: 0; width: {PAGE_W}px; height: {SEC_H}px; line-height: 23px; padding-left: 16px; font-size: 18px; font-weight: bold; letter-spacing: 0.8px; border-bottom: 3px solid #000; box-sizing: border-box; }}
.wx {{ position: absolute; left: 0; width: {PAGE_W}px; border-bottom: 3px solid #000; box-sizing: border-box; background: #fff; color: #000; }}
.wx-cond {{ position: absolute; left: 20px; top: 4px; font-size: 28px; font-weight: bold; }}
.wx-temp {{ position: absolute; right: 20px; top: 0; font-size: 36px; font-weight: bold; }}
.wx-rain {{ position: absolute; left: 0; width: {PAGE_W}px; text-align: center; font-size: 26px; font-weight: bold; }}
.wx-rain.due {{ background: #000; color: #fff; }}
.mini {{ position: absolute; left: 0; width: {PAGE_W}px; border-bottom: 3px solid #000; box-sizing: border-box; }}
.mini .dname {{ position: absolute; left: 16px; top: 2px; font-size: 24px; line-height: 28px; }}
.mini .dstate {{ position: absolute; right: 14px; top: 2px; font-size: 24px; font-weight: bold; line-height: 28px; letter-spacing: 1px; }}
.mini .dlab {{ position: absolute; top: 30px; width: 170px; font-size: 14px; }}
.mini .dval {{ position: absolute; top: 44px; width: 170px; font-size: 20px; font-weight: bold; }}
.mini .temp {{ left: 16px; }}
.mini .humid {{ left: 210px; }}
.mini .bat {{ left: 400px; }}
.mini.open {{ background: #000; color: #fff; }}
.mini.closed, .mini.other {{ background: #fff; color: #000; }}
a.tile {{ position: absolute; width: 300px; box-sizing: border-box; text-decoration: none; color: #000; background: #fff; }}
a.tile.edge {{ border-left: 3px solid #000; }}
a.tile.mid {{ border-bottom: 3px solid #000; }}
a.tile .tl {{ position: absolute; left: 8px; width: 284px; text-align: center; font-size: 26px; font-weight: bold; }}
a.tile .ts {{ position: absolute; left: 8px; width: 284px; text-align: center; font-size: 16px; }}
.row {{ position: absolute; left: 0; width: {PAGE_W}px; border-bottom: 3px solid #000; box-sizing: border-box; }}
.name {{ position: absolute; left: 16px; top: 10px; font-size: 30px; }}
.state {{ position: absolute; right: 14px; top: 8px; font-size: 28px; font-weight: bold; letter-spacing: 1px; }}
.metric {{ position: absolute; top: 58px; width: 180px; }}
.metric.temp {{ left: 18px; }}
.metric.humid {{ left: 210px; }}
.metric.bat {{ left: 400px; }}
.label {{ font-size: 16px; letter-spacing: 0.4px; }}
.value {{ font-size: 26px; font-weight: bold; margin-top: 2px; }}
.row.open {{ background: #000; color: #fff; }}
.row.closed, .row.other {{ background: #fff; color: #000; }}
a.tap {{ position: absolute; left: 0; width: {PAGE_W}px; box-sizing: border-box; text-decoration: none; border-bottom: 3px solid #000; }}
a.tap.on {{ background: #000; color: #fff; }}
a.tap.off {{ background: #fff; color: #000; }}
a.nav {{ position: absolute; top: {NAV_TOP}px; height: {NAV_H}px; width: 150px; box-sizing: border-box; text-decoration: none; color: #000; background: #fff; border-top: 3px solid #000; border-left: 3px solid #000; font-size: 20px; font-weight: bold; line-height: {NAV_H}px; text-align: center; }}
a.nav.first {{ border-left: 0; }}
</style>
</head>
<body>
<div class="page">
{body}
</div>
</body>
</html>
"""


def room_nav():
    nav = []
    for index, (slug, label, _title, _controls) in enumerate(ROOMS):
        first = " first" if index == 0 else ""
        nav.append(
            '<a class="nav%s" style="left:%dpx" href="/room/%s">%s</a>'
            % (first, index * 150, slug, label)
        )
    return "\n".join(nav)


def render_home(states):
    updated = datetime.now().strftime("%-I:%M %p")
    condition, temp = weather_now()
    rain_text, raining = rain_reminder()
    rain_class = " due" if raining else ""
    y = HEADER
    parts = [
        '<div class="head">Home<span class="time">%s</span></div>' % updated,
        '<div class="sec" style="top:%dpx">Weather</div>' % y,
    ]
    y += SEC_H
    parts.append(
        '<div class="wx" style="top:%dpx;height:%dpx">'
        '<div class="wx-cond">%s</div>'
        '<div class="wx-temp">%s</div>'
        '<div class="wx-rain%s" style="top:38px;height:47px;line-height:47px">%s</div>'
        "</div>"
        % (y, WX_H, condition, temp, rain_class, rain_text)
    )
    y += WX_H
    parts.append('<div class="sec" style="top:%dpx">Doors</div>' % y)
    y += SEC_H
    for name, device in DOORS:
        label, kind = classify(states.get(f"binary_sensor.eternal_{device}_opening"))
        temp = reading(states.get(f"sensor.eternal_{device}_temperature"), "C")
        humid = reading(states.get(f"sensor.eternal_{device}_humidity"), "%")
        battery = reading(states.get(f"sensor.eternal_{device}_battery"), "%")
        parts.append(
            '<div class="mini %s" style="top:%dpx;height:%dpx">'
            '<div class="dname">%s</div><div class="dstate">%s</div>'
            '<div class="dlab temp">Temp</div><div class="dval temp">%s</div>'
            '<div class="dlab humid">Humidity</div><div class="dval humid">%s</div>'
            '<div class="dlab bat">Battery</div><div class="dval bat">%s</div>'
            "</div>"
            % (kind, y, DOOR_H, name, label, temp, humid, battery)
        )
        y += DOOR_H
    parts.append('<div class="sec" style="top:%dpx">Rooms</div>' % y)
    y += SEC_H
    tiles = (
        ("All off", "", "all"),
        ("Bedroom", "Turn off", "bedroom"),
        ("Living room", "Turn off", "living"),
        ("Main bedroom", "Turn off", "main"),
    )
    row_h = int((NAV_TOP - y) / 2)
    for index, (title, subtitle, key) in enumerate(tiles):
        col = index % 2
        row = index // 2
        top = y + row * row_h
        height = (NAV_TOP - top) if row == 1 else row_h
        classes = "tile"
        if col == 1:
            classes += " edge"
        if row == 0:
            classes += " mid"
        if subtitle:
            title_top = max((height - 48) // 2, 8)
            inner = (
                '<span class="tl" style="top:%dpx">%s</span>'
                '<span class="ts" style="top:%dpx">%s</span>'
                % (title_top, title, title_top + 30, subtitle)
            )
        else:
            title_top = max((height - 30) // 2, 8)
            inner = '<span class="tl" style="top:%dpx">%s</span>' % (title_top, title)
        parts.append(
            '<a class="%s" style="top:%dpx;left:%dpx;height:%dpx" href="/s/%s">%s</a>'
            % (classes, top, col * 300, height, key, inner)
        )
    parts.append(room_nav())
    return page("Home", "/", "\n".join(parts))


def render_doors(states):
    count = len(DOORS)
    height = int((PAGE_H - HEADER) / count)
    rows = []
    for index, (name, device) in enumerate(DOORS):
        label, kind = classify(states.get(f"binary_sensor.eternal_{device}_opening"))
        temp = reading(states.get(f"sensor.eternal_{device}_temperature"), "C")
        humid = reading(states.get(f"sensor.eternal_{device}_humidity"), "%")
        battery = reading(states.get(f"sensor.eternal_{device}_battery"), "%")
        top = HEADER + index * height
        row_height = (PAGE_H - top) if index == count - 1 else height
        rows.append(
            '<div class="row %s" style="top:%dpx;height:%dpx">'
            '<div class="name">%s</div>'
            '<div class="state">%s</div>'
            '<div class="metric temp"><div class="label">Temperature</div><div class="value">%s</div></div>'
            '<div class="metric humid"><div class="label">Humidity</div><div class="value">%s</div></div>'
            '<div class="metric bat"><div class="label">Battery</div><div class="value">%s</div></div>'
            "</div>"
            % (kind, top, row_height, name, label, temp, humid, battery)
        )
    body = '<div class="head">Doors<a class="back" href="/">Back</a></div>\n' + "\n".join(rows)
    return page("Doors", "/doors", body)


def render_room(slug, states):
    _nav, title, controls = ROOM_BY[slug]
    count = max(len(controls), 1)
    top0 = HEADER
    height = int((PAGE_H - top0) / count)
    rows = []
    for index, (label, eid) in enumerate(controls):
        text, kind = on_off(states.get(eid))
        top = top0 + index * height
        row_height = (PAGE_H - top) if index == count - 1 else height
        pad = max((row_height - 34) // 2, 8)
        rows.append(
            '<a class="tap %s" href="/t/%s/%s" style="top:%dpx;height:%dpx">'
            '<span class="name" style="top:%dpx">%s</span>'
            '<span class="state" style="top:%dpx">%s</span>'
            "</a>"
            % (kind, slug, eid, top, row_height, pad, label, pad - 2, text)
        )
    body = (
        '<div class="head">%s<a class="back" href="/">Back</a></div>\n%s'
        % (title, "\n".join(rows))
    )
    return page(title, "/room/" + slug, body)


def route(path):
    path = path.split("?", 1)[0]
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[0] == "t":
        slug, eid = parts[1], parts[2]
        if eid in ALLOWED:
            toggle_entity(eid)
            time.sleep(0.6)
        states = fetch_states()
        if slug in ROOM_BY:
            return render_room(slug, states)
        return render_home(states)
    if len(parts) == 2 and parts[0] == "s" and parts[1] in SCRIPT_BY:
        run_script(SCRIPT_BY[parts[1]])
        time.sleep(0.6)
        return render_home(fetch_states())
    if len(parts) == 1 and parts[0] == "off":
        run_script(SCRIPT_BY["all"])
        time.sleep(0.6)
        return render_home(fetch_states())
    if parts == ["doors"]:
        return render_doors(fetch_states())
    if len(parts) == 2 and parts[0] == "room" and parts[1] in ROOM_BY:
        return render_room(parts[1], fetch_states())
    return render_home(fetch_states())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = route(self.path).encode()
            status = 200
            content_type = "text/html; charset=utf-8"
        except Exception as err:
            payload = f"<h1>Door board unavailable</h1><p>{err}</p>".encode()
            status = 500
            content_type = "text/html; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print("%s %s" % (datetime.now().isoformat(timespec="seconds"), fmt % args), flush=True)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"door board on http://0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
