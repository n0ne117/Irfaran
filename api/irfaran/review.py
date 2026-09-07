# SPDX-License-Identifier: AGPL-3.0-or-later
"""The holding pen: automatic data waits here until somebody says yes.

Everything else in Irfaran that adds to the map is something a person did -
dropped a file in, drew a line, pressed a button. The two automatic sources
are not: intervals.icu hands over whatever was uploaded to it, and a phone
running Overland posts wherever it has been, all day, without being asked.
Both write straight into the event log, and the event log is the truth the
whole map is rebuilt from. A wrong turn recorded by a watch, a taxi ride
nobody wants drawn, a morning that starts at the front door - all of it landed
before anyone had a chance to look at it, and taking it out afterwards means
finding the event and deleting it.

So it waits here first. A held batch is not an event and never reaches the
raster: it is the fixes themselves, kept aside with enough of a note to run
them through the ordinary ingest path later. Approving one is exactly what
would have happened automatically - `ingest_tracks` for a workout,
`live.append` for a phone - so nothing downstream has to know the pen exists,
and an install with every gate switched off behaves as it always did.

Two shapes, because the two sources are shaped differently:

  a workout is finished when it arrives. It is one batch, sealed from birth,
  and the same activity fetched twice is recognised by the key the event log
  would have used - so a re-sync while it is still waiting does not stack up
  three copies of the same ride.

  a phone never finishes. Points keep coming, so the pen keeps one unsealed
  batch per source per day and appends to it, the same way the live event
  itself would have grown. Opening one for review seals it: from that moment
  the set being looked at cannot change underneath, and anything the phone
  posts next starts a new batch. That is the whole of the locking mechanism,
  and it is one column - a review that is abandoned, or a browser that is
  closed halfway through, leaves a sealed batch waiting rather than a lock
  nobody can clear.

Edits are a note on the side, never a rewrite. The fixes are stored once and
left alone; what the review changes is a small document saying which range of
them to keep, which segments to leave out, and what to call the result. So
resetting is free, re-opening shows the same thing again, and there is no
moment where an edit has destroyed the data it was meant to be considering.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from irfaran.ingest import common, live

#: Sources that can be held back. Manual drawing and file imports are not
#: here: somebody is already looking at those when they happen.
SOURCES = ("workout", "overland", "owntracks", "ha")

#: What each one is called on screen.
LABELS = {
    "workout": "Workouts",
    "overland": "Overland",
    "owntracks": "OwnTracks",
    "ha": "Home Assistant",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS review (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,     -- workout | overland | owntracks | ha
  title       TEXT NOT NULL,
  day         TEXT NOT NULL,     -- YYYY-MM-DD, what the batch belongs to
  -- Sealed means no more points may join. A workout is born sealed; a live
  -- batch is sealed the moment somebody opens it to review.
  sealed      INTEGER NOT NULL DEFAULT 0,
  external_id TEXT,              -- the key the event log would use, for dedup
  fixes       TEXT NOT NULL,     -- JSON [[lon, lat, ISO 8601|null], ...]
  edits       TEXT,              -- JSON {from, to, dropped, title}
  meta        TEXT,              -- JSON, handed on to the ingest verbatim
  -- Everything below is derived from `fixes` and stored so that listing the
  -- pen never has to read a single batch. A day of Overland is megabytes of
  -- coordinates, the badge on the map asks every few seconds, and a list
  -- query that reads the geometry to count it is the render status
  -- recomputing its job count all over again.
  points      INTEGER NOT NULL DEFAULT 0,
  metres      REAL NOT NULL DEFAULT 0,
  first_at    TEXT,
  last_at     TEXT,
  west REAL, south REAL, east REAL, north REAL,
  arrived_at  TEXT NOT NULL
);

-- The same activity fetched twice is one batch, not two.
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_dedup
  ON review(source, external_id) WHERE external_id IS NOT NULL;

-- At most one batch per source per day is open to new points. Enforced here
-- rather than in Python because two batches posted at once would otherwise
-- both find nothing and both create one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_open
  ON review(source, day) WHERE sealed = 0;

CREATE INDEX IF NOT EXISTS idx_review_arrived ON review(arrived_at);
"""

#: Columns the list may read. Never `fixes`: see the schema comment.
SUMMARY_COLUMNS = (
    "id, source, title, day, sealed, points, metres, first_at, last_at, "
    "west, south, east, north, arrived_at, edits"
)


