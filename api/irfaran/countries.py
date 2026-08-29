# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which country a piece of cleared ground is in.

The archive knows where somebody has been and nothing about whose land it is.
The basemap carries border *lines* - segments with a detail level, not closed
rings per country - so there is nothing in it to test a point against. This is
therefore the one piece of Irfaran that comes from somewhere else: Natural
Earth's 1:10m country polygons, public domain, built into `data/countries.bin`
by `scripts/build_countries.py` and read here.

**Accuracy is the whole problem.** Counting countries is easy; not counting one
you have never been to is not. A border generalised by two kilometres hands you
Switzerland for a walk on the Austrian side of it, and nothing downstream can
undo that. Hence the most detailed free set and no simplification at all, which
is why the file is two megabytes. Two defences on top of that:

  ground is attributed **per pixel**, not per tile. A zoom 14 tile is 1.6 km
  across at these latitudes, so giving a whole tile to whichever country its
  centre falls in would invent kilometres of the wrong country along every
  border. Pixels are six metres.

  a country is only *listed* once enough ground has been cleared in it to be a
  visit rather than a rounding error, and everything below that line is still
  shown - separately, and with its figure - rather than silently dropped. If a
  country nobody has been to appears down there with forty square metres, that
  is the border being approximate, and it says so instead of hiding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from irfaran import geo

MAGIC = b"IRFCTRY1"
SCALE = 100_000

DATA = Path(__file__).resolve().parent / "data" / "countries.bin"

#: Below this much cleared ground, a country is reported apart from the rest.
#: Twenty thousand square metres is a kilometre of the twenty-metre corridor a
#: track leaves - far more than a generalised border can invent, and far less
#: than the shortest walk anybody would call visiting somewhere.
DEFAULT_FLOOR_M2 = 20_000.0


class CountriesUnavailable(RuntimeError):
    """The polygon file is missing or unreadable."""


@dataclass
class Ring:
    """One closed ring, with the box it lives in.

    The box is computed once at load rather than on each of the four thousand
    tiles that will ask whether this ring is anywhere near them. Working it out
    per question meant walking every vertex of every ring per tile - fifty
    thousand of them for a coastline - to answer something that never changes.
    """

    points: list[tuple[float, float]]
    west: float
    south: float
    east: float
    north: float

    def spans(self, west: float, south: float, east: float, north: float) -> bool:
        return not (
            self.east < west
            or self.west > east
            or self.north < south
            or self.south > north
        )

    @classmethod
    def of(cls, points: list[tuple[float, float]]) -> "Ring":
        lons = [point[0] for point in points]
        lats = [point[1] for point in points]
        return cls(points, min(lons), min(lats), max(lons), max(lats))


@dataclass
class Country:
    name: str
    code: str
    area_m2: float
    #: Degrees.
    west: float
    south: float
    east: float
    north: float
    #: [[outer, hole, ...], ...], each ring carrying its own bounding box.
    polygons: list[list[Ring]] = field(default_factory=list)

    def spans(
        self, west: float, south: float, east: float, north: float
    ) -> bool:
        return not (
            self.east < west
            or self.west > east
            or self.north < south
            or self.south > north
        )


# ------------------------------------------------------------------ the file


def _varints(blob: bytes, at: int, count: int) -> tuple[list[int], int]:
    """`count` zigzag varints from `at`. The encoding the basemap tiles use."""
    out: list[int] = []
    for _ in range(count):
        shift = 0
        value = 0
        while True:
            byte = blob[at]
            at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        out.append((value >> 1) ^ -(value & 1))
    return out, at


def _rings(blob: bytes) -> list[list[Ring]]:
    at = 0
    (polygon_count,) = struct.unpack_from("<I", blob, at)
    at += 4
    polygons = []
    for _ in range(polygon_count):
        (ring_count,) = struct.unpack_from("<I", blob, at)
        at += 4
        rings = []
        for _ in range(ring_count):
            (points,) = struct.unpack_from("<I", blob, at)
            at += 4
            deltas, at = _varints(blob, at, points * 2)
            ring: list[tuple[float, float]] = []
            x = y = 0
            for index in range(points):
                x += deltas[index * 2]
                y += deltas[index * 2 + 1]
                ring.append((x / SCALE, y / SCALE))
            rings.append(Ring.of(ring))
        polygons.append(rings)
    return polygons


