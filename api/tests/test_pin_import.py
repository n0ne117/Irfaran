# SPDX-License-Identifier: AGPL-3.0-or-later
"""Importing pins out of another application's places database.

The source file is built here rather than committed. Two reasons: `*.db` is in
`.gitignore`, so a fixture file would be invisible to git and mysteriously
absent in CI - and a real one is somebody's pins, which is a map to their front
door. Every coordinate below is synthetic, in open water near Null Island.

What has to be true:

  nothing imported is a pin. Not until somebody has looked at it. Three
  hundred pins arriving unexamined is what the holding pen exists to prevent,
  and a pin is a claim about somewhere you were.

  the category is read as people, not guessed at. A category naming one person
  assigns that person, a pair assigns both, and a category naming nobody the
  registry knows assigns nobody and says so - rather than inventing a name.

  a saved pin is an ordinary pin. It goes through places.create, so there is
  no second kind of pin with slightly different rules.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from irfaran import db, pinimport
from irfaran.main import app

TOKEN = "synthetic-pin-token"

# Open water in the Gulf of Guinea. Nobody's anything.
LON, LAT = 1.40, 0.55
STEP = 0.01


def auth() -> dict:
    return {"X-Irfaran-Token": TOKEN}


def source_db(
    path: Path,
    places: list[tuple[str, float, float, int | None]],
    categories: list[tuple[int, str]] | None = None,
    *,
    with_categories_table: bool = True,
) -> bytes:
    """A places database in the shape the importer reads."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE places ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL,"
        " description TEXT NOT NULL DEFAULT '',"
        " lat REAL NOT NULL,"
        " lng REAL NOT NULL,"
        " color TEXT NOT NULL DEFAULT '#e74c3c',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL)"
    )
    if with_categories_table:
        conn.execute(
            "CREATE TABLE categories ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL UNIQUE,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " color TEXT NOT NULL DEFAULT '#6b7280')"
        )
        for identifier, name in categories or []:
            conn.execute(
                "INSERT INTO categories (id, name) VALUES (?, ?)", (identifier, name)
            )
    for name, lat, lon, category in places:
        conn.execute(
            "INSERT INTO places (name, lat, lng, category_id) VALUES (?, ?, ?, ?)",
            (name, lat, lon, category),
        )
    conn.commit()
    conn.close()
    return path.read_bytes()


CATEGORIES = [(1, "Family"), (2, "Alex"), (3, "Andrea & Alex"), (4, "Restaurants")]


def standard(path: Path) -> bytes:
    return source_db(
        path,
        [
            ("Null Harbour", LAT, LON, 1),
            ("Guinea Rock", LAT + STEP, LON, 2),
            ("Equator Buoy", LAT + 2 * STEP, LON, 3),
            ("Deep Water", LAT + 3 * STEP, LON, 4),
            ("Nowhere", LAT + 4 * STEP, LON, None),
        ],
        CATEGORIES,
    )


