# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a day of live tracking breaks, and what that stops it drawing.

A phone stops reporting - iOS suspends the app, a tunnel, a flat battery - and
starts again somewhere else. Joining the two ends with a straight line claims a
route nobody took, and the raster clears a fog corridor along it. Until this,
the live path stored every day as one unbroken line: the rule that protects
file imports was never applied to it.

The thresholds here were chosen from a real archive and are asserted against
the shapes that archive contained:

  a train sampling every eleven seconds at 130 km/h covers four hundred metres
  per fix, ninety-two times in one day. None of that is a break.

  a drive through a town where reporting paused for forty seconds and resumed
  six hundred metres later is a break, and no distance threshold that leaves
  the train alone would catch it.

  a phone on a desk for two hours covering ten metres is not a break, however
  long the silence.

Every coordinate is synthetic, in open water near Null Island.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from irfaran import db, geo, review
from irfaran.ingest import live
from irfaran.ingest.common import Fix
from irfaran.main import app

TOKEN = "synthetic-split-token"

# Open water, well away from the other suites' patches.
LON, LAT = 2.10, 0.65
START = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)

#: Degrees of longitude per metre at the equator, near enough for a fixture.
PER_METRE = 1.0 / 111_320.0


def auth() -> dict:
    return {"X-Irfaran-Token": TOKEN}


def walk(spec: list[tuple[float, float]]) -> list[Fix]:
    """Fixes from a list of (seconds since the last, metres east of it)."""
    out: list[Fix] = []
    when, lon = START, LON
    for seconds, metres in spec:
        when = when + timedelta(seconds=seconds)
        lon = lon + metres * PER_METRE
        out.append(Fix(lon=lon, lat=LAT, time=when))
    return out


TRAIN = [(11, 400)] * 30
TOWN = [(11, 150)] * 12 + [(40, 600)] + [(11, 150)] * 12
DESK = [(11, 2)] * 5 + [(7200, 10)] + [(11, 2)] * 5
FAR = [(11, 150)] * 5 + [(148, 4860)] + [(11, 150)] * 5


@pytest.fixture
def conn():
    connection = db.open_initialised()
    for table in ("events", "blobs", "review", "pending_render"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute(
        "DELETE FROM settings WHERE key LIKE 'live_split_%'"
    )
    connection.commit()
    yield connection
    connection.execute("DELETE FROM settings WHERE key LIKE 'live_split_%'")
    connection.commit()
    connection.close()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    connection = db.open_initialised()
    for table in ("events", "blobs", "review", "pending_render"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute("DELETE FROM settings WHERE key LIKE 'live_split_%'")
    live.set_enabled(connection, "overland", True)
    # This file is about what the ingest stores, not about the holding pen.
    review.set_gated(connection, "overland", False)
    connection.commit()
    connection.close()

    with TestClient(app) as test_client:
        yield test_client
    connection = db.connect()
    review.set_gated(connection, "overland", True)
    connection.commit()
    connection.close()


def feature(fix: Fix) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [fix.lon, fix.lat]},
        "properties": {
            "timestamp": fix.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "horizontal_accuracy": 8,
        },
    }