class ReviewError(ValueError):
    """Something wrong with a review, phrased for whoever has to act on it."""


def install(conn: sqlite3.Connection) -> None:
    """Create the pen. Safe on every startup, like the rest of the schema."""
    conn.executescript(SCHEMA)


# ------------------------------------------------------------------- the gate


def setting_key(source: str) -> str:
    return f"review_{source}"


def is_gated(conn: sqlite3.Connection, source: str) -> bool:
    """Does this source wait to be reviewed?

    Defaults to yes for anything the pen knows about. Holding data back and
    being asked about it is recoverable; the other way round means finding the
    event afterwards, and the whole point of this was that that is the part
    nobody wants to do.
    """
    if source not in SOURCES:
        return False
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (setting_key(source),)
    ).fetchone()
    return True if row is None else str(row["value"]).strip().lower() == "true"


def set_gated(conn: sqlite3.Connection, source: str, gated: bool) -> None:
    if source not in SOURCES:
        raise ReviewError(
            f"Unknown source {source!r}. Reviewable sources are "
            f"{', '.join(SOURCES)}."
        )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (setting_key(source), "true" if gated else "false"),
    )


# ------------------------------------------------------------------- encoding


def _encode(fixes: list[common.Fix]) -> str:
    """Fixes as JSON: [lon, lat, ISO 8601, accuracy, motion] per point.

    Accuracy and motion ride along because they are what the review is for:
    a coarse fix or a motion that cannot cover the ground either side of a gap
    is the difference between a stale position and a real unreported stretch.
    A field the pen drops is a field the archive never sees.

    Nothing is rounded and nothing is shortened. The gate has to be a delay
    and not a second, subtly different way into the archive, which means a
    batch accepted unchanged must write byte for byte the events an ungated
    import would - and rounding a coordinate to a centimetre, or squeezing a
    timestamp through a float, is exactly the kind of difference that would
    not show up until two supposedly identical imports disagreed.

    It costs less than it looks: Python writes the shortest string that reads
    back as the same float, so a coordinate that arrived as `16.3724514` is
    stored as `16.3724514`. Accuracy is the one thing left out - inaccurate
    fixes are dropped on the way in, so what is reviewed is what will land -
    the accuracy stored is that of a fix that already passed the filter.
    """
    return json.dumps(
        [
            [
                fix.lon,
                fix.lat,
                None
                if fix.time is None
                else fix.time.astimezone(timezone.utc).isoformat(),
                fix.accuracy,
                fix.motion,
            ]
            for fix in fixes
        ],
        separators=(",", ":"),
    )


def _decode(raw: str | None) -> list[common.Fix]:
    if not raw:
        return []
    def at(item: list, index: int) -> object | None:
        return item[index] if len(item) > index else None

    out: list[common.Fix] = []
    for item in json.loads(raw):
        when = at(item, 2)
        accuracy = at(item, 3)
        motion = at(item, 4)
        out.append(
            common.Fix(
                lon=float(item[0]),
                lat=float(item[1]),
                time=None if when is None else datetime.fromisoformat(str(when)),
                accuracy=None if accuracy is None else float(accuracy),
                motion=None if motion is None else str(motion),
            )
        )
    return out


def _stamp(fix: common.Fix) -> str | None:
    if fix.time is None:
        return None
    return fix.time.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- measures


def length_m(fixes: list[common.Fix]) -> float:
    """Ground covered, with the gaps left out.

    Summed inside segments only. A batch that includes a flight would
    otherwise report the width of a continent as distance walked.
    """
    if len(fixes) < 2:
        return 0.0
    starts = set(common.breaks(fixes))
    total = 0.0
    for index in range(1, len(fixes)):
        if index in starts:
            continue
        before, after = fixes[index - 1], fixes[index]
        total += common.haversine_m(before.lon, before.lat, after.lon, after.lat)
    return total


def _bounds(fixes: list[common.Fix]) -> tuple[float, float, float, float] | None:
    if not fixes:
        return None
    lons = [fix.lon for fix in fixes]
    lats = [fix.lat for fix in fixes]
    return min(lons), min(lats), max(lons), max(lats)