@lru_cache(maxsize=1)
def load(path: str | None = None) -> list[Country]:
    """Every country, with its polygons decoded. Cached for the process.

    About 550,000 vertices, which is 40 MB of Python floats and a second to
    build - so it is done once and held, rather than on each of the four
    thousand tiles that will ask about it.
    """
    target = Path(path) if path else DATA
    try:
        blob = target.read_bytes()
    except OSError as exc:
        raise CountriesUnavailable(
            f"The country polygons are missing from {target}. They ship with "
            "Irfaran; rebuild them with scripts/build_countries.py."
        ) from exc

    if blob[: len(MAGIC)] != MAGIC:
        raise CountriesUnavailable(
            f"{target} does not start with {MAGIC!r}, so it is not the country "
            "file this reads."
        )

    at = len(MAGIC)
    (count,) = struct.unpack_from("<I", blob, at)
    at += 4

    out: list[Country] = []
    for _ in range(count):
        (size,) = struct.unpack_from("<H", blob, at)
        at += 2
        name = blob[at : at + size].decode("utf-8")
        at += size
        (size,) = struct.unpack_from("<H", blob, at)
        at += 2
        code = blob[at : at + size].decode("utf-8")
        at += size
        (area,) = struct.unpack_from("<d", blob, at)
        at += 8
        west, south, east, north = struct.unpack_from("<iiii", blob, at)
        at += 16
        (length,) = struct.unpack_from("<I", blob, at)
        at += 4
        polygons = _rings(blob[at : at + length])
        at += length

        out.append(
            Country(
                name=name,
                code=code,
                area_m2=area,
                west=west / SCALE,
                south=south / SCALE,
                east=east / SCALE,
                north=north / SCALE,
                polygons=polygons,
            )
        )
    # A stable order, so a pixel that two overlapping polygons both claim -
    # which happens where nobody agrees whose land it is - always goes to the
    # same one rather than to whichever was read first this time.
    out.sort(key=lambda country: (country.code, country.name))
    return out


def available() -> bool:
    try:
        load()
        return True
    except CountriesUnavailable:
        return False


# --------------------------------------------------------------- the raster


def country_at(lon: float, lat: float) -> Country | None:
    """Whose country one point is in, or None for open sea.

    Built on the same rasteriser the ground uses rather than a second
    point-in-polygon of its own, so a point and the pixel it sits in can never
    disagree about which country they are in.
    """
    tile_x, tile_y = geo.lonlat_to_tile(lon, lat)
    left, top = geo.tile_origin_px(tile_x, tile_y)
    x, y = geo.lonlat_to_px(lon, lat)
    column = min(geo.TILE_PX - 1, max(0, int(x - left)))
    row = min(geo.TILE_PX - 1, max(0, int(y - top)))

    west, south, east, north = tile_bounds(tile_x, tile_y)
    for country in load():
        if not country.spans(west, south, east, north):
            continue
        mask = mask_for(country, tile_x, tile_y)
        if mask is not None and bool(mask[row, column]):
            return country
    return None


def tile_bounds(tile_x: int, tile_y: int) -> tuple[float, float, float, float]:
    """A z14 tile as west, south, east, north in degrees."""
    left, top = geo.tile_origin_px(tile_x, tile_y)
    west, north = geo.px_to_lonlat(left, top)
    east, south = geo.px_to_lonlat(left + geo.TILE_PX, top + geo.TILE_PX)
    return west, south, east, north


def mask_for(country: Country, tile_x: int, tile_y: int) -> np.ndarray | None:
    """Which pixels of this tile are inside this country.

    Drawn rather than tested point by point: filling a polygon into a 256 by
    256 bitmap is what an imaging library is for, and it is already here for
    the tiles. Holes are drawn back out afterwards, so a lake inside a country
    is not inside it.

    Returns None when the country does not reach this tile at all, which is
    the answer for all but a handful of the 258 on every tile.
    """
    west, south, east, north = tile_bounds(tile_x, tile_y)
    if not country.spans(west, south, east, north):
        return None

    left, top = geo.tile_origin_px(tile_x, tile_y)
    image = Image.new("L", (geo.TILE_PX, geo.TILE_PX), 0)
    pen = ImageDraw.Draw(image)
    drew = False

    for rings in country.polygons:
        # Ring by ring, because a country is mostly islands it did not bring to
        # this tile. Skipping them by bounding box is the difference between
        # drawing Norway once and drawing it two thousand times.
        outer = rings[0]
        if not outer.spans(west, south, east, north):
            continue
        pen.polygon(_to_pixels(outer, left, top), fill=1)
        drew = True
        for hole in rings[1:]:
            if hole.spans(west, south, east, north):
                pen.polygon(_to_pixels(hole, left, top), fill=0)

    if not drew:
        return None
    return np.asarray(image, dtype=np.uint8) > 0


def _to_pixels(ring: Ring, left: int, top: int) -> list[tuple[float, float]]:
    out = []
    for lon, lat in ring.points:
        x, y = geo.lonlat_to_px(lon, lat)
        out.append((x - left, y - top))
    return out
