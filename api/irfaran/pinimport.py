# SPDX-License-Identifier: AGPL-3.0-or-later
"""Importing pins out of another application's database.

A one-off, and deliberately not advertised. There is nothing in the interface
that offers this: the file picker under Import accepts the file, the importer
recognises it by its shape rather than its name, and everything else about it
lives in the release notes. Somebody who has a database of pins somewhere else
and reads the changelog can use it; nobody else is invited to.

The file it reads is a small SQLite database with two tables - `places` with a
name, a latitude, a longitude and a category, and `categories` with a name and
a colour. Recognition is by those columns, so a database that happens to be
called the right thing but is not one is refused with a reason rather than
half-imported.

What it does with them is the interesting part, and none of it is guessed:

  the category is who was there. In the file that prompted this, the seven
  categories were people and pairs of people rather than kinds of place - so a
  category is split on "&", "and" and commas, and each part matched against
  the people registry. A category naming one person assigns that person; a
  category naming two assigns both. Nothing is invented: a part that matches
  nobody in the registry is dropped and reported, because silently attaching a
  name that does not exist is worse than attaching none.

  a category that names no people at all can be mapped by hand. "Family" is
  not a person and never will be, so `pin_import_people` in the settings holds
  a map from category name to the people it means. Kept as a setting rather
  than in this file because the answer is somebody's own family, and this file
  is public.

  the colours are dropped. In the file that prompted this every pin carried
  the same default colour and the real colour lived on the category - and a
  category is being read as people here, not as a label. Imported pins get no
  label; setting one is a decision for whoever reviews them.

  the dates are dropped. The only date in the file is when the pin was typed
  in, which is not when anybody was there, so filing pins under it would claim
  a decade of travel happened over one spring. They land in prehistory, where
  undated things belong.

Nothing imported is a pin yet. It waits in `place_import` until somebody has
looked at it - a pin is a claim about somewhere you were, and three hundred of
them arriving unexamined is exactly the thing 0.18.0 was about. Saving one
runs `places.create`, the same call the sidebar makes, so a reviewed pin is
indistinguishable from one dropped by hand.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from irfaran import places

#: Pins arrive minor. Three hundred major pins is a wall of markers at every
#: zoom from z7 out; minor ones drop away and leave the map readable. Changed
#: per pin while reviewing, which is where the decision belongs.
DEFAULT_PROMINENCE = "minor"

#: Category name to the people it means, as JSON, for categories that name no
#: person. Lives in settings because the value is somebody's own family.
PEOPLE_SETTING = "pin_import_people"

#: What separates two people in one category name.
SPLIT = re.compile(r"\s*(?:&|\+|,|\band\b|\bund\b)\s*", re.IGNORECASE)

#: A sane ceiling. Anything larger is not the file this was written for, and
#: three hundred reviews is already a long sitting.
MAX_PINS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS place_import (
  id          INTEGER PRIMARY KEY,
  source_id   INTEGER,           -- the id it had in the file it came from
  name        TEXT NOT NULL,
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  category    TEXT,              -- what the file called it, kept for the review
  people      TEXT NOT NULL,     -- JSON array of names
  label_id    INTEGER REFERENCES labels(id) ON DELETE SET NULL,
  prominence  TEXT NOT NULL DEFAULT 'minor',
  -- waiting | saved | discarded. Saved and discarded rows stay listed so the
  -- sidebar can show what has been dealt with; Done clears the lot.
  state       TEXT NOT NULL DEFAULT 'waiting',
  place_id    INTEGER,           -- what it became, once saved
  imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_place_import_state ON place_import(state);
"""

WAITING, SAVED, DISCARDED = "waiting", "saved", "discarded"


class PinImportError(ValueError):
    """Something wrong with the file or the request, phrased for a person."""


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ------------------------------------------------------------------- the file


@dataclass
class Incoming:
    """One row of the file, before anything has been decided about it."""

    source_id: int
    name: str
    lat: float
    lon: float
    category: str = ""


REQUIRED = {
    "places": {"id", "name", "lat", "lng"},
    "categories": {"id", "name"},
}