@pytest.fixture
def conn():
    connection = db.open_initialised()
    for table in ("place_import", "places", "people", "labels", "events", "blobs"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute(
        "DELETE FROM settings WHERE key = ?", (pinimport.PEOPLE_SETTING,)
    )
    for name in ("Alex", "Andrea", "Simon"):
        connection.execute("INSERT INTO people (name) VALUES (?)", (name,))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def client(monkeypatch, conn):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def upload(client, blob: bytes, name: str = "places.db"):
    return client.post(
        "/api/import/pins",
        headers=auth(),
        files={"file": (name, blob, "application/octet-stream")},
    )


class TestReadingTheFile:
    def test_it_reads_names_coordinates_and_categories(self, tmp_path) -> None:
        rows = pinimport.read(standard(tmp_path / "s.db"))
        assert [row.name for row in rows] == [
            "Null Harbour",
            "Guinea Rock",
            "Equator Buoy",
            "Deep Water",
            "Nowhere",
        ]
        assert rows[0].category == "Family"
        assert rows[4].category == ""
        assert rows[0].lat == pytest.approx(LAT)

    def test_something_that_is_not_a_database_is_refused(self) -> None:
        with pytest.raises(pinimport.PinImportError, match="not a SQLite database"):
            pinimport.read(b"PK\x03\x04 this is a zip")

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(pinimport.PinImportError, match="empty"):
            pinimport.read(b"")

    def test_a_database_without_the_tables_is_refused(self, tmp_path) -> None:
        blob = source_db(
            tmp_path / "bare.db",
            [("Somewhere", LAT, LON, None)],
            with_categories_table=False,
        )
        with pytest.raises(pinimport.PinImportError, match="no categories table"):
            pinimport.read(blob)

    def test_an_unreadable_row_does_not_refuse_the_file(self, tmp_path) -> None:
        path = tmp_path / "odd.db"
        source_db(path, [("Fine", LAT, LON, None)], CATEGORIES)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO places (name, lat, lng) VALUES ('', ?, ?)", (LAT, LON))
        conn.execute(
            "INSERT INTO places (name, lat, lng) VALUES ('Off world', 99.0, ?)", (LON,)
        )
        conn.commit()
        conn.close()

        rows = pinimport.read(path.read_bytes())
        assert [row.name for row in rows] == ["Fine"]

    def test_the_source_file_is_never_written_to(self, tmp_path) -> None:
        path = tmp_path / "s.db"
        blob = standard(path)
        pinimport.read(blob)
        assert path.read_bytes() == blob


class TestTheCategoryIsWhoWasThere:
    def test_one_name_assigns_one_person(self, conn) -> None:
        known = pinimport.registry(conn)
        assert pinimport.people_for("Alex", known, {}) == (["Alex"], [])

    @pytest.mark.parametrize(
        "category", ["Andrea & Alex", "Andrea and Alex", "Andrea, Alex", "Andrea + Alex"]
    )
    def test_a_pair_assigns_both(self, conn, category) -> None:
        known = pinimport.registry(conn)
        assert pinimport.people_for(category, known, {})[0] == ["Andrea", "Alex"]

    def test_matching_ignores_case(self, conn) -> None:
        known = pinimport.registry(conn)
        # And the stored spelling wins, not whatever the file used.
        assert pinimport.people_for("ALEX", known, {}) == (["Alex"], [])

    def test_a_name_nobody_knows_is_reported_not_invented(self, conn) -> None:
        known = pinimport.registry(conn)
        found, unknown = pinimport.people_for("Restaurants", known, {})
        assert found == []
        assert unknown == ["Restaurants"]

    def test_an_override_says_who_a_group_means(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (pinimport.PEOPLE_SETTING, json.dumps({"Family": ["Alex", "Andrea", "Simon"]})),
        )
        known, mapped = pinimport.registry(conn), pinimport.overrides(conn)
        assert pinimport.people_for("Family", known, mapped) == (
            ["Alex", "Andrea", "Simon"],
            [],
        )

    def test_an_override_is_matched_without_case(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (pinimport.PEOPLE_SETTING, json.dumps({"family": ["Simon"]})),
        )
        known, mapped = pinimport.registry(conn), pinimport.overrides(conn)
        assert pinimport.people_for("Family", known, mapped)[0] == ["Simon"]

    def test_a_broken_override_setting_is_ignored_rather_than_fatal(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (pinimport.PEOPLE_SETTING, "{not json"),
        )
        assert pinimport.overrides(conn) == {}


class TestStaging:
    def test_nothing_becomes_a_pin(self, conn, tmp_path) -> None:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        assert conn.execute("SELECT count(*) FROM places").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM pending_render").fetchone()[0] == 0
        )

    def test_everything_is_waiting(self, conn, tmp_path) -> None:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        state = pinimport.waiting(conn)
        assert state["total"] == 5
        assert state["waiting"] == 5
        assert state["finished"] is False

    def test_pins_arrive_minor(self, conn, tmp_path) -> None:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        assert {row["prominence"] for row in conn.execute("SELECT prominence FROM place_import")} == {
            "minor"
        }

    def test_pins_arrive_with_no_label(self, conn, tmp_path) -> None:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        assert all(
            row["label_id"] is None
            for row in conn.execute("SELECT label_id FROM place_import")
        )

    def test_a_name_already_here_is_skipped(self, conn, tmp_path) -> None:
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('guinea rock', ?, ?)",
            (LAT + 9, LON + 9),
        )
        result = pinimport.stage(conn, standard(tmp_path / "s.db"))
        assert result.already_here == ["Guinea Rock"]
        assert result.staged == 4

    def test_the_same_pin_twice_in_one_file_is_collapsed(self, conn, tmp_path) -> None:
        blob = source_db(
            tmp_path / "dupes.db",
            [
                ("Null Harbour", LAT, LON, 2),
                ("Null Harbour", LAT, LON, 2),
                ("Elsewhere", LAT + STEP, LON, 2),
            ],
            CATEGORIES,
        )
        result = pinimport.stage(conn, blob)
        assert result.collapsed == ["Null Harbour"]
        assert result.staged == 2

    def test_the_same_name_somewhere_else_is_not_a_duplicate(self, conn, tmp_path) -> None:
        blob = source_db(
            tmp_path / "twins.db",
            [("Harbour", LAT, LON, 2), ("Harbour", LAT + 1.0, LON, 2)],
            CATEGORIES,
        )
        assert pinimport.stage(conn, blob).staged == 2

    def test_a_category_matching_nobody_is_counted(self, conn, tmp_path) -> None:
        result = pinimport.stage(conn, standard(tmp_path / "s.db"))
        # Family and Restaurants, one pin each, with no override configured.
        assert result.unmatched == {"Family": 1, "Restaurants": 1}

    def test_the_people_land_on_the_row(self, conn, tmp_path) -> None:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        rows = {
            row["name"]: json.loads(row["people"])
            for row in conn.execute("SELECT name, people FROM place_import")
        }
        assert rows["Guinea Rock"] == ["Alex"]
        assert rows["Equator Buoy"] == ["Andrea", "Alex"]
        assert rows["Deep Water"] == []
        assert rows["Nowhere"] == []


