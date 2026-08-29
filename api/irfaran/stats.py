# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the archive adds up to.

Everything here is derived - not one number is stored as truth - so all of it
can be thrown away and recomputed. What it is not is cheap: measuring how much
ground has been cleared means reading every fog blob in the archive and
counting bits, which is three seconds today and will be more later. So it is
computed on demand, written down with a fingerprint of the archive it was
computed from, and handed back untouched until that fingerprint changes.

Two things about the area that are easy to get wrong and were:

  **Mercator is not a map of areas.** A pixel near the poles covers a fraction
  of the ground a pixel at the equator does - at zoom 14, a tile is 5.97 km2 at
  the equator and 0.72 km2 at the latitude of Tromso. Counting cleared pixels
  and calling the answer area would flatter anybody who has been north, and by
  a factor of eight. Every pixel here is weighted by the true ground area of
  its row. See geo.tile_row_area_m2.

  **The same street in two years is one piece of ground.** Fog is stored per
  source and per layer, so a commute walked in 2019 and again in 2024 has two
  blobs covering identical pixels. They are unioned per tile before anything is
  counted, or the figure would grow every year without anybody going anywhere
  new.

Water counts. The denominator is the whole planet, ocean included, because a
ferry crossing is as much a place you have been as a footpath - and because
excluding water would need a land mask nobody has.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

import numpy as np

from irfaran import geo, raster

#: Where the last computed answer is kept, with what it was computed from.
CACHE_SETTING = "stats_cache"
FINGERPRINT_SETTING = "stats_fingerprint"

#: Sources that arrive without anybody asking, as opposed to a file somebody
#: dropped in or a line somebody drew.
LIVE_SOURCES = ("overland", "owntracks", "ha")


def fingerprint(conn: sqlite3.Connection) -> str:
    """Something cheap that changes whenever any of the numbers could have.

    Three counts and a high-water mark, all of them index reads. The point is
    that asking "is the cached answer still true?" must not cost anything like
    what computing the answer costs, or the cache is pointless.
    """
    events = conn.execute(
        "SELECT count(*) AS n, COALESCE(max(id), 0) AS top FROM events"
    ).fetchone()
    blobs = conn.execute("SELECT count(*) AS n FROM blobs").fetchone()["n"]
    places = conn.execute("SELECT count(*) AS n FROM places").fetchone()["n"]
    return f"{events['n']}:{events['top']}:{blobs}:{places}"


# ------------------------------------------------------------------- ground


def cleared_tiles(conn: sqlite3.Connection) -> Iterator[tuple[int, int, np.ndarray]]:
    """Every z14 tile that holds fog, as one mask per tile.

    Blobs arrive grouped by tile so that the union can be built and released
    one tile at a time. Holding the whole archive's fog in memory at once is
    three hundred megabytes of masks for no reason.
    """
    rows = conn.execute(
        "SELECT x, y, source, layer, data FROM blobs WHERE kind = 'fog' "
        "ORDER BY x, y"
    )
    current: tuple[int, int] | None = None
    mask: np.ndarray | None = None

    for row in rows:
        here = (int(row["x"]), int(row["y"]))
        if here != current:
            if mask is not None and current is not None:
                yield current[0], current[1], mask
            current, mask = here, None

        blob = raster.decode(
            row["data"], "fog", row["source"], row["layer"], here[0], here[1]
        )
        # The union, not the sum: ground walked in two different years is one
        # piece of ground.
        mask = (blob > 0) if mask is None else (mask | (blob > 0))

    if mask is not None and current is not None:
        yield current[0], current[1], mask


def ground(conn: sqlite3.Connection) -> dict[str, object]:
    """How much of the planet has had its fog cleared."""
    square_metres = 0.0
    tiles = 0

    for _, tile_y, mask in cleared_tiles(conn):
        lit = int(np.count_nonzero(mask))
        if not lit:
            continue
        tiles += 1
        square_metres += lit * geo.tile_pixel_area_m2(tile_y)

    return {
        "square_metres": square_metres,
        "square_km": square_metres / 1e6,
        "tiles": tiles,
        # Of the whole planet rather than of the tile grid. The grid stops at
        # the latitudes Mercator can draw, so it is 0.37% short of a planet -
        # but that missing sliver is the two polar caps, and the question was
        # what fraction of the world has been seen.
        "percent_of_planet": square_metres / geo.EARTH_SURFACE_M2 * 100.0,
    }


# ------------------------------------------------------------------- counting