def _measured(fixes: list[common.Fix]) -> dict[str, object]:
    """The derived columns for a batch, all of them, in one place."""
    stamps = [_stamp(fix) for fix in fixes]
    dated = sorted(stamp for stamp in stamps if stamp)
    box = _bounds(fixes)
    return {
        "points": len(fixes),
        "metres": length_m(fixes),
        "first_at": dated[0] if dated else None,
        "last_at": dated[-1] if dated else None,
        "west": None if box is None else box[0],
        "south": None if box is None else box[1],
        "east": None if box is None else box[2],
        "north": None if box is None else box[3],
    }


def _today() -> str:
    """The day key a fix arriving right now would be filed under.

    UTC, because that is what the day keys are - a batch is a day of somebody's
    tracking as the server groups it, not as their calendar reads it. Asked on
    the server for the same reason: at one in the morning in Vienna the browser
    is already on tomorrow while the batch still collecting points is today's.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_of(fixes: list[common.Fix]) -> str:
    for fix in fixes:
        if fix.time is not None:
            return fix.time.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return _today()


# ------------------------------------------------------------------- the edits


#: Gaps narrower than this are not worth listing. A day of dense fixes has
#: hundreds of them and none of them is a decision.
GAP_FLOOR_M = 100.0

#: How many gaps a review will talk about, widest first.
GAP_LIMIT = 60


@dataclass
class Edits:
    """What a review decided to change, before it decided to accept it."""

    title: str = ""
    #: Inclusive index range into the whole fix list. -1 for "to the end",
    #: so a batch that grows does not silently re-trim itself.
    begin: int = 0
    end: int = -1
    #: Segments left out, named by the index of the fix each one starts at.
    #: Not by ordinal: adding a cut renumbers every segment after it, and a
    #: "leave part 3 out" that quietly became part 4 is worse than no control.
    dropped: tuple[int, ...] = ()
    #: Extra stretch boundaries a person added, where the rule saw nothing.
    cuts: tuple[int, ...] = ()
    #: Boundaries the rule found and a person disagreed with.
    joins: tuple[int, ...] = ()

    @classmethod
    def load(cls, raw: str | None) -> "Edits":
        if not raw:
            return cls()
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(stored, dict):
            return cls()

        def indexes(key: str) -> tuple[int, ...]:
            return tuple(sorted({int(item) for item in stored.get(key) or ()}))

        return cls(
            title=str(stored.get("title") or ""),
            begin=int(stored.get("from") or 0),
            end=int(stored.get("to", -1)),
            dropped=indexes("dropped"),
            cuts=indexes("cuts"),
            joins=indexes("joins"),
        )

    def dump(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "from": self.begin,
                "to": self.end,
                "dropped": list(self.dropped),
                "cuts": list(self.cuts),
                "joins": list(self.joins),
            },
            separators=(",", ":"),
        )

    @property
    def touched(self) -> bool:
        return (
            bool(self.title)
            or self.begin > 0
            or self.end >= 0
            or bool(self.dropped)
            or bool(self.cuts)
            or bool(self.joins)
        )


def detected_breaks(
    conn: sqlite3.Connection, source: str, fixes: list[common.Fix]
) -> list[int]:
    """Where the rule thinks this batch breaks, before anybody argues.

    Live sources use the live rule, which is deliberately not the file rule:
    what breaks a phone's day is that it stopped reporting, and that is not
    the same measurement as a gap in a recorded file. See live.survey.
    """
    if source in live.LIVE_SOURCES:
        return live.cut_points(fixes, live.thresholds(conn))
    return [index for index in common.breaks(fixes) if index > 0]


def effective_breaks(detected: Iterable[int], edits: Edits) -> list[int]:
    """The boundaries that count: found, plus added, minus argued away."""
    return sorted(
        (set(detected) | set(edits.cuts)) - set(edits.joins) - {0}
    )


def kept_indexes(
    fixes: list[common.Fix], edits: Edits, breaks: Iterable[int]
) -> list[int]:
    """Which fixes a review would actually add, by index.

    Segments are named by the fix they start at, so trimming the ends, leaving
    a segment out and moving a boundary are independent of each other and of
    the order they were decided in.
    """
    if not fixes:
        return []

    last = len(fixes) - 1
    begin = max(0, min(edits.begin, last))
    end = last if edits.end < 0 else max(begin, min(edits.end, last))

    if not edits.dropped:
        return list(range(begin, end + 1))

    owner = segment_of(fixes, breaks)
    return [
        index
        for index in range(begin, end + 1)
        if owner[index] not in edits.dropped
    ]


def apply_edits(
    fixes: list[common.Fix], edits: Edits, breaks: Iterable[int] = ()
) -> list[common.Fix]:
    """The fixes a review would actually add."""
    return [fixes[index] for index in kept_indexes(fixes, edits, breaks)]


def segment_of(fixes: list[common.Fix], breaks: Iterable[int]) -> list[int]:
    """Which segment each fix belongs to, named by the fix that starts it."""
    starts = {index for index in breaks if 0 < index < len(fixes)}
    owner = [0] * len(fixes)
    current = 0
    for index in range(len(fixes)):
        if index in starts:
            current = index
        owner[index] = current
    return owner


def segment_ranges(
    fixes: list[common.Fix], breaks: Iterable[int]
) -> list[tuple[int, int]]:
    """Each segment as an inclusive [first, last] index pair."""
    if not fixes:
        return []
    edges = [0, *sorted({b for b in breaks if 0 < b < len(fixes)}), len(fixes)]
    return [(begin, end - 1) for begin, end in zip(edges, edges[1:])]


def boundary_stamps(
    fixes: list[common.Fix], kept: list[int], breaks: Iterable[int]
) -> set[str]:
    """Timestamps of the fixes that start a stretch, in what will be added.

    Every boundary the review decided on, plus every hole trimming or dropping
    opened up - a batch with its middle segment left out is two stretches, and
    joining them would draw the straight line the drop was about.

    The first kept fix is deliberately not one. Whether a batch joins the
    stretch already sitting in the archive is not a decision this review made
    or could make: it is about the space between two things, only one of which
    was on screen. That call belongs to the rule, at the moment it lands.

    Timestamps rather than indexes because these have to survive the journey
    into live.append, where they are indexes into a different list.
    """
    starts = {index for index in breaks}
    out: set[str] = set()
    previous: int | None = None
    for index in kept:
        if previous is not None and (index != previous + 1 or index in starts):
            stamp = _stamp(fixes[index])
            if stamp is not None:
                out.add(stamp)
        previous = index
    return out


# --------------------------------------------------------------- holding back


@dataclass
class HoldResult:
    accepted: int = 0
    duplicates: int = 0
    dropped: int = 0
    batches: set[int] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "held": len(self.batches),
            "waiting_review": True,
        }


def hold_fixes(
    conn: sqlite3.Connection,
    source: str,
    fixes: list[common.Fix],
    meta: dict[str, object] | None = None,
) -> HoldResult:
    """Add fixes to the day's open batch, creating one if there is none.

    Deliberately the same shape as live.append, including the day grouping and
    the dedup on timestamp, because it stands in for it: a batch straddling
    midnight after an offline spell is two batches here for the same reason it
    would be two events there.
    """
    result = HoldResult()
    if not fixes:
        return result

    kept, dropped = common.drop_inaccurate(fixes)
    result.dropped = dropped
    if not kept:
        return result

    by_day: dict[str, list[common.Fix]] = {}
    for fix in kept:
        stamp = (fix.time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        by_day.setdefault(stamp.strftime("%Y-%m-%d"), []).append(fix)

    for day, day_fixes in sorted(by_day.items()):
        _hold_day(conn, source, day, day_fixes, meta or {}, result)
    return result


def _hold_day(
    conn: sqlite3.Connection,
    source: str,
    day: str,
    fixes: list[common.Fix],
    meta: dict[str, object],
    result: HoldResult,
) -> None:
    open_batch = conn.execute(
        "SELECT id, fixes, meta FROM review "
        "WHERE source = ? AND day = ? AND sealed = 0",
        (source, day),
    ).fetchone()

    existing = _decode(open_batch["fixes"]) if open_batch else []
    stored = json.loads(open_batch["meta"] or "{}") if open_batch else {}

    seen = {_stamp(fix) for fix in existing}
    before = len(existing)
    merged = list(existing)

    for fix in fixes:
        stamp = _stamp(fix)
        if stamp is not None and stamp in seen:
            result.duplicates += 1
            continue
        if stamp is not None:
            seen.add(stamp)
        merged.append(fix)

    if len(merged) == before:
        if open_batch:
            result.batches.add(int(open_batch["id"]))
        return

    # Held in time order, so a batch that arrives late after the phone has been
    # offline reads as one journey rather than a scribble back and forth.
    # Undated fixes last, so the sort never compares a datetime with None.
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    merged.sort(key=lambda fix: (fix.time is None, fix.time or far_past))
    result.accepted += len(merged) - before

    columns = _measured(merged)
    note = {**stored, **meta, "live": True}

    if open_batch is None:
        cursor = conn.execute(
            "INSERT INTO review "
            "(source, title, day, sealed, fixes, meta, points, metres, "
            " first_at, last_at, west, south, east, north, arrived_at) "
            "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source,
                f"{LABELS.get(source, source)} {day}",
                day,
                _encode(merged),
                json.dumps(note),
                columns["points"],
                columns["metres"],
                columns["first_at"],
                columns["last_at"],
                columns["west"],
                columns["south"],
                columns["east"],
                columns["north"],
                _now(),
            ),
        )
        result.batches.add(int(cursor.lastrowid))
        return

    conn.execute(
        "UPDATE review SET fixes = ?, meta = ?, points = ?, metres = ?, "
        "first_at = ?, last_at = ?, west = ?, south = ?, east = ?, north = ? "
        "WHERE id = ?",
        (
            _encode(merged),
            json.dumps(note),
            columns["points"],
            columns["metres"],
            columns["first_at"],
            columns["last_at"],
            columns["west"],
            columns["south"],
            columns["east"],
            columns["north"],
            int(open_batch["id"]),
        ),
    )
    result.batches.add(int(open_batch["id"]))


def track_key(track: common.Track) -> str | None:
    """The key the event log would give this track's first segment.

    Reused rather than invented so that "is this already here?" can be asked
    of the log and of the pen with the same string - which is what stops a
    re-sync from stacking three copies of a ride that is still waiting.
    """
    kept, _ = common.drop_inaccurate(track.fixes)
    parts = common.segment(kept)
    if not parts or not parts[0]:
        return None
    return common.external_id_for(track, parts[0], 0)


def already_known(
    conn: sqlite3.Connection, source: str, key: str | None
) -> bool:
    """Is this in the log already, or already waiting in the pen?"""
    if not key:
        return False
    in_log = conn.execute(
        "SELECT 1 FROM events WHERE source = ? AND external_id = ?", (source, key)
    ).fetchone()
    if in_log:
        return True
    return (
        conn.execute(
            "SELECT 1 FROM review WHERE source = ? AND external_id = ?",
            (source, key),
        ).fetchone()
        is not None
    )


def hold_track(
    conn: sqlite3.Connection,
    source: str,
    track: common.Track,
    meta: dict[str, object] | None = None,
) -> int | None:
    """Hold a finished track. Returns its id, or None if it is already known.

    Sealed from birth: an activity does not grow, and a workout that is being
    looked at while the tracker syncs again must not gain points halfway
    through somebody reading it.
    """
    key = track_key(track)
    if already_known(conn, source, key):
        return None

    kept, _ = common.drop_inaccurate(track.fixes)
    if not kept:
        return None

    columns = _measured(kept)
    note: dict[str, object] = {"track": track.name, **(meta or {})}
    if track.activity:
        note["activity"] = track.activity
    if track.device:
        note["device"] = track.device
    if track.source_id:
        note["source_id"] = track.source_id

    cursor = conn.execute(
        "INSERT OR IGNORE INTO review "
        "(source, title, day, sealed, external_id, fixes, meta, points, metres, "
        " first_at, last_at, west, south, east, north, arrived_at) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            track.name,
            _day_of(kept),
            key,
            _encode(kept),
            json.dumps(note),
            columns["points"],
            columns["metres"],
            columns["first_at"],
            columns["last_at"],
            columns["west"],
            columns["south"],
            columns["east"],
            columns["north"],
            _now(),
        ),
    )
    return None if cursor.rowcount == 0 else int(cursor.lastrowid)


# ------------------------------------------------------------------ reporting


def _summary(row: sqlite3.Row) -> dict[str, object]:
    edits = Edits.load(row["edits"])
    box = None
    if row["west"] is not None:
        box = [row["west"], row["south"], row["east"], row["north"]]
    return {
        "id": int(row["id"]),
        "source": row["source"],
        "label": LABELS.get(row["source"], row["source"]),
        "title": edits.title or row["title"],
        "day": row["day"],
        "sealed": bool(row["sealed"]),
        # Not the same question as "unsealed". A batch stays joinable until it
        # is opened, so a day that is over is still technically open - but
        # nothing more is coming, and saying "still collecting" about Saturday
        # on Monday is telling somebody to wait for a bus that has gone.
        "collecting": bool(not row["sealed"] and row["day"] == _today()),
        "edited": edits.touched,
        "points": int(row["points"]),
        "metres": float(row["metres"] or 0.0),
        "first_at": row["first_at"],
        "last_at": row["last_at"],
        "bounds": box,
        "arrived_at": row["arrived_at"],
    }


def waiting(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Everything in the pen, oldest first. Reads no geometry."""
    rows = conn.execute(
        f"SELECT {SUMMARY_COLUMNS} FROM review "
        "ORDER BY COALESCE(first_at, arrived_at), id"
    ).fetchall()
    return [_summary(row) for row in rows]