def post(client, fixes: list[Fix]) -> dict:
    response = client.post(
        "/api/ingest/overland",
        headers=auth(),
        json={"locations": [feature(fix) for fix in fixes]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def events_of(conn, source: str = "overland") -> list:
    return conn.execute(
        "SELECT * FROM events WHERE source = ? AND op = 'add' ORDER BY id",
        (source,),
    ).fetchall()


class TestTheRule:
    def test_a_fast_train_is_not_a_break(self, conn) -> None:
        # Thirty fixes, 400 m apart, eleven seconds apart: 130 km/h. Every one
        # of these is real travel and no distance threshold may cut them.
        assert live.cut_points(walk(TRAIN)) == []

    def test_a_pause_that_covers_ground_is_a_break(self, conn) -> None:
        assert live.cut_points(walk(TOWN)) == [12]

    def test_and_it_is_the_reporting_rate_that_says_so(self, conn) -> None:
        gap = next(g for g in live.survey(walk(TOWN)) if g.index == 12)
        assert gap.reason == "rate"
        assert gap.metres < 1000, "a distance rule would not have caught this"
        assert 3 < (gap.ratio or 0) < 5

    def test_a_long_pause_that_covers_nothing_is_not(self, conn) -> None:
        # Two hours on a desk. The silence is enormous and nothing moved, so
        # there is no straight line to avoid drawing.
        assert live.cut_points(walk(DESK)) == []

    def test_a_wide_jump_is_a_break_on_distance_alone(self, conn) -> None:
        gap = next(g for g in live.survey(walk(FAR)) if g.index == 5)
        assert gap.reason == "distance"

    def test_one_pause_does_not_excuse_the_next(self, conn) -> None:
        # The local rate is a median, not a mean, so a single long silence does
        # not raise the bar for the silence right after it.
        twice = [(11, 150)] * 12 + [(40, 600)] + [(40, 600)] + [(11, 150)] * 8
        assert live.cut_points(walk(twice)) == [12, 13]

    def test_the_time_rule_is_off_by_default(self, conn) -> None:
        limits = live.thresholds(conn)
        assert limits[live.SETTING_SECONDS] == 0.0
        # On a real archive it made nine cuts covering eighty-six metres.
        assert live.cut_points(walk(DESK), limits) == []

    def test_but_it_can_be_switched_on(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, '300') "
            "ON CONFLICT(key) DO UPDATE SET value = '300'",
            (live.SETTING_SECONDS,),
        )
        assert live.cut_points(walk(DESK), live.thresholds(conn)) == [5]

    def test_every_threshold_is_a_setting(self, conn) -> None:
        for key in (
            live.SETTING_METRES,
            live.SETTING_SECONDS,
            live.SETTING_RATIO,
            live.SETTING_RATIO_METRES,
        ):
            assert key in db.DEFAULT_SETTINGS
            assert key in live.SPLIT_DEFAULTS

    def test_turning_the_rate_rule_off_leaves_the_town_alone(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, '0') "
            "ON CONFLICT(key) DO UPDATE SET value = '0'",
            (live.SETTING_RATIO,),
        )
        assert live.cut_points(walk(TOWN), live.thresholds(conn)) == []

    def test_a_mistyped_setting_does_not_stop_a_phone_delivering(self, conn) -> None:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, 'quite far') "
            "ON CONFLICT(key) DO UPDATE SET value = 'quite far'",
            (live.SETTING_METRES,),
        )
        assert live.thresholds(conn)[live.SETTING_METRES] == 1000.0

    def test_undated_fixes_never_produce_a_rate_break(self, conn) -> None:
        # A source with no clock cannot be judged on how often it reports.
        blind = [Fix(lon=LON + index * 0.003, lat=LAT) for index in range(6)]
        assert all(gap.reason != "rate" for gap in live.survey(blind))


class TestOneEventPerStretch:
    def test_a_day_with_a_pause_becomes_two_events(self, conn) -> None:
        live.append(conn, "overland", walk(TOWN))
        rows = events_of(conn)
        assert len(rows) == 2
        assert [row["external_id"] for row in rows] == [
            "live-2026-08-16",
            "live-2026-08-16#2",
        ]

    def test_the_first_stretch_keeps_the_key_a_whole_day_used_to_have(self, conn) -> None:
        # So every event written before days were split is already stretch one
        # of its day, and nothing needs migrating.
        live.append(conn, "overland", walk(TOWN))
        assert events_of(conn)[0]["external_id"] == live.track_id(
            "overland", "2026-08-16"
        )

    def test_an_unbroken_day_is_still_one_event(self, conn) -> None:
        live.append(conn, "overland", walk(TRAIN))
        assert len(events_of(conn)) == 1

    def test_the_stretches_hold_the_right_points(self, conn) -> None:
        live.append(conn, "overland", walk(TOWN))
        rows = events_of(conn)
        counts = [len(json.loads(row["geometry"])["coordinates"]) for row in rows]
        assert counts == [12, 13]
        assert sum(counts) == len(TOWN)

    def test_each_stretch_keeps_its_own_timestamps(self, conn) -> None:
        live.append(conn, "overland", walk(TOWN))
        for row in events_of(conn):
            stamps = json.loads(row["meta"])["timestamps"]
            coordinates = json.loads(row["geometry"])["coordinates"]
            assert len(stamps) == len(coordinates)

    def test_a_continuing_batch_extends_the_last_stretch(self, conn) -> None:
        live.append(conn, "overland", walk([(11, 150)] * 6))
        more = walk([(11, 150)] * 12)[6:]
        live.append(conn, "overland", more)
        rows = events_of(conn)
        assert len(rows) == 1
        assert len(json.loads(rows[0]["geometry"])["coordinates"]) == 12

    def test_a_batch_after_a_pause_starts_a_new_stretch(self, conn) -> None:
        live.append(conn, "overland", walk([(11, 150)] * 12))
        assert len(events_of(conn)) == 1
        live.append(conn, "overland", walk(TOWN)[12:])
        assert len(events_of(conn)) == 2

    def test_a_replayed_batch_changes_nothing(self, conn) -> None:
        live.append(conn, "overland", walk(TOWN))
        before = [dict(row) for row in events_of(conn)]
        again = live.append(conn, "overland", walk(TOWN))
        assert again.accepted == 0
        assert again.duplicates == len(TOWN)
        assert [dict(row) for row in events_of(conn)] == before

    def test_history_is_not_re_split(self, conn) -> None:
        # A stretch boundary can be somebody's decision, so an existing stretch
        # is never reconsidered - otherwise the next batch the phone delivered
        # would undo a rejoin.
        live.append(conn, "overland", walk(TRAIN))
        assert len(events_of(conn)) == 1

        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, '100') "
            "ON CONFLICT(key) DO UPDATE SET value = '100'",
            (live.SETTING_METRES,),
        )
        # Every gap in TRAIN is 400 m, so a 100 m threshold would cut all of
        # them. The stretch already here stays whole.
        live.append(conn, "overland", walk(TRAIN + [(11, 50)])[-1:])
        assert len(events_of(conn)) == 1

    def test_the_result_says_how_many_stretches(self, conn) -> None:
        assert live.append(conn, "overland", walk(TOWN)).stretches == 2
        assert live.append(conn, "overland", walk(FAR)).stretches >= 1

    def test_midnight_still_splits_by_day_as_well(self, conn) -> None:
        crossing = [
            Fix(lon=LON, lat=LAT, time=datetime(2026, 8, 16, 23, 59, 30, tzinfo=timezone.utc)),
            Fix(lon=LON + 20 * PER_METRE, lat=LAT,
                time=datetime(2026, 8, 17, 0, 0, 10, tzinfo=timezone.utc)),
        ]
        live.append(conn, "overland", crossing)
        keys = sorted(str(row["external_id"]) for row in events_of(conn))
        assert keys == ["live-2026-08-16", "live-2026-08-17"]


