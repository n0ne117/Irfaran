# SPDX-License-Identifier: AGPL-3.0-or-later
"""The holding pen.

Everything automatic waits to be looked at. What has to be true of that:

  nothing held is on the map. Not an event, not a blob, not a tile - a held
  batch that has quietly rasterised itself is worse than no gate at all,
  because the whole promise is that discarding one leaves nothing to find.

  approving is the ordinary path. A batch that goes through the pen and is
  accepted unchanged has to produce exactly the events it would have produced
  without one, down to the dedup key - otherwise the gate is a second, subtly
  different way into the archive.

  a review cannot move underneath the person doing it. Opening a live batch
  seals it, and what the phone posts next waits in a batch of its own.

Every coordinate here is synthetic, in open water near Null Island.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from irfaran import db, review, trackers
from irfaran.ingest import common, live
from irfaran.main import app

from . import synthetic

TOKEN = "synthetic-review-token"

LON, LAT = 1.10, 0.40
STEP = 0.00012
START = datetime(2026, 8, 18, 7, 0, tzinfo=timezone.utc)


def auth() -> dict:
    return {"X-Irfaran-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    conn = db.open_initialised()
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM review")
    conn.execute("DELETE FROM log")
    for source in live.LIVE_SOURCES:
        live.set_enabled(conn, source, False)
    for source in review.SOURCES:
        review.set_gated(conn, source, True)
    conn.close()

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def conn():
    connection = db.open_initialised()
    connection.execute("DELETE FROM events")
    connection.execute("DELETE FROM blobs")
    connection.execute("DELETE FROM review")
    # Settings outlive any one test, so the gates are put back on the way in
    # rather than trusted to be where the last test left them.
    for source in review.SOURCES:
        review.set_gated(connection, source, True)
    connection.commit()
    yield connection
    for source in review.SOURCES:
        review.set_gated(connection, source, True)
    connection.commit()
    connection.close()


def enable(client, source: str) -> None:
    assert (
        client.patch(
            "/api/settings", headers=auth(), json={live.setting_key(source): "true"}
        ).status_code
        == 200
    )


def feature(index: int, minutes: float = 0.5, accuracy: float = 8.0) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [LON + index * STEP, LAT]},
        "properties": {
            "timestamp": (START + timedelta(minutes=minutes * index)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "horizontal_accuracy": accuracy,
            "motion": ["walking"],
            "device_id": "synthetic-phone",
        },
    }


def post(client, count: int = 10, first: int = 0) -> dict:
    response = client.post(
        "/api/ingest/overland",
        headers=auth(),
        json={"locations": [feature(index) for index in range(first, first + count)]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def fixes(count: int = 40, start: datetime | None = None) -> list[common.Fix]:
    clock = start or START
    return [
        common.Fix(
            lon=LON + index * STEP,
            lat=LAT,
            time=clock + timedelta(seconds=10 * index),
        )
        for index in range(count)
    ]


class TestTheGateIsOnByDefault:
    def test_every_source_waits_to_be_reviewed(self) -> None:
        # With no row at all, not merely with the row set to true: the default
        # has to be safe on an install that predates the setting.
        connection = db.open_initialised()
        try:
            connection.execute("DELETE FROM settings WHERE key LIKE 'review_%'")
            for source in review.SOURCES:
                assert review.is_gated(connection, source) is True
        finally:
            for source in review.SOURCES:
                review.set_gated(connection, source, True)
            connection.commit()
            connection.close()

    def test_a_fresh_install_seeds_them_on(self) -> None:
        for source in review.SOURCES:
            assert db.DEFAULT_SETTINGS[f"review_{source}"] == "true"

    def test_a_source_nobody_reviews_is_not_gated(self, conn) -> None:
        # Drawing and file imports are things somebody was already looking at.
        assert review.is_gated(conn, "manual") is False
        assert review.is_gated(conn, "place") is False


class TestNothingHeldReachesTheMap:
    def test_a_batch_writes_no_event(self, client) -> None:
        enable(client, "overland")
        post(client, 12)

        assert client.get("/api/events").json()["events"] == []
        waiting = client.get("/api/review").json()
        assert waiting["count"] == 1
        assert waiting["points"] == 12

    def test_it_writes_no_blobs_either(self, client) -> None:
        enable(client, "overland")
        post(client, 12)

        connection = db.connect()
        try:
            assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
            assert (
                connection.execute("SELECT count(*) FROM pending_render").fetchone()[0]
                == 0
            )
        finally:
            connection.close()

    def test_overland_is_still_told_the_batch_arrived(self, client) -> None:
        # Overland decides a batch was received by finding {"result": "ok"} in
        # the body, not by the status code. Answering anything else means the
        # phone re-sends the same payload forever - and it has no way of being
        # told that a human has to look at it first.
        enable(client, "overland")
        body = post(client, 5)
        assert body["result"] == "ok"
        assert body["accepted"] == 5
        assert body["waiting_review"] is True

    def test_switching_the_gate_off_restores_the_old_behaviour(self, client) -> None:
        enable(client, "overland")
        assert (
            client.patch(
                "/api/settings", headers=auth(), json={"review_overland": "false"}
            ).status_code
            == 200
        )
        post(client, 6)

        assert client.get("/api/review").json()["count"] == 0
        assert len(client.get("/api/events").json()["events"]) == 1

    def test_a_held_batch_is_not_searchable_as_a_track(self, client) -> None:
        # Search reads the event log. Nothing held is in it, so a held batch
        # cannot turn up in a search - which is the same statement as "it is
        # not on the map", said from the other side.
        enable(client, "overland")
        post(client, 8)
        assert (
            client.patch(
                "/api/settings", headers=auth(), json={"search_tracks": "true"}
            ).status_code
            == 200
        )
        found = client.get("/api/search", params={"q": "Overland"}).json()
        assert found["results"] == []


class TestTheSeal:
    def test_points_join_the_open_batch(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        post(client, 5, first=5)

        waiting = client.get("/api/review").json()
        assert waiting["count"] == 1
        assert waiting["items"][0]["points"] == 10
        assert waiting["items"][0]["sealed"] is False

    def test_the_same_batch_twice_adds_nothing(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        again = post(client, 5)

        assert again["duplicates"] == 5
        assert client.get("/api/review").json()["items"][0]["points"] == 5

    def test_opening_one_seals_it(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]

        opened = client.post(f"/api/review/{held}/open", headers=auth())
        assert opened.status_code == 200
        assert opened.json()["sealed"] is True

    def test_nothing_joins_a_sealed_batch(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.post(f"/api/review/{held}/open", headers=auth())

        post(client, 5, first=5)

        waiting = client.get("/api/review").json()
        assert waiting["count"] == 2, "the new points joined the sealed batch"
        by_id = {item["id"]: item for item in waiting["items"]}
        assert by_id[held]["points"] == 5
        assert by_id[held]["sealed"] is True

    def test_the_second_batch_is_open_again(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.post(f"/api/review/{held}/open", headers=auth())
        post(client, 3, first=5)
        post(client, 3, first=8)

        waiting = client.get("/api/review").json()
        fresh = [item for item in waiting["items"] if item["id"] != held]
        assert len(fresh) == 1, "the second batch split again instead of growing"
        assert fresh[0]["points"] == 6

    def test_opening_twice_is_harmless(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.post(f"/api/review/{held}/open", headers=auth())
        again = client.post(f"/api/review/{held}/open", headers=auth())
        assert again.status_code == 200
        assert again.json()["points"] == 5

    def test_a_batch_straddling_midnight_is_two(self, conn) -> None:
        # A day is the unit a live source is stored in, so it is the unit the
        # pen holds - a phone coming back from a tunnel at 00:05 delivers into
        # two batches for the same reason it would write two events.
        evening = datetime(2026, 8, 18, 23, 59, tzinfo=timezone.utc)
        held = review.hold_fixes(conn, "overland", fixes(30, start=evening))
        assert held.accepted == 30
        assert len(held.batches) == 2
        days = sorted(row["day"] for row in conn.execute("SELECT day FROM review"))
        assert days == ["2026-08-18", "2026-08-19"]


class TestListingIsCheap:
    def test_the_list_never_reads_a_batch(self, conn) -> None:
        # A day of Overland is megabytes of coordinates and the badge on the
        # map asks every few seconds. This is the render status recomputing
        # its job count, and the gazetteer walking dbstat, one more time -
        # both of which got as far as production.
        review.hold_fixes(conn, "overland", fixes(500))

        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            review.overview(conn)
        finally:
            conn.set_trace_callback(None)

        read = [line for line in statements if "fixes" in line.lower()]
        assert read == [], f"the listing read the coordinates: {read}"
        assert not any("select *" in line.lower() for line in statements)

    def test_it_still_reports_the_size_of_each_batch(self, conn) -> None:
        review.hold_fixes(conn, "overland", fixes(500))
        item = review.overview(conn)["items"][0]
        assert item["points"] == 500
        assert item["metres"] > 0
        assert item["bounds"] is not None


class TestEdits:
    @pytest.fixture
    def held(self, conn) -> int:
        review.hold_fixes(conn, "overland", fixes(40))
        return int(review.overview(conn)["items"][0]["id"])

    def test_a_trim_keeps_only_the_range(self, conn, held) -> None:
        detail = review.edit(conn, held, begin=10, end=29)
        assert detail["keeping"] == 20
        assert detail["points"] == 40, "the trim rewrote the stored batch"

    def test_the_coordinates_are_never_touched(self, conn, held) -> None:
        stored = "SELECT fixes FROM review WHERE id = ?"
        before = conn.execute(stored, (held,)).fetchone()[0]
        review.edit(conn, held, begin=10, end=29)
        after = conn.execute(stored, (held,)).fetchone()[0]
        assert before == after

    def test_reset_puts_it_back(self, conn, held) -> None:
        review.edit(conn, held, begin=10, end=29, title="Trimmed")
        detail = review.reset(conn, held)
        assert detail["keeping"] == 40
        assert detail["title"] != "Trimmed"

    def test_a_trim_that_ends_before_it_starts_is_refused(self, conn, held) -> None:
        with pytest.raises(review.ReviewError, match="ends before it starts"):
            review.edit(conn, held, begin=30, end=10)

    def test_a_point_that_is_not_there_is_refused(self, conn, held) -> None:
        with pytest.raises(review.ReviewError, match="has 40"):
            review.edit(conn, held, begin=400)

    def test_a_part_that_is_not_there_is_refused(self, conn, held) -> None:
        # Parts are named by the fix they start at rather than by ordinal, so
        # that adding a cut does not renumber every part after it and turn
        # "leave part 3 out" into a drop of part 4. Point 7 is inside the only
        # part this batch has, so nothing starts there.
        with pytest.raises(review.ReviewError, match="starts at point 7"):
            review.edit(conn, held, dropped=[7])

    def test_dropping_a_part_leaves_it_out(self, conn) -> None:
        # Two runs an hour apart: the gap rule splits them, so leaving one out
        # is the drive-to-the-start case.
        morning = fixes(20)
        afternoon = fixes(20, start=START + timedelta(hours=3))
        review.hold_fixes(conn, "overland", morning + afternoon)
        held = int(review.overview(conn)["items"][0]["id"])

        detail = review.detail(conn, held)
        assert len(detail["segments"]) == 2

        trimmed = review.edit(conn, held, dropped=[0])
        assert trimmed["keeping"] == 20
        assert trimmed["segments"][0]["dropped"] is True

    def test_the_endpoint_refuses_a_field_it_does_not_have(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]
        response = client.patch(
            f"/api/review/{held}", headers=auth(), json={"radius_m": 40}
        )
        assert response.status_code == 400
        assert "radius_m" in response.json()["detail"]

    def test_editing_needs_the_token(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        held = client.get("/api/review").json()["items"][0]["id"]
        blind = client.patch(f"/api/review/{held}", json={"title": "x"})
        assert blind.status_code == 401

    def test_reading_does_not(self, client) -> None:
        enable(client, "overland")
        post(client, 5)
        assert client.get("/api/review").status_code == 200


def paused(before: int = 12, after: int = 12) -> list[common.Fix]:
    """A run of fixes with one reporting pause that covers ground.

    Ten seconds apart and thirteen metres apart, then forty seconds of silence
    and six hundred metres - the shape of driving through a town while iOS has
    decided the app can wait.
    """
    out: list[common.Fix] = []
    when, lon = START, LON
    for _ in range(before):
        when += timedelta(seconds=10)
        lon += 13 / 111_320.0
        out.append(common.Fix(lon=lon, lat=LAT, time=when))
    when += timedelta(seconds=40)
    lon += 600 / 111_320.0
    out.append(common.Fix(lon=lon, lat=LAT, time=when))
    for _ in range(after - 1):
        when += timedelta(seconds=10)
        lon += 13 / 111_320.0
        out.append(common.Fix(lon=lon, lat=LAT, time=when))
    return out


class TestWhereALiveBatchBreaks:
    """The review has to show what will actually land, cuts included."""

    @pytest.fixture
    def held(self, conn) -> int:
        review.hold_fixes(conn, "overland", paused())
        return int(review.overview(conn)["items"][0]["id"])

    def test_a_pause_is_a_part_boundary(self, conn, held) -> None:
        detail = review.detail(conn, held)
        assert len(detail["segments"]) == 2
        assert detail["stretches"] == 2

    def test_the_gap_says_why_it_cuts(self, conn, held) -> None:
        cuts = [gap for gap in review.detail(conn, held)["gaps"] if gap["cut"]]
        assert len(cuts) == 1
        assert cuts[0]["reason"] == "rate"
        assert cuts[0]["metres"] < 1000

    def test_the_thresholds_come_back_with_it(self, conn, held) -> None:
        # A number nobody can see the effect of is a number nobody can tune.
        assert review.detail(conn, held)["thresholds"][live.SETTING_RATIO] == 2.5

    def test_a_workout_still_uses_the_file_rule(self, conn) -> None:
        review.hold_track(conn, "workout", common.Track(name="Ride", fixes=paused()))
        held = int(review.overview(conn)["items"][0]["id"])
        detail = review.detail(conn, held)
        # 600 m in 40 s is neither a kilometre nor five minutes, so the file
        # rule sees nothing - and says nothing, because gaps are a live idea.
        assert len(detail["segments"]) == 1
        assert detail["gaps"] == []


class TestCuttingAndRejoiningByHand:
    @pytest.fixture
    def held(self, conn) -> int:
        review.hold_fixes(conn, "overland", paused())
        return int(review.overview(conn)["items"][0]["id"])

    def test_a_join_removes_a_boundary_the_rule_found(self, conn, held) -> None:
        detail = review.edit(conn, held, joins=[12])
        assert len(detail["segments"]) == 1
        assert not any(gap["cut"] for gap in detail["gaps"])

    def test_a_cut_adds_one_where_the_rule_saw_nothing(self, conn, held) -> None:
        detail = review.edit(conn, held, cuts=[5])
        assert [segment["begin"] for segment in detail["segments"]] == [0, 5, 12]

    def test_the_gap_says_a_person_decided_it(self, conn, held) -> None:
        gaps = review.edit(conn, held, cuts=[5])["gaps"]
        mine = [gap for gap in gaps if gap["index"] == 5]
        # Only listed at all because somebody has an opinion about it: a
        # thirteen metre gap is under the floor.
        assert mine and mine[0]["by_hand"] is True and mine[0]["cut"] is True

    def test_a_boundary_that_is_not_a_point_is_refused(self, conn, held) -> None:
        with pytest.raises(review.ReviewError, match="cannot carry a cut"):
            review.edit(conn, held, cuts=[9999])

    def test_the_first_point_cannot_carry_one(self, conn, held) -> None:
        with pytest.raises(review.ReviewError, match="cannot carry a cut"):
            review.edit(conn, held, cuts=[0])

    def test_reset_forgets_them_too(self, conn, held) -> None:
        review.edit(conn, held, cuts=[5], joins=[12])
        detail = review.reset(conn, held)
        assert detail["edits"]["cuts"] == [] and detail["edits"]["joins"] == []
        assert len(detail["segments"]) == 2

    def test_a_drop_survives_an_unrelated_cut(self, conn, held) -> None:
        review.edit(conn, held, dropped=[12])
        detail = review.edit(conn, held, cuts=[5])
        assert detail["edits"]["dropped"] == [12]
        assert [s["dropped"] for s in detail["segments"]] == [False, False, True]

    def test_a_drop_whose_part_stopped_existing_is_let_go(self, conn, held) -> None:
        # Rejoining the batch means the part that was left out no longer
        # starts anywhere. A drop pointing at nothing, with nothing on screen
        # saying so, is worse than losing it.
        review.edit(conn, held, dropped=[12])
        detail = review.edit(conn, held, joins=[12])
        assert detail["edits"]["dropped"] == []
        assert detail["keeping"] == detail["points"]


class TestWhatALandedBatchLooksLike:
    def test_a_pause_lands_as_two_events(self, conn) -> None:
        review.hold_fixes(conn, "overland", paused())
        held = int(review.overview(conn)["items"][0]["id"])
        decision = review.approve(conn, held)
        assert decision.stretches == 2
        rows = conn.execute(
            "SELECT external_id FROM events WHERE source = 'overland' ORDER BY id"
        ).fetchall()
        assert [row["external_id"] for row in rows] == [
            "live-2026-08-18",
            "live-2026-08-18#2",
        ]

    def test_a_rejoin_by_hand_lands_as_one(self, conn) -> None:
        # The rule wanted two. Somebody said no, and that has to stick all the
        # way through to the event log.
        review.hold_fixes(conn, "overland", paused())
        held = int(review.overview(conn)["items"][0]["id"])
        review.edit(conn, held, joins=[12])
        assert review.approve(conn, held).stretches == 1
        assert conn.execute(
            "SELECT count(*) FROM events WHERE source = 'overland'"
        ).fetchone()[0] == 1

    def test_a_cut_by_hand_lands_as_two(self, conn) -> None:
        review.hold_fixes(conn, "overland", fixes(24))
        held = int(review.overview(conn)["items"][0]["id"])
        assert len(review.detail(conn, held)["segments"]) == 1
        review.edit(conn, held, cuts=[12])
        assert review.approve(conn, held).stretches == 2

    def test_a_dropped_middle_does_not_join_across_itself(self, conn) -> None:
        # Leaving a part out opens a hole, and joining across it would draw
        # exactly the straight line the drop was about.
        review.hold_fixes(conn, "overland", paused())
        held = int(review.overview(conn)["items"][0]["id"])
        review.edit(conn, held, cuts=[6], dropped=[6])
        review.approve(conn, held)
        rows = conn.execute(
            "SELECT geometry FROM events WHERE source = 'overland'"
        ).fetchall()
        assert len(rows) == 2


class TestApproving:
    def test_a_batch_becomes_the_days_live_track(self, client) -> None:
        enable(client, "overland")
        post(client, 12)
        held = client.get("/api/review").json()["items"][0]["id"]

        done = client.post(f"/api/review/{held}/approve", headers=auth())
        assert done.status_code == 200, done.text
        assert done.json()["points"] == 12

        events = client.get("/api/events").json()["events"]
        assert len(events) == 1
        assert events[0]["source"] == "overland"
        assert client.get("/api/review").json()["count"] == 0

    def test_a_trim_is_what_lands(self, client) -> None:
        enable(client, "overland")
        post(client, 20)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.patch(f"/api/review/{held}", headers=auth(), json={"from": 5, "to": 14})
        client.post(f"/api/review/{held}/approve", headers=auth())

        connection = db.connect()
        try:
            row = connection.execute(
                "SELECT geometry FROM events WHERE source = 'overland'"
            ).fetchone()
        finally:
            connection.close()
        assert len(json.loads(row["geometry"])["coordinates"]) == 10

    def test_a_rename_is_what_lands(self, conn) -> None:
        track = common.Track(name="Morning ride", fixes=fixes(30))
        review.hold_track(conn, "workout", track)
        held = int(review.overview(conn)["items"][0]["id"])
        review.edit(conn, held, title="Ride to the coast")
        review.approve(conn, held)

        row = conn.execute(
            "SELECT meta FROM events WHERE source = 'workout'"
        ).fetchone()
        assert json.loads(row["meta"])["track"] == "Ride to the coast"

    def test_approving_the_whole_thing_matches_the_ungated_path(self, tmp_path) -> None:
        # The gate is a delay, not another door. A batch that goes through it
        # unchanged has to write exactly what an ungated ingest would - the
        # same geometry, the same radius, the same layers, and above all the
        # same dedup key, or an activity imported by hand afterwards would be
        # drawn a second time.
        track = common.Track(name="Synthetic ride", fixes=fixes(60), activity="Ride")

        direct = db.open_initialised(tmp_path / "direct.db")
        gated = db.open_initialised(tmp_path / "gated.db")
        try:
            common.ingest_tracks(direct, "workout", [track])

            review.hold_track(gated, "workout", track)
            held = int(review.overview(gated)["items"][0]["id"])
            review.approve(gated, held)

            columns = "source, op, geometry, radius_m, layers, external_id, meta"
            left = [
                tuple(row)
                for row in direct.execute(f"SELECT {columns} FROM events ORDER BY id")
            ]
            right = [
                tuple(row)
                for row in gated.execute(f"SELECT {columns} FROM events ORDER BY id")
            ]
        finally:
            direct.close()
            gated.close()

        assert left and left == right

    def test_the_render_is_deferred_rather_than_done(self, client) -> None:
        enable(client, "overland")
        post(client, 12)
        held = client.get("/api/review").json()["items"][0]["id"]
        done = client.post(f"/api/review/{held}/approve", headers=auth()).json()
        assert done["tiles_touched"] > 0
        assert done["render_pending"] >= 0

    def test_approving_twice_is_a_404(self, client) -> None:
        enable(client, "overland")
        post(client, 6)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.post(f"/api/review/{held}/approve", headers=auth())
        again = client.post(f"/api/review/{held}/approve", headers=auth())
        assert again.status_code == 404
        assert "another tab" in again.json()["detail"]

    def test_approving_nothing_is_refused(self, conn) -> None:
        review.hold_fixes(conn, "overland", fixes(20))
        held = int(review.overview(conn)["items"][0]["id"])
        review.edit(conn, held, dropped=[0])
        with pytest.raises(review.ReviewError, match="Discard it instead"):
            review.approve(conn, held)

    def test_two_batches_for_one_day_merge_into_one_track(self, conn) -> None:
        # Approving a second batch for a day that already has one goes through
        # live.append, which owns the day's line - so it grows rather than
        # colliding with its own external id.
        review.hold_fixes(conn, "overland", fixes(10))
        first = int(review.overview(conn)["items"][0]["id"])
        review.open_for_review(conn, first)
        review.hold_fixes(
            conn, "overland", fixes(10, start=START + timedelta(seconds=200))
        )
        second = [
            item["id"] for item in review.overview(conn)["items"] if item["id"] != first
        ][0]

        review.approve(conn, first)
        review.approve(conn, int(second))

        rows = conn.execute(
            "SELECT geometry FROM events WHERE source = 'overland'"
        ).fetchall()
        assert len(rows) == 1
        assert len(json.loads(rows[0]["geometry"])["coordinates"]) == 20

    def test_it_is_written_into_the_history(self, client) -> None:
        enable(client, "overland")
        post(client, 6)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.post(f"/api/review/{held}/approve", headers=auth())

        lines = client.get("/api/history").json()["entries"]
        assert any("Accepted" in line["message"] for line in lines)


class TestDiscarding:
    def test_it_leaves_nothing_behind(self, client) -> None:
        enable(client, "overland")
        post(client, 10)
        held = client.get("/api/review").json()["items"][0]["id"]

        gone = client.delete(f"/api/review/{held}", headers=auth())
        assert gone.status_code == 200
        assert client.get("/api/review").json()["count"] == 0
        assert client.get("/api/events").json()["events"] == []

        connection = db.connect()
        try:
            assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
        finally:
            connection.close()

    def test_it_is_written_into_the_history(self, client) -> None:
        enable(client, "overland")
        post(client, 6)
        held = client.get("/api/review").json()["items"][0]["id"]
        client.delete(f"/api/review/{held}", headers=auth())

        lines = client.get("/api/history").json()["entries"]
        assert any("Discarded" in line["message"] for line in lines)

    def test_discarding_needs_the_token(self, client) -> None:
        enable(client, "overland")
        post(client, 6)
        held = client.get("/api/review").json()["items"][0]["id"]
        assert client.delete(f"/api/review/{held}").status_code == 401


class TestWhatTravels:
    def test_the_gates_go_with_a_backup(self) -> None:
        # Being careful is a preference about the archive, not about the
        # machine, so restoring somewhere else should not quietly turn every
        # gate back on - or off.
        from irfaran import transfer

        for source in review.SOURCES:
            assert f"review_{source}" in transfer.PORTABLE_SETTINGS

    def test_held_batches_do_not(self, conn) -> None:
        # A backup holds the archive. Nothing held is in the archive yet, and
        # a restore that resurrected somebody's undecided batches on another
        # machine would be carrying exactly what the gate was refusing to.
        from irfaran import transfer

        assert "review" not in transfer.TABLES


class TestWorkouts:
    def test_an_activity_is_held_rather_than_drawn(self, conn) -> None:
        track = common.Track(name="Ride", fixes=fixes(30))
        held = review.hold_track(conn, "workout", track)
        assert held is not None
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0

    def test_it_is_sealed_from_birth(self, conn) -> None:
        review.hold_track(conn, "workout", common.Track(name="Ride", fixes=fixes(30)))
        assert review.overview(conn)["items"][0]["sealed"] is True

    def test_the_same_activity_twice_is_held_once(self, conn) -> None:
        track = common.Track(name="Ride", fixes=fixes(30))
        assert review.hold_track(conn, "workout", track) is not None
        assert review.hold_track(conn, "workout", track) is None
        assert review.overview(conn)["count"] == 1

    def test_one_already_in_the_log_is_not_held(self, conn) -> None:
        track = common.Track(name="Ride", fixes=fixes(30))
        common.ingest_tracks(conn, "workout", [track])
        assert review.hold_track(conn, "workout", track) is None
        assert review.overview(conn)["count"] == 0

    def test_a_sync_holds_and_says_so(self, conn, monkeypatch) -> None:
        _stub_intervals(conn, monkeypatch)
        result = trackers.sync(conn, force=True)
        assert result.held == 1
        assert result.imported == 0
        assert "waiting to be reviewed" in result.summary()
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0

    def test_syncing_twice_holds_one(self, conn, monkeypatch) -> None:
        _stub_intervals(conn, monkeypatch)
        trackers.sync(conn, force=True)
        again = trackers.sync(conn, force=True)
        assert again.held == 0
        assert again.already_here == 1
        assert review.overview(conn)["count"] == 1

    def test_with_the_gate_off_it_imports_as_before(self, conn, monkeypatch) -> None:
        review.set_gated(conn, "workout", False)
        _stub_intervals(conn, monkeypatch)
        result = trackers.sync(conn, force=True)
        assert result.imported == 1
        assert result.held == 0
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def _stub_intervals(conn, monkeypatch) -> None:
    """One synthetic activity with positions, and no network."""
    trackers.put(conn, "intervals", "api_key", "test-key")
    trackers.put(conn, "intervals", "last_sync", "")

    points = synthetic.straight_line(40, base_lon=LON)
    activity = {
        "id": "synthetic-1",
        "name": "Synthetic ride",
        "type": "Ride",
        "start_date": START.isoformat(),
        "stream_types": ["latlng", "time"],
    }
    streams = [
        {
            "type": "latlng",
            "data": [point[1] for point in points],
            "data2": [point[0] for point in points],
        },
        {"type": "time", "data": [index * 5 for index in range(len(points))]},
    ]

    monkeypatch.setattr(
        trackers, "list_activities", lambda *args, **kwargs: [activity]
    )
    monkeypatch.setattr(
        trackers,
        "download_streams",
        lambda key, identifier: {item["type"]: item for item in streams},
    )