def overview(conn: sqlite3.Connection) -> dict[str, object]:
    """What the badge on the map needs, and what the list needs, together."""
    items = waiting(conn)
    by_source: dict[str, int] = {}
    for item in items:
        source = str(item["source"])
        by_source[source] = by_source.get(source, 0) + 1

    return {
        "count": len(items),
        "points": sum(int(item["points"]) for item in items),
        "by_source": by_source,
        "oldest": items[0]["first_at"] or items[0]["arrived_at"] if items else None,
        "gates": {source: is_gated(conn, source) for source in SOURCES},
        "items": items,
    }


def _row(conn: sqlite3.Connection, review_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        raise ReviewError(
            f"Nothing is waiting under review {review_id}. It was probably "
            "accepted or discarded in another tab."
        )
    return row


def detail(conn: sqlite3.Connection, review_id: int) -> dict[str, object]:
    """One batch, with the coordinates, for drawing on the map.

    The points go to the browser rather than a rendered geometry, because
    trimming with a slider has to redraw as the slider moves and a round trip
    per pixel is not that. The edits are applied there and here, and approving
    applies them again from the stored document - so what the map shows is a
    preview of a decision, never the decision itself.

    The gaps come with it, carrying the numbers the split was decided on. A
    threshold nobody can see the effect of is a threshold nobody can tune.
    """
    row = _row(conn, review_id)
    source = str(row["source"])
    fixes = _decode(row["fixes"])
    edits = Edits.load(row["edits"])

    found = detected_breaks(conn, source, fixes)
    breaks = effective_breaks(found, edits)
    kept = kept_indexes(fixes, edits, breaks)

    segments = []
    for number, (begin, end) in enumerate(segment_ranges(fixes, breaks)):
        part = fixes[begin : end + 1]
        dated = [stamp for stamp in (_stamp(fix) for fix in part) if stamp]
        segments.append(
            {
                "index": number,
                "begin": begin,
                "end": end,
                "points": len(part),
                "metres": length_m(part),
                "first_at": dated[0] if dated else None,
                "last_at": dated[-1] if dated else None,
                "dropped": begin in edits.dropped,
            }
        )

    return {
        **_summary(row),
        "meta": json.loads(row["meta"] or "{}"),
        "fixes": json.loads(row["fixes"]),
        "segments": segments,
        "gaps": _gaps(source, fixes, found, edits, breaks),
        "edits": {
            "title": edits.title,
            "from": edits.begin,
            "to": edits.end,
            "dropped": list(edits.dropped),
            "cuts": list(edits.cuts),
            "joins": list(edits.joins),
        },
        "keeping": len(kept),
        "keeping_metres": length_m([fixes[index] for index in kept]),
        "stretches": len(segment_ranges(fixes, breaks)),
        "thresholds": live.thresholds(conn)
        if source in live.LIVE_SOURCES
        else {},
    }


def _gaps(
    source: str,
    fixes: list[common.Fix],
    found: Iterable[int],
    edits: Edits,
    breaks: Iterable[int],
) -> list[dict[str, object]]:
    """The gaps worth talking about, in the order they happen.

    Every one that cuts, every one a person has an opinion about, and the
    widest of the rest - so a gap the rule missed can be promoted to a cut
    without hunting for it on the map. Capped, because a day of dense fixes
    has hundreds and none of the narrow ones is a decision.
    """
    if source not in live.LIVE_SOURCES:
        return []

    seen = set(found)
    opinions = set(edits.cuts) | set(edits.joins)
    cutting = set(breaks)

    rows: list[dict[str, object]] = []
    for gap in live.survey(fixes):
        interesting = (
            gap.index in seen
            or gap.index in opinions
            or gap.metres >= GAP_FLOOR_M
        )
        if not interesting:
            continue
        rows.append(
            {
                **gap.as_dict(),
                "cut": gap.index in cutting,
                "by_hand": gap.index in opinions,
            }
        )

    if len(rows) > GAP_LIMIT:
        # Widest first for the cut, then back into order, so what is dropped is
        # the narrow tail rather than the end of the day.
        keep = sorted(rows, key=lambda row: -float(row["metres"]))[:GAP_LIMIT]
        order = {id(row) for row in keep}
        rows = [row for row in rows if id(row) in order]
    return rows


# ------------------------------------------------------------------- deciding


def open_for_review(conn: sqlite3.Connection, review_id: int) -> dict[str, object]:
    """Seal a batch so the set being looked at cannot change underneath.

    Idempotent, and never reversible: unsealing would mean points joining a
    batch somebody has already trimmed by index, which would move the trim
    onto different points than the ones it was aimed at.
    """
    row = _row(conn, review_id)
    if not row["sealed"]:
        conn.execute("UPDATE review SET sealed = 1 WHERE id = ?", (review_id,))
    return detail(conn, review_id)


def edit(
    conn: sqlite3.Connection,
    review_id: int,
    *,
    title: str | None = None,
    begin: int | None = None,
    end: int | None = None,
    dropped: Iterable[int] | None = None,
    cuts: Iterable[int] | None = None,
    joins: Iterable[int] | None = None,
) -> dict[str, object]:
    """Change what approving would add. Nothing enters the log here."""
    row = _row(conn, review_id)
    total = int(row["points"])
    current = Edits.load(row["edits"])

    if title is not None:
        cleaned = " ".join(str(title).split())
        if len(cleaned) > 200:
            raise ReviewError("A name that long is not a name. Keep it under 200.")
        current.title = cleaned

    if begin is not None:
        current.begin = _index(begin, total, "The start of the trim")
    if end is not None:
        current.end = -1 if int(end) < 0 else _index(end, total, "The end of the trim")
    if current.end >= 0 and current.end < current.begin:
        raise ReviewError(
            "The trim ends before it starts. Drag the two handles back past "
            "each other."
        )

    fixes = _decode(row["fixes"])
    found = detected_breaks(conn, str(row["source"]), fixes)

    if cuts is not None:
        current.cuts = _boundaries(cuts, total, "cut")
    if joins is not None:
        current.joins = _boundaries(joins, total, "join")

    if dropped is not None:
        starts = {begin for begin, _ in segment_ranges(
            fixes, effective_breaks(found, current)
        )}
        chosen = sorted({int(item) for item in dropped})
        for index in chosen:
            if index not in starts:
                raise ReviewError(
                    f"No part of this batch starts at point {index}. Reload the "
                    "review - the parts moved when a cut did."
                )
        current.dropped = tuple(chosen)

    # A part that no longer starts anywhere cannot stay left out: moving a
    # boundary would otherwise leave a drop pointing at nothing, and nothing
    # on screen would say so.
    starts = {begin for begin, _ in segment_ranges(
        fixes, effective_breaks(found, current)
    )}
    current.dropped = tuple(index for index in current.dropped if index in starts)

    conn.execute(
        "UPDATE review SET edits = ? WHERE id = ?", (current.dump(), review_id)
    )
    return detail(conn, review_id)


def _boundaries(values: Iterable[int], total: int, what: str) -> tuple[int, ...]:
    """Fix indexes that may carry a boundary: anything but the first point."""
    out: set[int] = set()
    for value in values:
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise ReviewError(f"A {what} is a point number.") from exc
        if not 0 < index < max(total, 1):
            raise ReviewError(
                f"Point {index} cannot carry a {what} - this batch has "
                f"{total} points, and a stretch cannot start before the first."
            )
        out.add(index)
    return tuple(sorted(out))


def _index(value: object, total: int, what: str) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ReviewError(f"{what} must be a whole number.") from exc
    if total and not 0 <= number < total:
        raise ReviewError(
            f"{what} is point {number}, and this batch has {total}."
        )
    return max(0, number)


def reset(conn: sqlite3.Connection, review_id: int) -> dict[str, object]:
    """Forget every edit. The fixes were never touched, so this is a delete."""
    _row(conn, review_id)
    conn.execute("UPDATE review SET edits = NULL WHERE id = ?", (review_id,))
    return detail(conn, review_id)


@dataclass
class Decision:
    """What approving a batch did, in the terms the caller has to act on."""

    review_id: int = 0
    source: str = ""
    title: str = ""
    events: int = 0
    skipped: int = 0
    points: int = 0
    left_out: int = 0
    #: How many continuous stretches it went in as. More than one means the
    #: phone stopped reporting somewhere in the middle.
    stretches: int = 0
    tiles: set[tuple[int, int]] = field(default_factory=set)
    views: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.review_id,
            "source": self.source,
            "title": self.title,
            "events": self.events,
            "skipped": self.skipped,
            "points": self.points,
            "left_out": self.left_out,
            "stretches": self.stretches,
            "tiles_touched": len(self.tiles),
            "views": self.views,
        }

    def summary(self) -> str:
        text = f"{self.title}: {self.points} " + (
            "point" if self.points == 1 else "points"
        )
        if self.left_out:
            text += f", {self.left_out} left out"
        return text


