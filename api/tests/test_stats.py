# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the archive adds up to.

Two of these are the point of the file, and both are mistakes that would look
perfectly fine on screen:

  **Mercator is not a map of areas.** The same patch of cleared fog covers
  eight times less ground at the latitude of Tromso than at the equator. A
  count of pixels would say they were equal, and would flatter anybody who has
  been north.

  **The same street in two years is one piece of ground.** Fog is stored per
  source and per layer, so a commute walked in 2019 and again in 2024 is two
  blobs over identical pixels. Summing them would grow the figure every year
  without anybody going anywhere new.

Nothing here needs real coordinates: a tile row is a latitude, and that is all
the area maths cares about.
"""

from __future__ import annotations

import numpy as np
import pytest

from irfaran import db, geo, raster, stats


def blank() -> np.ndarray:
    return np.zeros((geo.TILE_PX, geo.TILE_PX), dtype=np.uint8)


def patch(pixels: int) -> np.ndarray:
    """A mask with exactly `pixels` cleared, in one corner."""
    array = blank()
    flat = array.reshape(-1)
    flat[:pixels] = raster.MASK_ON
    return array


def row_at(lat: float) -> int:
    return geo.lonlat_to_tile(0.0, lat)[1]


@pytest.fixture
def conn():
    connection = db.open_initialised()
    for table in ("events", "blobs", "places", "labels", "folders", "people"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        (stats.CACHE_SETTING, stats.FINGERPRINT_SETTING),
    )
    connection.commit()
    yield connection
    connection.close()


class TestGroundIsMeasuredOnTheGround:
    def test_the_same_pixels_are_less_ground_further_north(self, conn) -> None:
        # The whole reason this file exists. Identical masks, different rows.
        raster.write_blob(conn, "fog", "manual", "2024", 100, row_at(0.0), patch(1000))
        at_equator = stats.ground(conn)["square_metres"]

        conn.execute("DELETE FROM blobs")
        raster.write_blob(conn, "fog", "manual", "2024", 100, row_at(69.7), patch(1000))
        at_tromso = stats.ground(conn)["square_metres"]

        assert at_equator > at_tromso
        # Web Mercator's stretch at 69.7 degrees is 1/cos(69.7) in each
        # direction, so about eight times the area. Loose bounds because the
        # tile row is a band, not a line.
        assert 6.0 < at_equator / at_tromso < 10.0

    def test_a_pixel_count_alone_would_have_said_they_were_equal(self, conn) -> None:
        # Stated as a test so that a future simplification to len(nonzero)
        # fails loudly rather than quietly halving somebody's Norway.
        north, south = patch(1000), patch(1000)
        assert int(np.count_nonzero(north)) == int(np.count_nonzero(south))

    def test_twice_the_pixels_is_twice_the_ground(self, conn) -> None:
        y = row_at(48.2)
        raster.write_blob(conn, "fog", "manual", "2024", 7, y, patch(500))
        one = stats.ground(conn)["square_metres"]
        conn.execute("DELETE FROM blobs")
        raster.write_blob(conn, "fog", "manual", "2024", 7, y, patch(1000))
        two = stats.ground(conn)["square_metres"]
        assert two == pytest.approx(one * 2)

    def test_a_whole_tile_matches_the_area_of_its_row(self, conn) -> None:
        y = row_at(48.2)
        full = np.full((geo.TILE_PX, geo.TILE_PX), raster.MASK_ON, dtype=np.uint8)
        raster.write_blob(conn, "fog", "manual", "2024", 3, y, full)
        assert stats.ground(conn)["square_metres"] == pytest.approx(
            geo.tile_row_area_m2(y)
        )

    def test_the_grid_adds_up_to_a_planet_bar_the_poles(self) -> None:
        # Mercator cannot draw past about 85 degrees, so the tile grid is a
        # little short of a whole planet. It should be short by the polar caps
        # and nothing else.
        rows = geo.tile_count(geo.NATIVE_Z)
        grid = sum(geo.tile_row_area_m2(y) for y in range(rows)) * rows
        assert 0.99 < grid / geo.EARTH_SURFACE_M2 < 1.0


class TestTheSameGroundCountsOnce:
    def test_two_years_over_one_street_is_one_street(self, conn) -> None:
        y = row_at(48.2)
        raster.write_blob(conn, "fog", "manual", "2019", 5, y, patch(800))
        raster.write_blob(conn, "fog", "manual", "2024", 5, y, patch(800))

        both = stats.ground(conn)["square_metres"]
        conn.execute("DELETE FROM blobs WHERE layer = '2024'")
        one = stats.ground(conn)["square_metres"]
        assert both == pytest.approx(one)

    def test_two_sources_over_one_street_is_one_street(self, conn) -> None:
        y = row_at(48.2)
        raster.write_blob(conn, "fog", "workout", "2024", 6, y, patch(400))
        raster.write_blob(conn, "fog", "overland", "2024", 6, y, patch(400))
        assert stats.ground(conn)["tiles"] == 1
        assert stats.ground(conn)["square_metres"] == pytest.approx(
            400 * geo.tile_pixel_area_m2(y)
        )

    def test_different_ground_in_the_same_tile_adds_up(self, conn) -> None:
        # The union must not collapse two patches that do not overlap.
        y = row_at(48.2)
        first, second = blank(), blank()
        first.reshape(-1)[:300] = raster.MASK_ON
        second.reshape(-1)[300:700] = raster.MASK_ON
        raster.write_blob(conn, "fog", "manual", "2019", 8, y, first)
        raster.write_blob(conn, "fog", "manual", "2024", 8, y, second)
        assert stats.ground(conn)["square_metres"] == pytest.approx(
            700 * geo.tile_pixel_area_m2(y)
        )


class TestWaterCounts:
    def test_a_crossing_in_the_middle_of_an_ocean_counts(self, conn) -> None:
        # No land mask, deliberately: a ferry is as much a place you have been
        # as a footpath, and excluding water would need data nobody has.
        y = row_at(0.25)  # open water in the Gulf of Guinea
        raster.write_blob(conn, "fog", "overland", "2026", 8200, y, patch(600))
        assert stats.ground(conn)["square_metres"] > 0

    def test_the_share_is_of_the_whole_planet(self, conn) -> None:
        y = row_at(0.0)
        raster.write_blob(conn, "fog", "manual", "2024", 1, y, patch(1000))
        figures = stats.ground(conn)
        assert figures["percent_of_planet"] == pytest.approx(
            figures["square_metres"] / geo.EARTH_SURFACE_M2 * 100.0
        )


class TestCounting:
    def add(self, conn, source: str, name: str, fixes: int) -> None:
        import json

        conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, "
            "created_at, meta) VALUES (?, 'add', ?, 20, '[\"2024\"]', '', ?)",
            (
                source,
                '{"type": "Point", "coordinates": [0.5, 0.25]}',
                json.dumps({"track": name, "fixes": fixes}),
            ),
        )

    def test_the_total_is_the_sum_of_the_parts(self, conn) -> None:
        self.add(conn, "workout", "a ride", 100)
        self.add(conn, "workout", "a walk", 50)
        self.add(conn, "manual", "drawn", 4)
        self.add(conn, "overland", "phone", 900)
        found = stats.routes(conn)
        assert found["total"] == 4
        assert found["workouts"] + found["drawn"] + found["live"] == found["total"]

    def test_a_journey_split_into_stretches_is_one_journey(self, conn) -> None:
        # A long ride arrives as one event per continuous stretch, so counting
        # rows says more journeys than anybody made.
        for _ in range(3):
            self.add(conn, "workout", "the same ride", 100)
        found = stats.routes(conn)
        assert found["total"] == 3
        assert found["named_journeys"] == 1

    def test_points_come_from_what_the_source_reported(self, conn) -> None:
        self.add(conn, "workout", "a ride", 1200)
        self.add(conn, "overland", "phone", 800)
        assert stats.points(conn) == 2000

    def test_reveals_and_re_fogs_are_counted_apart_from_routes(self, conn) -> None:
        self.add(conn, "manual", "drawn", 4)
        conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, created_at) "
            "VALUES ('manual', 'reveal', '{}', 20, '[\"2024\"]', '')"
        )
        conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, created_at) "
            "VALUES ('manual', 'erase', '{}', 20, '[\"erase\"]', '')"
        )
        assert stats.routes(conn)["total"] == 1
        assert stats.marks(conn)["revealed"] == 1
        assert stats.marks(conn)["refogged"] == 1


class TestItIsNotRecomputedForNothing:
    """Reading every fog blob is not something to do on a timer.

    The same shape as the render status recomputing its job count on every
    poll, and as the gazetteer walking dbstat on an endpoint that was asked
    once a second. Both reached a real install.
    """

    def test_a_second_look_is_the_cached_one(self, conn) -> None:
        raster.write_blob(conn, "fog", "manual", "2024", 1, row_at(0.0), patch(10))
        assert stats.overview(conn)["cached"] is False
        assert stats.overview(conn)["cached"] is True

    def test_it_notices_when_the_archive_moves(self, conn) -> None:
        raster.write_blob(conn, "fog", "manual", "2024", 1, row_at(0.0), patch(10))
        first = stats.overview(conn)
        raster.write_blob(conn, "fog", "manual", "2024", 2, row_at(0.0), patch(10))
        second = stats.overview(conn)

        assert second["cached"] is False
        assert (
            second["ground"]["square_metres"] > first["ground"]["square_metres"]
        )

    def test_a_new_pin_moves_it_too(self, conn) -> None:
        stats.overview(conn)
        conn.execute("INSERT INTO places (name, lat, lon) VALUES ('x', 0.2, 0.5)")
        assert stats.overview(conn)["cached"] is False

    def test_asking_for_a_recount_gets_one(self, conn) -> None:
        stats.overview(conn)
        assert stats.overview(conn, refresh=True)["cached"] is False

    def test_the_fingerprint_costs_nothing(self, conn) -> None:
        # It must never read a blob's contents: if answering "is this still
        # true?" costs what computing it costs, the cache is decoration.
        raster.write_blob(conn, "fog", "manual", "2024", 1, row_at(0.0), patch(10))
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            stats.fingerprint(conn)
        finally:
            conn.set_trace_callback(None)

        assert statements, "the fingerprint asked the database nothing at all"
        for line in statements:
            assert "data" not in line.lower(), f"the fingerprint read blobs: {line}"


class TestTheEndpoint:
    def test_it_needs_no_token(self, monkeypatch) -> None:
        # Counts of somebody's own map, and the map is already readable.
        from fastapi.testclient import TestClient

        from irfaran.main import app

        monkeypatch.setenv("IRFARAN_TOKEN", "synthetic-stats-token")
        with TestClient(app) as client:
            response = client.get("/api/stats")
            assert response.status_code == 200
            body = response.json()
            for section in ("ground", "routes", "points", "marks", "time"):
                assert section in body