def read(blob: bytes) -> list[Incoming]:
    """Rows out of another application's places database.

    Written to a temporary file because SQLite reads files, not buffers, and
    opened read-only: this is somebody's other application's data and there is
    no version of this that should write to it.
    """
    if not blob:
        raise PinImportError("That file is empty.")
    if blob[:16] != b"SQLite format 3\x00":
        raise PinImportError(
            "That is not a SQLite database. This importer reads a places "
            "database from another application - a file whose first bytes say "
            "'SQLite format 3'."
        )

    with tempfile.TemporaryDirectory(prefix="irfaran-pins-") as directory:
        path = Path(directory) / "incoming.db"
        path.write_bytes(blob)
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            _check(source)
            rows = source.execute(
                "SELECT p.id AS id, p.name AS name, p.lat AS lat, p.lng AS lng, "
                "       c.name AS category "
                "FROM places p LEFT JOIN categories c ON c.id = p.category_id "
                "ORDER BY p.id"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PinImportError(f"That database could not be read ({exc}).") from exc
        finally:
            source.close()

    if len(rows) > MAX_PINS:
        raise PinImportError(
            f"That file holds {len(rows):,} pins, and this importer stops at "
            f"{MAX_PINS:,}. Every one of them has to be reviewed by hand."
        )

    return [item for item in (_one(row) for row in rows) if item is not None]


def _check(source: sqlite3.Connection) -> None:
    present = {
        row["name"]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    for table, columns in REQUIRED.items():
        if table not in present:
            raise PinImportError(
                f"That database has no {table} table, so it is not a places "
                "database this importer knows. It expects `places` with name, "
                "lat, lng and category_id, and `categories` with a name."
            )
        found = {row["name"] for row in source.execute(f"PRAGMA table_info({table})")}
        missing = sorted(columns - found)
        if missing:
            raise PinImportError(
                f"The {table} table is missing {', '.join(missing)}. This "
                "importer reads places with name, lat and lng, categorised by "
                "category_id."
            )


def _one(row: sqlite3.Row) -> Incoming | None:
    """One row, or None if it is not usable. Never raises on bad data.

    A single unreadable row must not refuse the whole file: three hundred good
    pins are worth having, and what was dropped is reported.
    """
    name = str(row["name"] or "").strip()
    if not name:
        return None
    try:
        lat, lon = float(row["lat"]), float(row["lng"])
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return Incoming(
        source_id=int(row["id"]),
        name=name,
        lat=lat,
        lon=lon,
        category=str(row["category"] or "").strip(),
    )


# ----------------------------------------------------------------- the people


def registry(conn: sqlite3.Connection) -> dict[str, str]:
    """Everyone Irfaran knows, by casefolded name."""
    return {
        str(row["name"]).strip().casefold(): str(row["name"]).strip()
        for row in conn.execute("SELECT name FROM people")
    }


def overrides(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Category names that mean people but do not name them."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (PEOPLE_SETTING,)
    ).fetchone()
    if not row:
        return {}
    try:
        stored = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return {}
    if not isinstance(stored, dict):
        return {}

    out: dict[str, list[str]] = {}
    for key, value in stored.items():
        names = value if isinstance(value, list) else [value]
        out[str(key).strip().casefold()] = [
            str(name).strip() for name in names if str(name).strip()
        ]
    return out


def people_for(
    category: str, known: dict[str, str], mapped: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    """Who a category means, and which parts of it named nobody.

    An override wins outright - that is the whole point of having one. Failing
    that the name is split and each part looked up, because "Andrea & Alex" is
    two people and "Alex" is one, and nothing else about a category name is
    guessable.
    """
    if not category:
        return [], []

    override = mapped.get(category.strip().casefold())
    if override is not None:
        return [known.get(name.casefold(), name) for name in override], []

    found: list[str] = []
    unknown: list[str] = []
    for part in SPLIT.split(category):
        part = part.strip()
        if not part:
            continue
        match = known.get(part.casefold())
        if match is None:
            unknown.append(part)
        elif match not in found:
            found.append(match)
    return found, unknown


# ----------------------------------------------------------------- staging


@dataclass
class Staged:
    read_in: int = 0
    staged: int = 0
    already_here: list[str] = field(default_factory=list)
    collapsed: list[str] = field(default_factory=list)
    unreadable: int = 0
    #: Category names that matched nobody, so their pins arrived with no people.
    unmatched: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "read": self.read_in,
            "staged": self.staged,
            "already_here": sorted(set(self.already_here)),
            "collapsed": sorted(set(self.collapsed)),
            "unreadable": self.unreadable,
            "unmatched": self.unmatched,
        }

    def summary(self) -> str:
        text = f"{self.staged} to review out of {self.read_in}"
        if self.already_here:
            text += f", {len(self.already_here)} already here"
        if self.collapsed:
            text += f", {len(self.collapsed)} duplicate"
        if self.unreadable:
            text += f", {self.unreadable} unreadable"
        return text


def _fingerprint(name: str, lat: float, lon: float) -> tuple[str, float, float]:
    """What counts as the same pin twice.

    Five decimals is about a metre, which is finer than any of the coordinates
    in the file that prompted this and coarse enough that the same place typed
    in twice matches itself.
    """
    return name.strip().casefold(), round(lat, 5), round(lon, 5)


def existing_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"]).strip().casefold()
        for row in conn.execute("SELECT name FROM places")
    }


def stage(conn: sqlite3.Connection, blob: bytes) -> Staged:
    """Read a file and hold everything in it that is worth reviewing.

    Two things are dropped rather than shown: a pin whose name already exists
    here, and the same pin twice in one file. Both are counted and named in
    the answer, because "324 of 331" invites the question.
    """
    incoming = read(blob)
    result = Staged(read_in=len(incoming))

    known = registry(conn)
    mapped = overrides(conn)
    here = existing_names(conn)
    seen: set[tuple[str, float, float]] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for item in incoming:
        if item.name.strip().casefold() in here:
            result.already_here.append(item.name)
            continue

        fingerprint = _fingerprint(item.name, item.lat, item.lon)
        if fingerprint in seen:
            result.collapsed.append(item.name)
            continue
        seen.add(fingerprint)

        assigned, unknown = people_for(item.category, known, mapped)
        for name in unknown:
            result.unmatched[name] = result.unmatched.get(name, 0) + 1

        conn.execute(
            "INSERT INTO place_import "
            "(source_id, name, lat, lon, category, people, prominence, state, "
            " imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.source_id,
                item.name,
                item.lat,
                item.lon,
                item.category,
                json.dumps(assigned),
                DEFAULT_PROMINENCE,
                WAITING,
                now,
            ),
        )
        result.staged += 1

    return result