class TestEditing:
    @pytest.fixture
    def staged(self, conn, tmp_path) -> int:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        return int(
            conn.execute(
                "SELECT id FROM place_import WHERE name = 'Deep Water'"
            ).fetchone()["id"]
        )

    def test_a_name_can_be_changed(self, conn, staged) -> None:
        assert pinimport.edit(conn, staged, {"name": "Deeper Water"})["name"] == "Deeper Water"

    def test_people_can_be_assigned_by_hand(self, conn, staged) -> None:
        row = pinimport.edit(conn, staged, {"people": ["Simon"]})
        assert row["people"] == ["Simon"]

    def test_prominence_can_be_changed_while_reviewing(self, conn, staged) -> None:
        assert pinimport.edit(conn, staged, {"prominence": "major"})["prominence"] == "major"

    def test_a_field_it_does_not_have_is_refused(self, conn, staged) -> None:
        with pytest.raises(pinimport.PinImportError, match="date_from"):
            pinimport.edit(conn, staged, {"date_from": "1994"})

    def test_an_empty_name_is_refused_the_same_way_a_pin_is(self, conn, staged) -> None:
        with pytest.raises(pinimport.PinImportError, match="needs a name"):
            pinimport.edit(conn, staged, {"name": "   "})

    def test_a_label_that_does_not_exist_is_refused(self, conn, staged) -> None:
        with pytest.raises(pinimport.PinImportError):
            pinimport.edit(conn, staged, {"label_id": 9999})

    def test_a_decided_pin_cannot_be_edited(self, conn, staged) -> None:
        pinimport.discard(conn, staged)
        with pytest.raises(pinimport.PinImportError, match="already been decided"):
            pinimport.edit(conn, staged, {"name": "Too late"})


