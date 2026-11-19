#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive a running stack the way a person would, through nginx.

The test suite runs the code; this runs the images. It exists because the
dependencies underneath - nginx, FastAPI and Starlette, Pillow, numpy - are
upgraded as a block of their own, and a passing suite does not prove that the
container starts, that nginx accepts its config, that a tile comes out of a
real render, or that a phone's delivery survives the review and lands.

Run against a stack with an empty data directory, never a real one: it writes
events. Every coordinate is synthetic, in open water near Null Island.

    python3 scripts/smoke.py http://127.0.0.1:8080 http://127.0.0.1:8000 TOKEN

Standard library only, so CI needs nothing installed for it.
"""

from __future__ import annotations

import json
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

WEB, API, TOKEN = sys.argv[1].rstrip("/"), sys.argv[2].rstrip("/"), sys.argv[3]
VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()

LON, LAT, STEP = 1.10, 0.40, 0.0002
START = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)

checks = 0


def ok(condition: bool, what: str) -> None:
    global checks
    if not condition:
        raise SystemExit(f"FAILED: {what}")
    checks += 1
    print(f"  ok  {what}")


def call(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def get_json(url: str) -> dict:
    status, _, body = call("GET", url)
    ok(status == 200, f"GET {url.replace(WEB, '')} answers 200")
    return json.loads(body)


def send_json(method: str, url: str, payload: object, token: bool = True) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Irfaran-Token"] = TOKEN
    status, _, body = call(method, url, json.dumps(payload).encode(), headers)
    return status, (json.loads(body) if body else {})


def wait_for_render() -> dict:
    for _ in range(240):
        state = json.loads(call("GET", f"{WEB}/api/render")[2])
        if state.get("state") not in ("running", "stopping") and not state.get("pending_tiles"):
            return state
        time.sleep(0.5)
    raise SystemExit("FAILED: the render queue never went idle")


def png_size(data: bytes) -> tuple[int, int]:
    ok(data[:8] == b"\x89PNG\r\n\x1a\n", "the tile is a PNG")
    return struct.unpack(">II", data[16:24])


def gpx() -> bytes:
    points = [(LON + i * STEP, LAT) for i in range(40)] + [
        (LON + 40 * STEP, LAT + i * STEP) for i in range(40)
    ]
    rows = "\n".join(
        f'<trkpt lat="{lat}" lon="{lon}"><time>'
        f'{(START + timedelta(seconds=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
        for i, (lon, lat) in enumerate(points)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="irfaran-smoke" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>Smoke loop</name><trkseg>{rows}</trkseg></trk></gpx>"
    ).encode()


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/gpx+xml\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> None:
    print(f"smoke: {WEB} (through nginx), {API} (direct), expecting {VERSION}")

    for _ in range(120):
        try:
            if call("GET", f"{WEB}/healthz")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(1)
    health = get_json(f"{WEB}/healthz")
    ok(health.get("version") == VERSION, f"the api reports {VERSION} through nginx")

    print("the page")
    status, headers, page = call("GET", f"{WEB}/")
    ok(status == 200 and "text/html" in headers.get("content-type", ""), "index.html is served")
    ok("no-cache" in headers.get("cache-control", ""), "index.html is not cached")
    text = page.decode()
    assets = sorted({part.split('"')[0] for part in text.split('/assets/')[1:]})
    ok(any(a.endswith(".js") for a in assets), "the page names its script bundle")
    for asset in assets:
        status, headers, _ = call("GET", f"{WEB}/assets/{asset}")
        kind = headers.get("content-type", "")
        expected = "javascript" if asset.endswith(".js") else "css" if asset.endswith(".css") else ""
        ok(status == 200 and expected in kind, f"/assets/{asset} is {kind}")
    status, headers, _ = call("GET", f"{WEB}/manifest.webmanifest")
    ok(status == 200 and headers.get("content-type") == "application/manifest+json",
       "the manifest is served as one")

    print("the token")
    status, _, _ = call("POST", f"{WEB}/api/events")
    ok(status == 401, "a write with no token is refused")
    status, _, _ = call("POST", f"{API}/api/events", headers={"Host": "x/api/ingest/overland?"})
    ok(status == 401, "a Host header cannot borrow an exempt path on the published port")

    print("an import, rendered")
    blank = call("GET", f"{WEB}/api/tiles/dark/all/fog/0/0/0.png")[2]
    body, kind = multipart("file", "smoke.gpx", gpx())
    status, _, answer = call("POST", f"{WEB}/api/ingest/gpx", body,
                             {"Content-Type": kind, "X-Irfaran-Token": TOKEN})
    ok(status == 200, f"a GPX upload is accepted ({answer[:120]!r})")
    wait_for_render()
    status, headers, tile = call("GET", f"{WEB}/api/tiles/dark/all/fog/0/0/0.png")
    ok(status == 200, "the rendered tile is served")
    ok(int(headers.get("content-length", -1)) == len(tile), "its body is as long as it says")
    ok(png_size(tile) == (256, 256), "it is 256 x 256")
    ok(tile != blank, "the track changed the tile")
    for kind in ("trail", "fog"):
        status, _, tile = call("GET", f"{WEB}/api/tiles/light/all/{kind}/14/8241/8173.png")
        ok(status == 200 and png_size(tile) == (256, 256), f"a z14 {kind} tile over the track")
    status, headers, _ = call("GET", f"{WEB}/api/tiles/dark/all/fog/0/0/0.png",
                              headers={"If-None-Match": headers.get("etag", "")})
    ok(status in (200, 304), "a revalidation is answered")

    print("a phone, through the review")
    status, _ = send_json("PATCH", f"{WEB}/api/settings", {"overland_ingest_enabled": "true"})
    ok(status == 200, "Overland can be switched on")
    locations = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [LON + i * STEP, LAT - 0.01]},
            "properties": {
                "timestamp": (START + timedelta(hours=2, seconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "horizontal_accuracy": 8,
                "motion": ["walking"],
            },
        }
        for i in range(20)
    ]
    status, _ = send_json("POST", f"{WEB}/api/ingest/overland", {"locations": locations})
    ok(status == 200, "a delivery is acknowledged")
    waiting = get_json(f"{WEB}/api/review")
    ok(waiting.get("count") == 1, "it waits in the review")
    held = waiting["items"][0]["id"]
    status, detail = send_json("POST", f"{WEB}/api/review/{held}/open", {})
    ok(status == 200 and detail.get("points") == 20, "opening it shows all twenty points")
    status, detail = send_json("PATCH", f"{WEB}/api/review/{held}", {
        "removed": [3],
        "moved": [[10, LON + 10 * STEP, LAT - 0.011]],
        "strokes": [{"line": [[LON, LAT - 0.02], [LON + 0.002, LAT - 0.02]], "radius_m": 25}],
    })
    ok(status == 200 and detail.get("keeping") == 18, "a remove, a move and a drawn line are kept")
    status, done = send_json("POST", f"{WEB}/api/review/{held}/approve", {})
    ok(status == 200 and done.get("moved") == 1 and done.get("drawn") == 1, "accepting it lands")
    wait_for_render()
    sources = sorted(event["source"] for event in get_json(f"{WEB}/api/events")["events"])
    ok(sources.count("overland") >= 1 and sources.count("manual") == 2 and "workout" in sources,
       f"the log holds what was accepted ({', '.join(sources)})")

    print("the rest of the page's reads")
    for path in ("/api/meta", "/api/stats", "/api/search?q=Smoke", "/api/history"):
        get_json(f"{WEB}{path}")

    print(f"smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
