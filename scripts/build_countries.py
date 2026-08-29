#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn Natural Earth's country polygons into the file Irfaran ships.

Run once, by hand, and the result is committed. This exists so that the
2 MB binary in the repository is not a thing that appeared: anybody can run
this against the same input and get the same bytes out.

    scripts/build_countries.py ne_10m_admin_0_countries.geojson

The input is Natural Earth 1:10m Admin 0 - Countries, which is public domain:

    https://www.naturalearthdata.com/downloads/10m-cultural-vectors/

Why 1:10m and not something smaller: the whole difficulty of counting
countries is not counting one you have never been to. A generalised border
that cuts a corner by two kilometres will hand you Switzerland for a walk on
the Austrian side of it, and no amount of care downstream can undo that. So
the most detailed free set, and **no simplification at all** - which is also
why the file is two megabytes rather than two hundred kilobytes.

The format is deltas as zigzag varints, the same encoding the vector tiles in
the basemap use and that `mvt.py` already reads. Consecutive vertices of a
border are close together, so their differences are small, and small numbers
are one or two bytes. That is 2.2 MB against 3.9 MB for gzipped JSON, and it
needs no decompression to read.
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

MAGIC = b"IRFCTRY1"

#: Coordinates are stored as integers of this many per degree. 1e5 is about a
#: metre at the equator - far finer than the data, and coarse enough that the
#: deltas stay small.
SCALE = 100_000

EARTH_RADIUS_M = 6_371_008.8


def zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def varint(out: bytearray, value: int) -> None:
    value = zigzag(value)
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return


def ring_area_m2(ring: list[list[float]]) -> float:
    """Signed area of a ring on a sphere, in square metres.

    The standard spherical excess formula. Signed, so a hole subtracts when
    its winding is opposite - though holes are handled explicitly below rather
    than relying on winding, because Natural Earth does not promise one.
    """
    if len(ring) < 4:
        return 0.0
    total = 0.0
    for (lon_a, lat_a), (lon_b, lat_b) in zip(ring, ring[1:]):
        total += math.radians(lon_b - lon_a) * (
            2 + math.sin(math.radians(lat_a)) + math.sin(math.radians(lat_b))
        )
    return total * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0


def polygons_of(geometry: dict) -> list[list[list[list[float]]]]:
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return [geometry["coordinates"]]


def build(source: Path, target: Path) -> None:
    features = json.loads(source.read_text())["features"]

    out = bytearray(MAGIC)
    out += struct.pack("<I", len(features))
    vertices = 0

    for feature in features:
        properties = feature["properties"]
        name = str(properties.get("ADMIN") or properties.get("NAME") or "?")

        # ISO_A2 is -99 for a handful of places nobody agrees about. ADM0_A3 is
        # always there, so it is the fallback rather than losing the country.
        code = properties.get("ISO_A2")
        if code in (None, "", "-99"):
            code = str(properties.get("ADM0_A3") or "??")

        polygons = polygons_of(feature["geometry"])
        area = 0.0
        west = south = 1e9
        east = north = -1e9
        for polygon in polygons:
            area += abs(ring_area_m2(polygon[0]))
            area -= sum(abs(ring_area_m2(hole)) for hole in polygon[1:])
            for lon, lat in polygon[0]:
                west, east = min(west, lon), max(east, lon)
                south, north = min(south, lat), max(north, lat)

        encoded = bytearray()
        encoded += struct.pack("<I", len(polygons))
        for polygon in polygons:
            encoded += struct.pack("<I", len(polygon))
            for ring in polygon:
                encoded += struct.pack("<I", len(ring))
                previous_x = previous_y = 0
                for lon, lat in ring:
                    x, y = round(lon * SCALE), round(lat * SCALE)
                    varint(encoded, x - previous_x)
                    varint(encoded, y - previous_y)
                    previous_x, previous_y = x, y
                vertices += len(ring)

        name_bytes, code_bytes = name.encode("utf-8"), str(code).encode("utf-8")
        out += struct.pack("<H", len(name_bytes)) + name_bytes
        out += struct.pack("<H", len(code_bytes)) + code_bytes
        out += struct.pack("<d", area)
        out += struct.pack(
            "<iiii",
            round(west * SCALE),
            round(south * SCALE),
            round(east * SCALE),
            round(north * SCALE),
        )
        out += struct.pack("<I", len(encoded))
        out += encoded

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(out))
    print(f"{len(features)} countries, {vertices:,} vertices")
    print(f"{target} — {len(out) / 1e6:.2f} MB")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    root = Path(__file__).resolve().parent.parent
    build(Path(sys.argv[1]), root / "api" / "irfaran" / "data" / "countries.bin")