class TestKeepingAndDiscarding:
    @pytest.fixture
    def staged(self, conn, tmp_path) -> list[int]:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        return [
            int(row["id"]) for row in conn.execute("SELECT id FROM place_import ORDER BY id")
        ]

    def test_saving_makes_an_ordinary_pin(self, conn, staged) -> None:
        done = pinimport.save(conn, staged[1])
        row = conn.execute(
            "SELECT * FROM places WHERE id = ?", (done.place_id,)
        ).fetchone()
        assert row["name"] == "Guinea Rock"
        assert json.loads(row["people"]) == ["Alex"]
        assert row["prominence"] == "minor"
        assert row["label_id"] is None
        # And it clears fog, like any pin.
        assert done.tiles

    def test_a_saved_pin_lands_in_prehistory(self, conn, staged) -> None:
        done = pinimport.save(conn, staged[1])
        assert done.layers == ["prehistory"]
        row = conn.execute(
            "SELECT layers FROM events WHERE source = 'place'"
        ).fetchone()
        assert json.loads(row["layers"]) == ["prehistory"]

    def test_a_saved_pin_keeps_its_place_in_the_list(self, conn, staged) -> None:
        pinimport.save(conn, staged[1])
        state = pinimport.waiting(conn)
        assert state["total"] == 5
        assert state["saved"] == 1
        assert [item["id"] for item in state["items"]] == staged

    def test_discarding_writes_nothing(self, conn, staged) -> None:
        pinimport.discard(conn, staged[0])
        assert conn.execute("SELECT count(*) FROM places").fetchone()[0] == 0
        state = pinimport.waiting(conn)
        assert state["discarded"] == 1

    def test_saving_twice_is_refused(self, conn, staged) -> None:
        pinimport.save(conn, staged[1])
        with pytest.raises(pinimport.PinImportError, match="already in the archive"):
            pinimport.save(conn, staged[1])

    def test_a_discarded_pin_cannot_be_saved(self, conn, staged) -> None:
        pinimport.discard(conn, staged[1])
        with pytest.raises(pinimport.PinImportError, match="was discarded"):
            pinimport.save(conn, staged[1])

    def test_a_saved_pin_cannot_be_discarded(self, conn, staged) -> None:
        pinimport.save(conn, staged[1])
        with pytest.raises(pinimport.PinImportError, match="Delete it from Places"):
            pinimport.discard(conn, staged[1])

    def test_an_edit_is_what_lands(self, conn, staged) -> None:
        pinimport.edit(
            conn, staged[3], {"name": "Renamed", "people": ["Simon"], "prominence": "major"}
        )
        done = pinimport.save(conn, staged[3])
        row = conn.execute(
            "SELECT * FROM places WHERE id = ?", (done.place_id,)
        ).fetchone()
        assert row["name"] == "Renamed"
        assert json.loads(row["people"]) == ["Simon"]
        assert row["prominence"] == "major"


class TestFinishing:
    @pytest.fixture
    def staged(self, conn, tmp_path) -> list[int]:
        pinimport.stage(conn, standard(tmp_path / "s.db"))
        return [
            int(row["id"]) for row in conn.execute("SELECT id FROM place_import ORDER BY id")
        ]

    def test_done_is_refused_while_anything_waits(self, conn, staged) -> None:
        pinimport.save(conn, staged[0])
        with pytest.raises(pinimport.PinImportError, match="still waiting"):
            pinimport.done(conn)

    def test_done_clears_the_lot(self, conn, staged) -> None:
        for pin in staged[:2]:
            pinimport.save(conn, pin)
        for pin in staged[2:]:
            pinimport.discard(conn, pin)

        result = pinimport.done(conn)
        assert result == {"saved": 2, "discarded": 3, "cleared": 5}
        assert conn.execute("SELECT count(*) FROM place_import").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM places").fetchone()[0] == 2

    def test_finished_is_only_true_once_nothing_waits(self, conn, staged) -> None:
        assert pinimport.waiting(conn)["finished"] is False
        for pin in staged:
            pinimport.discard(conn, pin)
        assert pinimport.waiting(conn)["finished"] is True

    def test_an_empty_pen_is_not_finished(self, conn) -> None:
        # Nothing imported is not the same as an import that is done, or the
        # sidebar would offer Done to somebody who never opened one.
        assert pinimport.waiting(conn)["finished"] is False


