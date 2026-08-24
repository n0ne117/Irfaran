# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live tracking sources.

Overland, OwnTracks and Home Assistant all post single fixes or small batches
rather than files. One event per fix would give thousands of rows a day and a
map made of dots, so fixes append to a same-day open track per source: one
event, growing, with the points held in time order.

Every source is opt-in and off by default. Irfaran is entirely usable with none
of them configured - these are optional data sources, not dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import AbstractSet as Set, Sequence

from irfaran import raster
from irfaran.ingest.common import (
    Fix,
    haversine_m,
    RADIUS_DEFAULTS_M,
    drop_inaccurate,
    layer_for,
)

LIVE_SOURCES = ("ha", "overland", "owntracks")


class LiveError(ValueError):
    """Bad payload, phrased for whoever has to configure the tracker."""


@dataclass
class LiveResult:
    accepted: int = 0
    duplicates: int = 0
    dropped: int = 0
    event_id: int | None = None
    #: How many continuous stretches the day now holds. More than one means the
    #: phone stopped reporting and started again somewhere else.
    stretches: int = 0
    tiles_touched: set[tuple[int, int]] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "event_id": self.event_id,
            "stretches": self.stretches,
            "tiles_touched": len(self.tiles_touched),
        }


def setting_key(source: str) -> str:
    return f"{source}_ingest_enabled"


def is_enabled(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (setting_key(source),)
    ).fetchone()
    return bool(row) and str(row["value"]).strip().lower() == "true"


def set_enabled(conn: sqlite3.Connection, source: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (setting_key(source), "true" if enabled else "false"),
    )


def has_events(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE source = ? LIMIT 1", (source,)
    ).fetchone()
    return row is not None


# -- parsing -----------------------------------------------------------------