class TestWhatThePhoneSaidIsKept:
    """Accuracy and motion, per fix, because they cannot be recovered later.

    Overland sends both with every location. Accuracy was used to drop the bad
    ones and then discarded; motion was collapsed into one set for the whole
    delivery. Between them they are the only fields that can distinguish a
    stale position from a real unreported stretch - and the batch that would
    have proved something about iOS is always the one already thrown away.
    """

    def test_overland_reports_motion_per_fix(self) -> None:
        payload = {
            "locations": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [LON, LAT]},
                    "properties": {
                        "timestamp": "2026-08-16T09:00:00Z",
                        "horizontal_accuracy": 8,
                        "motion": ["driving"],
                    },
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [LON + 0.001, LAT]},
                    "properties": {
                        "timestamp": "2026-08-16T09:00:20Z",
                        "horizontal_accuracy": 42,
                        "motion": ["walking", "stationary"],
                    },
                },
            ]
        }
        fixes, meta = live.parse_overland(payload)
        assert [fix.motion for fixes_ in [fixes] for fix in fixes_] == [
            "driving",
            "stationary, walking",
        ]
        assert [fix.accuracy for fix in fixes] == [8.0, 42.0]
        # And the batch-level set is still there, so nothing that read it broke.
        assert meta["motion"] == ["driving", "stationary", "walking"]

    def test_they_are_stored_on_the_event(self, conn) -> None:
        fixes = [
            Fix(lon=LON, lat=LAT, time=START, accuracy=8.0, motion="driving"),
            Fix(
                lon=LON + 20 * PER_METRE,
                lat=LAT,
                time=START + timedelta(seconds=10),
                accuracy=31.0,
                motion="walking",
            ),
        ]
        live.append(conn, "overland", fixes)
        meta = json.loads(events_of(conn)[0]["meta"])
        assert meta["accuracies"] == [8.0, 31.0]
        assert meta["motions"] == ["driving", "walking"]

    def test_a_source_that_says_nothing_stores_nothing(self, conn) -> None:
        # No array of nulls in every event for the rest of time.
        live.append(conn, "overland", walk([(10, 20)] * 4))
        meta = json.loads(events_of(conn)[0]["meta"])
        assert "accuracies" not in meta and "motions" not in meta

    def test_they_survive_a_stretch_being_split(self, conn) -> None:
        fixes = walk(TOWN)
        fixes = [
            Fix(
                lon=fix.lon,
                lat=fix.lat,
                time=fix.time,
                accuracy=float(index),
                motion="driving" if index < 12 else "walking",
            )
            for index, fix in enumerate(fixes)
        ]
        live.append(conn, "overland", fixes)
        rows = events_of(conn)
        assert len(rows) == 2
        first, second = (json.loads(row["meta"]) for row in rows)
        assert first["accuracies"] == [float(i) for i in range(12)]
        assert second["accuracies"] == [float(i) for i in range(12, 25)]
        assert set(first["motions"]) == {"driving"}
        assert set(second["motions"]) == {"walking"}

    def test_a_gap_reports_what_was_said_either_side(self, conn) -> None:
        fixes = walk(TOWN)
        fixes = [
            Fix(
                lon=fix.lon,
                lat=fix.lat,
                time=fix.time,
                accuracy=9.0 if index != 12 else 65.0,
                motion="driving",
            )
            for index, fix in enumerate(fixes)
        ]
        gap = next(g for g in live.survey(fixes) if g.index == 12)
        assert gap.accuracy_before == 9.0
        assert gap.accuracy_after == 65.0
        assert gap.motion_before == "driving" and gap.motion_after == "driving"

    def test_the_pen_carries_them_to_the_archive(self, conn) -> None:
        # A field the holding pen drops is a field the archive never sees.
        fixes = [
            Fix(lon=LON, lat=LAT, time=START, accuracy=7.0, motion="driving"),
            Fix(
                lon=LON + 20 * PER_METRE,
                lat=LAT,
                time=START + timedelta(seconds=10),
                accuracy=12.0,
                motion="driving",
            ),
        ]
        review.hold_fixes(conn, "overland", fixes)
        held = int(review.overview(conn)["items"][0]["id"])
        review.approve(conn, held)
        meta = json.loads(events_of(conn)[0]["meta"])
        assert meta["accuracies"] == [7.0, 12.0]
        assert meta["motions"] == ["driving", "driving"]