class TestTheEndpoints:
    def test_uploading_stages_and_reports(self, client, tmp_path) -> None:
        response = upload(client, standard(tmp_path / "s.db"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["read"] == 5
        assert body["staged"] == 5
        assert body["unmatched"] == {"Family": 1, "Restaurants": 1}

    def test_uploading_needs_the_token(self, client, tmp_path) -> None:
        response = client.post(
            "/api/import/pins",
            files={"file": ("places.db", standard(tmp_path / "s.db"), "application/octet-stream")},
        )
        assert response.status_code == 401

    def test_a_second_import_is_refused_while_one_is_open(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        again = upload(client, standard(tmp_path / "t.db"))
        assert again.status_code == 400
        assert "still open" in again.json()["detail"]

    def test_the_wrong_kind_of_file_is_refused_with_a_reason(self, client) -> None:
        response = upload(client, b"just some text", name="notes.db")
        assert response.status_code == 400
        assert "SQLite" in response.json()["detail"]

    def test_saving_defers_the_render(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        pin = client.get("/api/import/pins").json()["items"][1]["id"]
        done = client.post(f"/api/import/pins/{pin}/save", headers=auth())
        assert done.status_code == 200, done.text
        assert done.json()["tiles_touched"] > 0

    def test_the_whole_round_trip(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        items = client.get("/api/import/pins").json()["items"]

        client.patch(
            f"/api/import/pins/{items[0]['id']}",
            headers=auth(),
            json={"people": ["Alex", "Andrea", "Simon"]},
        )
        for item in items[:2]:
            assert (
                client.post(
                    f"/api/import/pins/{item['id']}/save", headers=auth()
                ).status_code
                == 200
            )
        for item in items[2:]:
            assert (
                client.post(
                    f"/api/import/pins/{item['id']}/discard", headers=auth()
                ).status_code
                == 200
            )

        finished = client.get("/api/import/pins").json()
        assert finished["finished"] is True

        closed = client.post("/api/import/pins/done", headers=auth())
        assert closed.status_code == 200
        assert closed.json() == {"saved": 2, "discarded": 3, "cleared": 5}
        assert client.get("/api/import/pins").json()["total"] == 0

        names = [p["name"] for p in client.get("/api/places").json()["places"]]
        assert sorted(names) == ["Guinea Rock", "Null Harbour"]

    def test_done_before_everything_is_decided_is_refused(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        response = client.post("/api/import/pins/done", headers=auth())
        assert response.status_code == 400
        assert "still waiting" in response.json()["detail"]

    def test_reading_the_list_needs_no_token(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        assert client.get("/api/import/pins").status_code == 200

    def test_it_is_written_into_the_history(self, client, tmp_path) -> None:
        upload(client, standard(tmp_path / "s.db"))
        lines = client.get("/api/history").json()["entries"]
        assert any("to review" in line["message"] for line in lines)


class TestNothingStagedTravels:
    def test_a_backup_does_not_carry_staged_pins(self, conn, tmp_path) -> None:
        from irfaran import transfer

        pinimport.stage(conn, standard(tmp_path / "s.db"))
        assert "place_import" not in transfer.TABLES

        archive = zipfile.ZipFile(io.BytesIO(transfer.export_bytes(conn)))
        assert not any("place_import" in name for name in archive.namelist())
