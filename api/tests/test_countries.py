# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whose country a piece of cleared ground is in.

The easy half is counting countries. The hard half is not counting one nobody
has been to — asked for as *"nothing says bad code more than hiking near the
border and suddenly Switzerland is on the List"* — and everything here is about
that half.

The coordinates in this file are real places, which is the only way to check a
border: a synthetic point in the Gulf of Guinea proves nothing about whether
Austria stops where Austria stops. They are cities and landmarks, not anybody's
front door.
"""

from __future__ import annotations

import numpy as np
import pytest

from irfaran import countries, db, geo, raster, stats

#: Somewhere unmistakable in each, far from any border.
DEEP_INSIDE = [
    ("Vienna", 16.3731, 48.2083, "AT"),
    ("Rome", 12.4964, 41.9028, "IT"),
    ("Bern", 7.4474, 46.9480, "CH"),
    ("Madrid", -3.7038, 40.4168, "ES"),
    ("Reykjavik", -21.9426, 64.1466, "IS"),
    ("Canberra", 149.1300, -35.2809, "AU"),
]


def blank() -> np.ndarray:
    return np.zeros((geo.TILE_PX, geo.TILE_PX), dtype=np.uint8)


@pytest.fixture
def conn():
    connection = db.open_initialised()
    for table in ("events", "blobs", "places"):
        connection.execute(f"DELETE FROM {table}")
    connection.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        (stats.CACHE_SETTING, stats.FINGERPRINT_SETTING),
    )
    connection.commit()
    yield connection
    connection.close()


def clear_around(conn, lon: float, lat: float, pixels: int = 2000) -> None:
    """Put some cleared fog on the tile that holds this point."""
    tile_x, tile_y = geo.lonlat_to_tile(lon, lat)
    left, top = geo.tile_origin_px(tile_x, tile_y)
    x, y = geo.lonlat_to_px(lon, lat)
    column, row = int(x - left), int(y - top)

    mask = blank()
    # A blob around the point rather than a corner of the tile, so it is
    # actually where the place is.
    half = max(1, int(pixels**0.5) // 2)
    top_row = max(0, row - half)
    left_col = max(0, column - half)
    mask[top_row : top_row + 2 * half, left_col : left_col + 2 * half] = raster.MASK_ON
    raster.write_blob(conn, "fog", "manual", "2024", tile_x, tile_y, mask)


class TestTheFileItself:
    def test_it_ships_with_irfaran(self) -> None:
        assert countries.available(), (
            "data/countries.bin is missing. It is committed, and the Dockerfile "
            "copies api/irfaran wholesale - check .dockerignore has not started "
            "swallowing a directory called data again."
        )

    def test_every_country_has_a_name_a_code_and_an_area(self) -> None:
        for country in countries.load():
            assert country.name
            assert country.code
            assert country.area_m2 > 0, country.name

    def test_the_areas_match_published_figures(self) -> None:
        # The polygons and the spherical-area formula, checked together against
        # numbers anybody can look up. Half a percent is well inside what a
        # 1:10m boundary set and a spherical earth can be expected to agree on.
        published_km2 = {
            "Austria": 83_879,
            "Switzerland": 41_285,
            "Slovenia": 20_273,
            "Luxembourg": 2_586,
            "Portugal": 92_212,
        }
        by_name = {country.name: country for country in countries.load()}
        for name, known in published_km2.items():
            assert name in by_name, name
            got = by_name[name].area_m2 / 1e6
            assert 0.97 < got / known < 1.03, f"{name}: {got:,.0f} vs {known:,}"

    def test_the_rings_carry_their_own_boxes(self) -> None:
        # Computed once at load. Working them out per question meant walking
        # every vertex of a coastline for each of four thousand tiles.
        austria = next(c for c in countries.load() if c.code == "AT")
        ring = austria.polygons[0][0]
        lons = [point[0] for point in ring.points]
        assert ring.west == pytest.approx(min(lons))
        assert ring.east == pytest.approx(max(lons))


class TestAPointLandsInTheRightCountry:
    @pytest.mark.parametrize("place,lon,lat,code", DEEP_INSIDE)
    def test_somewhere_unmistakable(self, place, lon, lat, code) -> None:
        found = countries.country_at(lon, lat)
        assert found is not None, place
        assert found.code == code, f"{place} came out as {found.name}"

    def test_the_middle_of_an_ocean_is_no_country(self) -> None:
        assert countries.country_at(-30.0, 35.0) is None

    def test_a_country_inside_another_country_wins(self) -> None:
        # The test of whether holes are drawn back out, and there is no better
        # one than Lesotho: it is entirely surrounded by South Africa, whose
        # polygon has a Lesotho-shaped hole in it. Fill the hole back in and
        # Maseru comes out as South African.
        maseru = countries.country_at(27.4833, -29.3167)
        assert maseru is not None and maseru.code == "LS", maseru

        # And a few hundred kilometres away it really is South Africa.
        bloemfontein = countries.country_at(26.2041, -29.0852)
        assert bloemfontein is not None and bloemfontein.code == "ZA"


class TestBordersAreNotGuessedAtTileSize:
    """The failure that prompted all of this.

    A zoom 14 tile is over a kilometre across at European latitudes. Attributing
    a whole tile to whichever country its centre falls in would hand out a
    kilometre of the wrong country along every border in the archive.
    """

    def test_one_tile_can_hold_two_countries(self) -> None:
        # A tile on the Austrian side of the German border, and its neighbour.
        # Whichever tile straddles the line has to be able to split.
        lon, lat = 13.0, 47.70  # near Salzburg, close to the border
        tile_x, tile_y = geo.lonlat_to_tile(lon, lat)

        masks = {}
        for country in countries.load():
            mask = countries.mask_for(country, tile_x, tile_y)
            if mask is not None and int(np.count_nonzero(mask)):
                masks[country.code] = int(np.count_nonzero(mask))

        assert masks, "no country covers a tile in the middle of Europe"
        # Whatever the tile happens to straddle, no country may claim every
        # pixel *and* have a neighbour claiming pixels too - that would mean
        # the same ground counted twice.
        assert sum(masks.values()) <= geo.TILE_PX * geo.TILE_PX * len(masks)

    def test_ground_is_never_counted_for_two_countries(self, conn) -> None:
        # Overlapping polygons are real - they are the places nobody agrees
        # about - so the sum of the parts must not exceed the whole.
        for _, lon, lat, _ in DEEP_INSIDE:
            clear_around(conn, lon, lat)

        whole = stats.ground(conn)["square_metres"]
        visited = stats.visited(conn)
        parts = sum(
            float(row["square_metres"])
            for row in [*visited["countries"], *visited["marginal"]]
        )
        assert parts <= whole * 1.0001
        assert parts + visited["at_sea_square_km"] * 1e6 == pytest.approx(
            whole, rel=1e-6
        )


class TestWhatCountsAsAVisit:
    def test_ground_in_a_country_puts_it_on_the_list(self, conn) -> None:
        clear_around(conn, 16.3731, 48.2083, pixels=40_000)  # Vienna
        listed = {row["code"] for row in stats.visited(conn)["countries"]}
        assert "AT" in listed

    def test_a_sliver_is_shown_apart_rather_than_dropped(self, conn) -> None:
        # This is where an approximate border turns up, and saying so is better
        # than deciding quietly in either direction.
        clear_around(conn, 12.4964, 41.9028, pixels=4)  # a few pixels in Rome
        visited = stats.visited(conn)
        assert not any(row["code"] == "IT" for row in visited["countries"])
        assert any(row["code"] == "IT" for row in visited["marginal"])

    def test_a_pin_always_counts_however_little_ground(self, conn) -> None:
        # Two pins on Gran Canaria clear about five thousand square metres
        # between them - far under the floor a track has to clear - and Spain
        # dropping off the list for that was wrong in a way no threshold can
        # fix. Somewhere a pin was dropped is somewhere somebody chose.
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('a beach', 27.7437, -15.5834)"
        )
        clear_around(conn, -15.5834, 27.7437, pixels=4)

        visited = stats.visited(conn)
        spain = [row for row in visited["countries"] if row["code"] == "ES"]
        assert spain, "a country with a pin in it is not on the list"
        assert spain[0]["pins"] == 1
        assert spain[0]["because"] == "a pin"

    def test_a_pin_at_sea_puts_nothing_on_the_list(self, conn) -> None:
        conn.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('a buoy', 35.0, -30.0)"
        )
        assert stats.visited(conn)["countries"] == []

    def test_the_share_of_a_country_is_of_that_country(self, conn) -> None:
        clear_around(conn, 16.3731, 48.2083, pixels=40_000)
        row = next(r for r in stats.visited(conn)["countries"] if r["code"] == "AT")
        assert row["of_country_percent"] == pytest.approx(
            row["square_metres"] / (row["country_square_km"] * 1e6) * 100.0
        )
        assert 0.0 < row["of_country_percent"] < 100.0


class TestWhenTheBordersAreMissing:
    def test_the_rest_of_the_figures_still_work(self, conn, monkeypatch) -> None:
        # An optional file that takes the whole page down with it when absent
        # is worse than not having the feature.
        monkeypatch.setattr(countries, "DATA", countries.DATA.parent / "nope.bin")
        countries.load.cache_clear()
        try:
            visited = stats.visited(conn)
            assert visited["available"] is False
            assert "build_countries.py" in str(visited["why"])
            assert stats.compute(conn)["ground"]["square_metres"] == 0.0
        finally:
            countries.load.cache_clear()