def cleared_between(client) -> int:
    """Transparent fog pixels on the tile halfway across the jump in FAR."""
    fixes = walk(FAR)
    before, after = fixes[4], fixes[5]
    tile_x, tile_y = geo.lonlat_to_tile(
        (before.lon + after.lon) / 2, (before.lat + after.lat) / 2
    )
    response = client.get(f"/api/tiles/dark/all/fog/14/{tile_x}/{tile_y}.png")
    assert response.status_code == 200
    pixels = np.array(Image.open(io.BytesIO(response.content)))
    return int((pixels[..., 3] == 0).sum())


class TestTheStraightLineIsNotDrawn:
    """The whole point. A cut is only worth anything if the fog follows it."""

    def test_no_fog_is_cleared_across_the_jump(self, client) -> None:
        post(client, walk(FAR))
        assert cleared_between(client) == 0

    def test_and_it_would_be_without_the_rule(self, client) -> None:
        # The same batch with every threshold switched off: one stretch, and a
        # corridor cleared along 4.9 km of straight line nobody travelled.
        for key in (live.SETTING_METRES, live.SETTING_RATIO):
            assert (
                client.patch(
                    "/api/settings", headers=auth(), json={key: "0"}
                ).status_code
                == 200
            )
        post(client, walk(FAR))

        connection = db.connect()
        try:
            assert len(events_of(connection)) == 1
        finally:
            connection.close()
        assert cleared_between(client) > 0

    def test_both_ends_are_still_cleared(self, client) -> None:
        post(client, walk(FAR))
        fixes = walk(FAR)
        for fix in (fixes[0], fixes[-1]):
            tile_x, tile_y = geo.lonlat_to_tile(fix.lon, fix.lat)
            response = client.get(f"/api/tiles/dark/all/fog/14/{tile_x}/{tile_y}.png")
            pixels = np.array(Image.open(io.BytesIO(response.content)))
            assert int((pixels[..., 3] == 0).sum()) > 0


class TestRebuildStaysCanonical:
    def test_a_split_day_rebuilds_to_the_same_bytes(self, client) -> None:
        # Invariant 1, with more than one event per day in the log.
        post(client, walk(TOWN))
        connection = db.connect()
        try:
            before = {
                (row["kind"], row["layer"], row["x"], row["y"]): row["data"]
                for row in connection.execute("SELECT * FROM blobs")
            }
            assert before, "nothing was stamped, so this proves nothing"

            from irfaran import raster

            tiles = {(x, y) for _, _, x, y in before}
            with db.transaction(connection):
                raster.rebuild_tiles(connection, tiles)

            after = {
                (row["kind"], row["layer"], row["x"], row["y"]): row["data"]
                for row in connection.execute("SELECT * FROM blobs")
            }
        finally:
            connection.close()
        assert after == before