# ---------------------------------------------------------------- the review


def _as_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "source_id": row["source_id"],
        "name": row["name"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "category": row["category"] or "",
        "people": json.loads(row["people"] or "[]"),
        "label_id": row["label_id"],
        "prominence": row["prominence"],
        "state": row["state"],
        "place_id": row["place_id"],
    }


def waiting(conn: sqlite3.Connection) -> dict[str, object]:
    """Everything in this import, in the order it arrived.

    Saved and discarded rows are included: the sidebar shows a tick or a line
    through what has been dealt with, and a list that dropped rows as they
    were decided would renumber itself under the person using it.
    """
    rows = [
        _as_dict(row)
        for row in conn.execute("SELECT * FROM place_import ORDER BY id")
    ]
    counted = {state: 0 for state in (WAITING, SAVED, DISCARDED)}
    for row in rows:
        counted[str(row["state"])] = counted.get(str(row["state"]), 0) + 1

    return {
        "total": len(rows),
        "waiting": counted[WAITING],
        "saved": counted[SAVED],
        "discarded": counted[DISCARDED],
        # Done is only offered once nothing is left undecided.
        "finished": len(rows) > 0 and counted[WAITING] == 0,
        "items": rows,
    }


def _row(conn: sqlite3.Connection, pin_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM place_import WHERE id = ?", (pin_id,)
    ).fetchone()
    if row is None:
        raise PinImportError(
            f"Nothing is waiting under {pin_id}. The import was probably "
            "finished in another tab."
        )
    return row