def _timestamp(value: object, what: str) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    text = str(value or "").strip()
    if not text:
        raise LiveError(f"{what} is missing a timestamp.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveError(
            f"{what} has an unreadable timestamp {text!r}. Expected ISO 8601 "
            "or unix seconds."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coordinate(value: object, what: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LiveError(f"{what} is not a number, got {value!r}.") from exc


def parse_overland(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """Overland posts {"locations": [GeoJSON Feature, ...]}.

    Batches arrive out of order after the phone has been offline, so nothing
    here assumes the points are chronological.
    """
    if not isinstance(payload, dict) or "locations" not in payload:
        raise LiveError(
            'Overland payloads look like {"locations": [...]}. '
            f"Got {type(payload).__name__}."
        )

    locations = payload.get("locations")
    if not isinstance(locations, list):
        raise LiveError('"locations" must be a list of GeoJSON features.')

    fixes: list[Fix] = []
    motions: set[str] = set()
    devices: set[str] = set()

    for index, feature in enumerate(locations, start=1):
        if not isinstance(feature, dict):
            raise LiveError(f"Overland location {index} is not an object.")

        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
            raise LiveError(
                f"Overland location {index} has no [lon, lat] coordinates."
            )

        properties = feature.get("properties") or {}
        accuracy = properties.get("horizontal_accuracy")

        fixes.append(
            Fix(
                lon=_coordinate(coordinates[0], f"Overland location {index} longitude"),
                lat=_coordinate(coordinates[1], f"Overland location {index} latitude"),
                time=_timestamp(
                    properties.get("timestamp"), f"Overland location {index}"
                ),
                accuracy=None if accuracy is None else _coordinate(
                    accuracy, f"Overland location {index} accuracy"
                ),
            )
        )

        motion = properties.get("motion")
        if isinstance(motion, list):
            motions.update(str(item) for item in motion)
        elif motion:
            motions.add(str(motion))

        if properties.get("device_id"):
            devices.add(str(properties["device_id"]))

    meta: dict[str, object] = {}
    if motions:
        meta["motion"] = sorted(motions)
    if devices:
        meta["device"] = sorted(devices)
    return fixes, meta


def parse_owntracks(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """OwnTracks posts one object per request.

    Anything that is not a location report is ignored rather than refused -
    the app sends several message types down the same endpoint.
    """
    if payload is None or payload == "" or payload == {}:
        # OwnTracks posts an empty body when a friend is deleted. Not an error.
        return [], {}

    if not isinstance(payload, dict):
        raise LiveError(
            f"OwnTracks payloads are single JSON objects, got "
            f"{type(payload).__name__}."
        )

    if payload.get("_type") != "location":
        return [], {}

    fix = Fix(
        lon=_coordinate(payload.get("lon"), "OwnTracks longitude"),
        lat=_coordinate(payload.get("lat"), "OwnTracks latitude"),
        time=_timestamp(payload.get("tst"), "OwnTracks report"),
        accuracy=None
        if payload.get("acc") is None
        else _coordinate(payload.get("acc"), "OwnTracks accuracy"),
    )

    meta: dict[str, object] = {}
    headers = headers or {}
    device = headers.get("x-limit-d") or payload.get("tid")
    user = headers.get("x-limit-u")
    if device:
        meta["device"] = str(device)
    if user:
        meta["user"] = str(user)
    return [fix], meta


def parse_ha(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """Home Assistant posts {lat, lon, accuracy, timestamp, device}."""
    if not isinstance(payload, dict):
        raise LiveError(
            f"Home Assistant payloads are JSON objects, got "
            f"{type(payload).__name__}."
        )

    accuracy = payload.get("accuracy")
    fix = Fix(
        lon=_coordinate(payload.get("lon"), "Home Assistant longitude"),
        lat=_coordinate(payload.get("lat"), "Home Assistant latitude"),
        time=_timestamp(payload.get("timestamp"), "Home Assistant report"),
        accuracy=None
        if accuracy in (None, "", "unknown")
        else _coordinate(accuracy, "Home Assistant accuracy"),
    )

    meta: dict[str, object] = {}
    if payload.get("device"):
        meta["device"] = str(payload["device"])
    return [fix], meta


PARSERS = {
    "overland": parse_overland,
    "owntracks": parse_owntracks,
    "ha": parse_ha,
}


# -- where a day breaks ------------------------------------------------------
#
# A day of live tracking is not one journey. A phone stops reporting - iOS
# suspends the app, a tunnel, a dead battery - and starts again somewhere else,
# and joining the two ends with a straight line claims a route nobody took.
# Worse than claiming it: the raster clears a fog corridor along it.
#
# The file importers have always split on this. `common.segment` cuts wherever
# the trace jumps, and its own comment says why. The live path never called it,
# so every day since the beginning has been stored as one unbroken line.
#
# The rule here is not `common.segment`, because live data needs different
# thresholds and measurement says so:
#
#   distance alone misses the case that prompted this. Driving through a town,
#   reporting pauses for forty seconds and resumes six hundred metres later -
#   under any distance threshold that does not also cut a train sampling every
#   eleven seconds at 130 km/h, which covers four hundred metres per fix
#   legitimately.
#
#   speed cannot separate them either. Measured on a real day, the 4.9 km
#   invented line implied 118 km/h and twenty-eight perfectly good fixes
#   implied more.
#
#   what does separate them is that the phone stopped reporting. A gap many
#   times longer than the interval it had just been reporting at is a pause,
#   whatever distance it covers - and combined with a distance floor it flagged
#   every invented line in the sample and none of the 92 fast-but-real gaps,
#   nor any of the 15 stationary pauses where nothing moved.
#
# All four numbers are settings rather than constants, because they were chosen
# from one archive and the next one will disagree.

#: Cut when two consecutive fixes are this far apart, whatever the timing.
SETTING_METRES = "live_split_metres"

#: Cut on elapsed time alone. Off by default: on a real day this made nine cuts
#: covering eighty-six metres in total - a phone sitting on a desk. Splitting
#: there costs events and buys nothing, because nothing moved.
SETTING_SECONDS = "live_split_seconds"

#: Cut when the silence is this many times longer than the interval the phone
#: had just been reporting at.
#:
#: The default is 2.5, chosen from a measured distribution rather than picked.
#: Across 92 gaps over 250 m in a real archive, ninety of them - a train
#: sampling every eleven seconds at 130 km/h - sat between 0.91x and 1.10x,
#: and the two invented lines sat at 6.91x and 13.45x. Nothing at all fell
#: between. 2.5 is more than double the highest legitimate reading and well
#: under the lowest bad one, and it catches the case that prompted this: a
#: drive through a town where reporting paused for forty seconds and resumed
#: six hundred metres later, which measures 3.6x.
SETTING_RATIO = "live_split_ratio"

#: ...and only when it covers at least this far, so a pause while standing
#: still is not a cut.
SETTING_RATIO_METRES = "live_split_ratio_metres"

SPLIT_DEFAULTS: dict[str, float] = {
    SETTING_METRES: 1000.0,
    SETTING_SECONDS: 0.0,
    SETTING_RATIO: 2.5,
    SETTING_RATIO_METRES: 250.0,
}

#: How many previous intervals the local reporting rate is measured over.
#: Median rather than mean, so one long pause does not raise the bar for the
#: pause right after it.
RATE_WINDOW = 10


@dataclass(frozen=True)
class Gap:
    """The space between two consecutive fixes, and what it looks like."""

    #: Index of the later fix - the one that would start a new stretch.
    index: int
    metres: float
    seconds: float | None
    #: The median interval over the preceding fixes, for scale.
    local: float | None
    #: How many times the local interval this gap took.
    ratio: float | None
    #: Why it is a cut: distance, time, rate - or "" if it is not one.
    reason: str = ""

    @property
    def cut(self) -> bool:
        return bool(self.reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "metres": self.metres,
            "seconds": self.seconds,
            "ratio": self.ratio,
            "reason": self.reason,
        }


def thresholds(conn: sqlite3.Connection) -> dict[str, float]:
    """The four numbers, from settings, falling back to the defaults."""
    limits = dict(SPLIT_DEFAULTS)
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        f"({', '.join('?' * len(SPLIT_DEFAULTS))})",
        tuple(SPLIT_DEFAULTS),
    ).fetchall()
    for row in rows:
        try:
            limits[str(row["key"])] = max(0.0, float(row["value"]))
        except (TypeError, ValueError):
            # A setting somebody mistyped must not stop a phone delivering.
            continue
    return limits


def survey(fixes: list[Fix], limits: dict[str, float] | None = None) -> list[Gap]:
    """Every gap in a run of fixes, marked with whether it cuts and why.

    One pass, so the review can show the same numbers the split was decided
    on - which is the only way tuning the thresholds is possible rather than
    guesswork.
    """
    limits = limits or dict(SPLIT_DEFAULTS)
    if len(fixes) < 2:
        return []

    metres_cut = limits.get(SETTING_METRES, 0.0)
    seconds_cut = limits.get(SETTING_SECONDS, 0.0)
    ratio_cut = limits.get(SETTING_RATIO, 0.0)
    ratio_metres = limits.get(SETTING_RATIO_METRES, 0.0)

    out: list[Gap] = []
    intervals: list[float] = []

    for index in range(1, len(fixes)):
        before, after = fixes[index - 1], fixes[index]
        metres = haversine_m(before.lon, before.lat, after.lon, after.lat)
        seconds: float | None = None
        if before.time is not None and after.time is not None:
            seconds = abs((after.time - before.time).total_seconds())

        local: float | None = None
        ratio: float | None = None
        if seconds is not None and intervals:
            local = median(intervals[-RATE_WINDOW:])
            if local > 0:
                ratio = seconds / local

        reason = ""
        if metres_cut and metres > metres_cut:
            reason = "distance"
        elif seconds_cut and seconds is not None and seconds > seconds_cut:
            reason = "time"
        elif (
            ratio_cut
            and ratio is not None
            and ratio >= ratio_cut
            and metres > ratio_metres
        ):
            reason = "rate"

        out.append(Gap(index, metres, seconds, local, ratio, reason))
        if seconds is not None and seconds > 0:
            intervals.append(seconds)

    return out


def cut_points(fixes: list[Fix], limits: dict[str, float] | None = None) -> list[int]:
    """Indexes where a new stretch starts. Never 0."""
    return [gap.index for gap in survey(fixes, limits) if gap.cut]


def stretches(count: int, breaks: Sequence[int]) -> list[tuple[int, int]]:
    """Inclusive [first, last] index pairs for each continuous stretch."""
    if count <= 0:
        return []
    edges = [0, *sorted({b for b in breaks if 0 < b < count}), count]
    return [(begin, end - 1) for begin, end in zip(edges, edges[1:])]


# -- the open track ----------------------------------------------------------


def track_id(source: str, day: str, ordinal: int = 1) -> str:
    """The dedup key for one continuous stretch of a day.

    The first stretch keeps the key a whole day used to have, so every event
    written before days were split is already stretch one of its day and needs
    no migration - and the second stretch of the same day is `#2`.
    """
    return f"live-{day}" if ordinal <= 1 else f"live-{day}#{ordinal}"


def _ordinal_of(external_id: str, day: str) -> int:
    marker = f"live-{day}#"
    if external_id.startswith(marker):
        try:
            return int(external_id[len(marker) :])
        except ValueError:
            return 1
    return 1


def _day_events(
    conn: sqlite3.Connection, source: str, day: str
) -> list[sqlite3.Row]:
    """This day's stretches, in the order they happened."""
    rows = conn.execute(
        "SELECT * FROM events WHERE source = ? AND op = 'add' "
        "AND (external_id = ? OR external_id LIKE ?)",
        (source, track_id(source, day), f"live-{day}#%"),
    ).fetchall()
    return sorted(rows, key=lambda row: _ordinal_of(str(row["external_id"]), day))


def _points_of(row: sqlite3.Row) -> list[tuple[str, float, float]]:
    """A stretch's fixes as (timestamp, lon, lat), in stored order."""
    stored = json.loads(row["meta"] or "{}")
    # The first fix of a stretch is stored as a Point and everything after it
    # as a LineString, so read it back through the helper that knows both.
    coordinates = raster.geometry_points(row["geometry"], int(row["id"]))
    stamps = stored.get("timestamps") or []
    return [
        (stamp, lon, lat) for stamp, (lon, lat) in zip(stamps, coordinates)
    ]


def _geometry_of(points: list[tuple[str, float, float]]) -> str:
    if len(points) == 1:
        return json.dumps(
            {"type": "Point", "coordinates": [points[0][1], points[0][2]]}
        )
    return json.dumps(
        {
            "type": "LineString",
            "coordinates": [[lon, lat] for _, lon, lat in points],
        }
    )


def _as_fixes(points: list[tuple[str, float, float]]) -> list[Fix]:
    out: list[Fix] = []
    for stamp, lon, lat in points:
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            when = None
        out.append(Fix(lon=lon, lat=lat, time=when))
    return out


def _append_day(
    conn: sqlite3.Connection,
    source: str,
    day: str,
    fixes: list[Fix],
    meta: dict[str, object],
    radius_m: float,
    result: LiveResult,
    breaks_at: Set[str] | None = None,
) -> set[tuple[int, int]]:
    """Add fixes to a day, extending its last stretch or starting new ones.

    History is never re-split. The day's earlier stretches are left exactly as
    they are and only the join to the last one is judged - which matters
    because a stretch boundary can be somebody's decision. Somebody who joined
    two stretches back together in a review would otherwise have that undone by
    the next batch the phone delivered.

    The cost of that is a late fix arriving in the middle of an old gap does
    not merge the stretches either side of it. That is what the rejoin control
    is for, and it is a better trade than silently overruling a person.
    """
    day_events = _day_events(conn, source, day)
    open_row = day_events[-1] if day_events else None

    # Dedup against the whole day, so replaying a batch changes nothing even
    # when its points belong to a stretch that is no longer the open one.
    seen: set[str] = set()
    for row in day_events:
        seen.update(stamp for stamp, _, _ in _points_of(row))

    fresh: list[tuple[str, float, float]] = []
    for fix in fixes:
        stamp = (
            (fix.time or datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .isoformat(timespec="seconds")
        )
        if stamp in seen:
            result.duplicates += 1
            continue
        seen.add(stamp)
        fresh.append((stamp, fix.lon, fix.lat))

    if not fresh:
        return set()

    result.accepted += len(fresh)

    held = _points_of(open_row) if open_row is not None else []
    combined = [*held, *fresh]
    combined.sort(key=lambda item: item[0])

    # Only breaks at or after the boundary count: anything earlier is history,
    # and history is not re-split.
    floor = max(1, len(held))
    if breaks_at is None:
        found = cut_points(_as_fixes(combined), thresholds(conn))
    else:
        # Decided by a person in a review, and carried by timestamp rather than
        # by index: the indexes they were decided against are indexes into the
        # batch, and by the time it lands the batch may be sitting behind a
        # stretch that is already here.
        found = [
            index
            for index, (stamp, _, _) in enumerate(combined)
            if index > 0 and stamp in breaks_at
        ]
        # ...but the join to what is already here is still the rule's call. A
        # review looked at one batch; whether it continues the stretch in the
        # archive is about the space between two things, only one of which was
        # on screen.
        if held and len(held) in cut_points(_as_fixes(combined), thresholds(conn)):
            found.append(len(held))
    cuts = [index for index in found if index >= floor]

    pieces = stretches(len(combined), cuts)
    layers = json.dumps([layer_for([Fix(lon=0.0, lat=0.0, time=_parse_day(day))])])
    stored = json.loads(open_row["meta"] or "{}") if open_row is not None else {}

    touched: set[tuple[int, int]] = set()
    if open_row is not None:
        # Whatever ground it covered before, so a stretch that shrinks - the
        # first piece no longer reaching as far - has its old corridor rebuilt.
        touched |= raster.event_tiles(open_row)

    next_ordinal = (
        _ordinal_of(str(open_row["external_id"]), day) if open_row is not None else 0
    )

    for number, (begin, end) in enumerate(pieces):
        points = combined[begin : end + 1]
        note: dict[str, object] = {**stored, **meta} if number == 0 else dict(meta)
        note["timestamps"] = [stamp for stamp, _, _ in points]
        note["live"] = True
        note["fixes"] = len(points)

        if number == 0 and open_row is not None:
            event_id = int(open_row["id"])
            conn.execute(
                "UPDATE events SET geometry = ?, meta = ?, layers = ? WHERE id = ?",
                (_geometry_of(points), json.dumps(note), layers, event_id),
            )
        else:
            next_ordinal += 1
            cursor = conn.execute(
                "INSERT INTO events "
                "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
                "VALUES (?, 'add', ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    _geometry_of(points),
                    radius_m,
                    layers,
                    track_id(source, day, next_ordinal),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    json.dumps(note),
                ),
            )
            event_id = int(cursor.lastrowid)

        result.event_id = event_id
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        touched |= raster.event_tiles(row)

    result.stretches = len(pieces)

    # Rebuilt from the event log rather than painted on top. Adding would
    # inflate the trail count on every batch and drift away from what a full
    # rebuild produces, which invariant 1 does not allow - and a stretch that
    # was just split has to lose the corridor its straight line used to clear.
    raster.rebuild_tiles(conn, touched)
    return touched


def append(
    conn: sqlite3.Connection,
    source: str,
    fixes: list[Fix],
    meta: dict[str, object] | None = None,
    radius_m: float | None = None,
    breaks_at: Set[str] | None = None,
) -> LiveResult:
    """Add fixes to a day, as one event per continuous stretch of it.

    Points are held in time order and deduplicated on their timestamp, so a
    batch replayed after an offline spell changes nothing and a batch that
    arrives late is inserted where it belongs rather than appended to the end.

    A day is not one journey. Where the phone stopped reporting and started
    again somewhere else, the day is cut - because joining the two ends with a
    straight line claims a route nobody took and clears a fog corridor along
    it. See the notes above `survey` for how that is decided.

    `breaks_at` overrides the decision with the timestamps a person chose in a
    review, which is what makes the manual cut and rejoin controls stick.
    """
    if source not in LIVE_SOURCES:
        raise LiveError(
            f"Unknown live source {source!r}. Valid sources are "
            f"{', '.join(LIVE_SOURCES)}."
        )

    result = LiveResult()
    if not fixes:
        return result

    kept, dropped = drop_inaccurate(fixes)
    result.dropped = dropped
    if not kept:
        return result

    radius = RADIUS_DEFAULTS_M[source] if radius_m is None else radius_m

    # A batch can straddle midnight after an offline spell, so group by day
    # rather than assuming one batch belongs to one track.
    by_day: dict[str, list[Fix]] = {}
    for fix in kept:
        stamp = (fix.time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        by_day.setdefault(stamp.strftime("%Y-%m-%d"), []).append(fix)

    for day, day_fixes in sorted(by_day.items()):
        result.tiles_touched |= _append_day(
            conn, source, day, day_fixes, meta or {}, radius, result, breaks_at
        )
    return result


def _parse_day(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