def routes(conn: sqlite3.Connection) -> dict[str, object]:
    """How many routes there are, and where they came from.

    Workouts are one bucket rather than two, and that is the data's doing: a
    file dropped in under Import and an activity a tracker fetched are both
    written as source `workout` with the same shape, deliberately, so that an
    activity already imported by hand is recognised rather than drawn twice.
    Nothing in the log distinguishes them afterwards.
    """
    counts = {
        row["source"]: int(row["n"])
        for row in conn.execute(
            "SELECT source, count(*) AS n FROM events "
            "WHERE op = 'add' GROUP BY source"
        )
    }

    live = sum(counts.get(source, 0) for source in LIVE_SOURCES)
    workouts = counts.get("workout", 0)
    drawn = counts.get("manual", 0)

    # What a person would call a route, as well as what the log calls one: a
    # long ride arrives as one event per continuous stretch, so a bare count
    # of rows says more journeys than anybody made.
    names: set[str] = set()
    for row in conn.execute(
        "SELECT meta FROM events WHERE op = 'add' AND meta IS NOT NULL"
    ):
        meta = json.loads(row["meta"])
        if meta.get("track"):
            names.add(str(meta["track"]))

    return {
        "total": live + workouts + drawn,
        "workouts": workouts,
        "live": live,
        "drawn": drawn,
        "by_source": counts,
        "named_journeys": len(names),
    }


def marks(conn: sqlite3.Connection) -> dict[str, object]:
    """The other things drawn on the map by hand."""
    one = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731
    return {
        "pins": one("SELECT count(*) FROM places"),
        "labels": one("SELECT count(*) FROM labels"),
        "folders": one("SELECT count(*) FROM folders"),
        "people": one("SELECT count(*) FROM people"),
        "revealed": one("SELECT count(*) FROM events WHERE op = 'reveal'"),
        "refogged": one("SELECT count(*) FROM events WHERE op = 'erase'"),
    }


def points(conn: sqlite3.Connection) -> int:
    """How many positions the archive is built from.

    Read out of each event's meta rather than by counting coordinates, which
    would mean parsing 28 MB of geometry to answer a question nobody needs to
    the digit. It is what each source reported, so a track imported twice in
    two formats counts twice - which is the honest answer to "how many points
    are on the map" anyway.
    """
    total = 0
    for row in conn.execute(
        "SELECT meta FROM events WHERE op = 'add' AND meta IS NOT NULL"
    ):
        total += int(json.loads(row["meta"]).get("fixes") or 0)
    return total


def years(conn: sqlite3.Connection) -> dict[str, object]:
    """Which time layers have anything in them."""
    layers = [
        str(row["layer"])
        for row in conn.execute(
            "SELECT DISTINCT layer FROM blobs WHERE kind IN ('fog', 'trail') "
            "ORDER BY layer"
        )
    ]
    dated = [layer for layer in layers if layer.isdigit()]
    return {
        "years": dated,
        "count": len(dated),
        "first": dated[0] if dated else None,
        "last": dated[-1] if dated else None,
        "undated": "prehistory" in layers,
    }


# -------------------------------------------------------------------- the lot


def compute(conn: sqlite3.Connection) -> dict[str, object]:
    """Every figure, from scratch. Seconds, not milliseconds."""
    return {
        "ground": ground(conn),
        "routes": routes(conn),
        "points": points(conn),
        "marks": marks(conn),
        "time": years(conn),
    }


def overview(
    conn: sqlite3.Connection, *, refresh: bool = False
) -> dict[str, object]:
    """The figures, computed if the archive has moved since last time.

    The same shape of mistake as the render status recomputing its job count
    on every poll would be fatal here - reading every fog blob is three seconds
    on this archive - so the answer is written down beside a fingerprint of
    what produced it, and only recomputed when that changes.
    """
    mark = fingerprint(conn)
    if not refresh:
        stored = conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (CACHE_SETTING, FINGERPRINT_SETTING),
        ).fetchall()
        held = {row["key"]: row["value"] for row in stored}
        if held.get(FINGERPRINT_SETTING) == mark and held.get(CACHE_SETTING):
            try:
                return {**json.loads(held[CACHE_SETTING]), "cached": True}
            except json.JSONDecodeError:
                pass

    figures = compute(conn)
    for key, value in (
        (CACHE_SETTING, json.dumps(figures, separators=(",", ":"))),
        (FINGERPRINT_SETTING, mark),
    ):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return {**figures, "cached": False}