def edit(
    conn: sqlite3.Connection, pin_id: int, payload: dict
) -> dict[str, object]:
    """Change a staged pin. Nothing is written to the archive here."""
    row = _row(conn, pin_id)
    if row["state"] != WAITING:
        raise PinImportError(
            "That one has already been decided. Only pins still waiting can "
            "be changed."
        )

    allowed = {"name", "people", "label_id", "prominence", "lat", "lon"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PinImportError(
            f"A staged pin has no {', '.join(unknown)}. It has "
            f"{', '.join(sorted(allowed))}."
        )

    current = _as_dict(row)

    # Built field by field rather than by merging the row, because the row
    # carries the file's category - and here a category is who was there, not
    # what kind of place it is. Handed to the pin validator it is checked
    # against Irfaran's own category vocabulary and refused: "Category
    # 'restaurants' is not one of home, school, family..." on an edit that
    # only changed a name. It is context for whoever is reviewing and nothing
    # else, and it never becomes a pin's category.
    merged = {
        "name": payload.get("name", current["name"]),
        "people": payload.get("people", current["people"]),
        "lat": payload.get("lat", current["lat"]),
        "lon": payload.get("lon", current["lon"]),
        "prominence": payload.get("prominence", current["prominence"]),
    }

    # Validated by the same functions that guard a hand-dropped pin, so a
    # staged edit cannot store something the sidebar would refuse. Both inside
    # the same try: a label id pointing at nothing raises from the second one,
    # and letting a PlaceError out of here is a 500 on an ordinary typo.
    try:
        name, _, people, lat, lon, prominence = places.validate(merged)
        label_id = places.reference(
            conn, "labels", payload.get("label_id", current["label_id"]), "label"
        )
    except places.PlaceError as exc:
        raise PinImportError(str(exc)) from exc

    conn.execute(
        "UPDATE place_import SET name = ?, people = ?, label_id = ?, "
        "prominence = ?, lat = ?, lon = ? WHERE id = ?",
        (name, json.dumps(people), label_id, prominence, lat, lon, pin_id),
    )
    return _as_dict(_row(conn, pin_id))


@dataclass
class Committed:
    pin_id: int = 0
    place_id: int = 0
    name: str = ""
    layers: list[str] = field(default_factory=list)
    tiles: set[tuple[int, int]] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.pin_id,
            "place_id": self.place_id,
            "name": self.name,
            "layers": self.layers,
            "tiles_touched": len(self.tiles),
        }


def save(conn: sqlite3.Connection, pin_id: int) -> Committed:
    """Commit one staged pin to the archive.

    Through `places.create`, the same call the sidebar makes when somebody
    drops a pin by hand - so a pin that came out of another application's
    database is not a second kind of pin. No dates: the only date in the file
    was when it was typed, so these land in prehistory.
    """
    row = _row(conn, pin_id)
    if row["state"] == SAVED:
        raise PinImportError(f"{row['name']} is already in the archive.")
    if row["state"] == DISCARDED:
        raise PinImportError(
            f"{row['name']} was discarded. Nothing here can put it back."
        )

    staged = _as_dict(row)
    place, layers, tiles = places.create(
        conn,
        {
            "name": staged["name"],
            "lat": staged["lat"],
            "lon": staged["lon"],
            "people": staged["people"],
            "prominence": staged["prominence"],
            "label_id": staged["label_id"],
        },
    )

    conn.execute(
        "UPDATE place_import SET state = ?, place_id = ? WHERE id = ?",
        (SAVED, int(place["id"]), pin_id),
    )
    return Committed(
        pin_id=pin_id,
        place_id=int(place["id"]),
        name=str(place["name"]),
        layers=layers,
        tiles=set(tiles),
    )


def discard(conn: sqlite3.Connection, pin_id: int) -> dict[str, object]:
    """Decide against one. It never became a pin, so there is nothing to undo."""
    row = _row(conn, pin_id)
    if row["state"] == SAVED:
        raise PinImportError(
            f"{row['name']} is already in the archive. Delete it from Places "
            "instead."
        )
    conn.execute(
        "UPDATE place_import SET state = ? WHERE id = ?", (DISCARDED, pin_id)
    )
    return _as_dict(_row(conn, pin_id))


def done(conn: sqlite3.Connection) -> dict[str, object]:
    """Close the import and forget everything it held.

    Refused while anything is still waiting: the whole point was that each one
    gets looked at, and a Done that quietly threw away the undecided ones
    would make that a suggestion.
    """
    state = waiting(conn)
    if state["waiting"]:
        raise PinImportError(
            f"{state['waiting']} pins are still waiting. Save or discard each "
            "of them first."
        )

    conn.execute("DELETE FROM place_import")
    return {
        "saved": state["saved"],
        "discarded": state["discarded"],
        "cleared": state["total"],
    }