def approve(conn: sqlite3.Connection, review_id: int) -> Decision:
    """Let a batch through the ordinary door, edits and all.

    Nothing here writes an event by hand. A workout goes through
    ingest_tracks and a phone through live.append, exactly as they would have
    without the pen - which is what makes the gate a delay rather than a
    second, subtly different, way into the archive.
    """
    row = _row(conn, review_id)
    source = str(row["source"])
    fixes = _decode(row["fixes"])
    edits = Edits.load(row["edits"])
    breaks = effective_breaks(detected_breaks(conn, source, fixes), edits)
    keeping = kept_indexes(fixes, edits, breaks)
    kept = [fixes[index] for index in keeping]

    if not kept:
        raise ReviewError(
            "Nothing is left to add - the trim and the parts left out cover "
            "the whole batch. Discard it instead."
        )

    decision = Decision(
        review_id=review_id,
        source=source,
        title=edits.title or str(row["title"]),
        points=len(kept),
        left_out=len(fixes) - len(kept),
    )
    meta = json.loads(row["meta"] or "{}")

    if source in live.LIVE_SOURCES:
        # The live path owns its own dedup and re-stamps the day's line, so
        # approving twice - or approving a second batch for a day that already
        # has one - merges rather than duplicating.
        outcome = live.append(
            conn,
            source,
            kept,
            meta,
            breaks_at=boundary_stamps(fixes, keeping, breaks),
        )
        decision.events = outcome.stretches or (1 if outcome.accepted else 0)
        decision.stretches = outcome.stretches
        decision.tiles = set(outcome.tiles_touched)
        decision.views = _views_of_event(conn, outcome.event_id)
    else:
        track = common.Track(
            name=decision.title,
            fixes=kept,
            activity=str(meta.get("activity") or "") or None,
            device=str(meta.get("device") or "") or None,
            source_id=str(meta.get("source_id") or "") or None,
        )
        outcome = common.ingest_tracks(conn, source, [track])
        decision.events = outcome.events_created
        decision.skipped = outcome.events_skipped
        decision.tiles = set(outcome.tiles_touched)
        decision.views = outcome.affected_views()

    conn.execute("DELETE FROM review WHERE id = ?", (review_id,))
    return decision


def _views_of_event(conn: sqlite3.Connection, event_id: int | None) -> list[str]:
    if event_id is None:
        return ["all"]
    row = conn.execute(
        "SELECT layers FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    layers = json.loads(row["layers"]) if row else []
    views = ["all"]
    views += sorted(f"year:{layer}" for layer in layers if str(layer).isdigit())
    if common.PREHISTORY in layers:
        views.append(common.PREHISTORY)
    return views


def discard(conn: sqlite3.Connection, review_id: int) -> dict[str, object]:
    """Throw a batch away. It never became an event, so there is nothing else
    to undo - and nothing to find and delete later, which was the point."""
    row = _row(conn, review_id)
    summary = _summary(row)
    conn.execute("DELETE FROM review WHERE id = ?", (review_id,))
    return summary
