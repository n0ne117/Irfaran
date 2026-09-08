# SPDX-License-Identifier: AGPL-3.0-or-later
"""The little world at the bottom of the statistics page.

Every country drawn, the visited ones in a colour. The answer to *how much of
the world* as a picture rather than as a list and a percentage.

**Rendered here rather than in the browser**, which was measured before it was
decided. The polygons are 548,471 vertices. Decimated to half a degree - about
one pixel at this size, so as coarse as it can be without showing - they are
still 39,349 vertices in 818 rings, which is 346 KB of SVG path data for a
thumbnail. A 640-pixel PNG of all 258 countries is 8 KB and a quarter of a
second, PIL is already here for the tiles and the country masks, and the
polygons never leave the machine.

**Equal Earth**, and not for looks. This is the one picture in Irfaran whose
whole subject is *how much*, and Mercator is the projection that lies about
exactly that - Greenland the size of Africa on a page that spends its effort
weighting pixels by their true ground area. Nothing stops it here: this is a
projection formula and a polygon filler, not MapLibre, so the projection the
map itself cannot offer yet is free in this one place.
"""

from __future__ import annotations

import io
import math
from functools import lru_cache

from PIL import Image, ImageDraw

from irfaran import countries

#: Equal Earth, Savric, Patterson and Jenny 2018. An equal-area pseudo-
#: cylindrical projection: every country covers a share of the picture equal to
#: its share of the planet, which is the only honest way to draw this one.
A1, A2, A3, A4 = 1.340264, -0.081106, 0.000893, 0.003796
_ROOT_THREE = math.sqrt(3.0)

#: Three states, because the statistics list has three. Merging *marginal* into
#: *visited* here would quietly promote the countries the list is careful to
#: hold apart - the ones where an approximate border would show up.
PALETTE = {
    "dark": {
        "land": (50, 50, 58),
        "marginal": (45, 68, 96),
        "visited": (77, 143, 214),
    },
    "light": {
        "land": (217, 217, 212),
        "marginal": (166, 190, 214),
        "visited": (31, 95, 168),
    },
}

#: Wider than this and it stops being a thumbnail; the statistics section it
#: sits in is about 40rem at most.
DEFAULT_WIDTH = 960


def project(lon: float, lat: float) -> tuple[float, float]:
    """Longitude and latitude to Equal Earth units, x east and y north."""
    theta = math.asin(_ROOT_THREE / 2.0 * math.sin(math.radians(lat)))
    t2 = theta * theta
    denominator = 3.0 * (9.0 * A4 * t2**4 + 7.0 * A3 * t2**3 + 3.0 * A2 * t2 + A1)
    x = 2.0 * _ROOT_THREE * math.radians(lon) * math.cos(theta) / denominator
    y = theta * (A1 + t2 * (A2 + t2 * t2 * (A3 + A4 * t2)))
    return x, y


@lru_cache(maxsize=1)
def extent() -> tuple[float, float]:
    """Half-width and half-height of the whole world, in projection units.

    Computed rather than written down: the aspect ratio of Equal Earth falls
    out of the constants above, and a number typed in by hand here would be a
    number to check against them later.
    """
    return project(180.0, 0.0)[0], project(0.0, 90.0)[1]


def size_for(width: int) -> tuple[int, int]:
    half_width, half_height = extent()
    return width, max(1, round(width * half_height / half_width))


def _draw(
    visited: frozenset[str],
    marginal: frozenset[str],
    theme: str,
    width: int,
) -> bytes:
    colours = PALETTE[theme]
    width, height = size_for(width)
    half_width, half_height = extent()
    # A transparent ground rather than a painted one: the page has a background
    # already, and it is different in the two themes and behind a blur.
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    #: Smaller than one pixel of the finished picture.
    floor_degrees = 360.0 / width

    def to_pixels(ring: countries.Ring) -> list[tuple[float, float]]:
        out = []
        for lon, lat in ring.points:
            x, y = project(lon, lat)
            out.append(
                (
                    (x + half_width) / (2.0 * half_width) * width,
                    (half_height - y) / (2.0 * half_height) * height,
                )
            )
        return out

    for country in countries.load():
        if country.code in visited:
            fill = colours["visited"]
        elif country.code in marginal:
            fill = colours["marginal"]
        else:
            fill = colours["land"]

        for rings in country.polygons:
            outer = rings[0]
            # An island narrower than a pixel is a pixel of noise, and there
            # are thousands of them. Dropped by size rather than by name.
            if (
                outer.east - outer.west < floor_degrees
                and outer.north - outer.south < floor_degrees
            ):
                continue
            points = to_pixels(outer)
            if len(points) < 3:
                continue

            # Drawn through a mask of its own rather than straight onto the
            # picture, so that a hole subtracts this country and nothing else.
            # Punching holes out of the shared image erased what was already
            # underneath - which is precisely a country inside another
            # country's hole, and precisely what a hole is usually for:
            # Lesotho came out as a Lesotho-shaped piece of sea.
            left = max(0, int(min(x for x, _ in points)) - 1)
            top = max(0, int(min(y for _, y in points)) - 1)
            right = min(width, int(max(x for x, _ in points)) + 2)
            bottom = min(height, int(max(y for _, y in points)) + 2)
            if right <= left or bottom <= top:
                continue

            stencil = Image.new("L", (right - left, bottom - top), 0)
            cutter = ImageDraw.Draw(stencil)
            cutter.polygon([(x - left, y - top) for x, y in points], fill=255)
            for hole in rings[1:]:
                if (
                    hole.east - hole.west < floor_degrees
                    and hole.north - hole.south < floor_degrees
                ):
                    continue
                cut = to_pixels(hole)
                if len(cut) >= 3:
                    cutter.polygon([(x - left, y - top) for x, y in cut], fill=0)

            image.paste(fill, (left, top), stencil)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


@lru_cache(maxsize=8)
def render(
    visited: frozenset[str],
    marginal: frozenset[str],
    theme: str = "dark",
    width: int = DEFAULT_WIDTH,
) -> bytes:
    """One PNG of the world. Cached on exactly what it is a picture of.

    The cache key is the answer rather than a fingerprint of the archive, so a
    day of walking in a country already on the list costs no redraw at all.
    """
    if theme not in PALETTE:
        raise ValueError(
            f"Unknown theme {theme!r}. The world map is drawn for "
            f"{' and '.join(sorted(PALETTE))}."
        )
    if not 120 <= width <= 2000:
        raise ValueError(f"A world {width} pixels wide is not a thumbnail.")
    return _draw(visited, marginal, theme, width)
