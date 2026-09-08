# SPDX-License-Identifier: AGPL-3.0-or-later
"""The little world at the bottom of the statistics page.

Two things are being checked here that a picture usually cannot be checked
for. The first is that it is drawn in an equal-area projection, which is
testable precisely because that is what equal-area means: a country's share of
the coloured pixels has to match its share of the planet. The second is that
the three states stay three - the list is careful to hold *marginal* apart from
*visited*, and a picture that quietly merges them would undo that.
"""

from __future__ import annotations

import io
import re

import numpy as np
import pytest
from PIL import Image

from irfaran import countries, worldmap

from .test_markup import web_dir


def picture(
    visited: set[str],
    marginal: set[str] | None = None,
    theme: str = "dark",
    width: int = 960,
) -> np.ndarray:
    png = worldmap.render(
        frozenset(visited), frozenset(marginal or set()), theme, width
    )
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))


def count(image: np.ndarray, colour: tuple[int, int, int]) -> int:
    """Pixels of exactly this colour. Numpy, because these are megapixels."""
    return int(np.count_nonzero(np.all(image == (*colour, 255), axis=-1)))


@pytest.fixture(scope="module")
def areas() -> dict[str, float]:
    return {country.code: country.area_m2 for country in countries.load()}


class TestItIsEqualArea:
    """The whole reason for choosing Equal Earth, and it is measurable.

    Not exactly, though, and the reason is worth writing down. Filling a
    polygon into a bitmap includes its boundary pixels, so a country gains
    roughly half a pixel all the way round its coast - which is nothing for
    Algeria and a great deal for Canada. The projection is exact; the
    rasterising is not, and the error is a perimeter rather than an area, so it
    shrinks as the picture grows. That is what the last test here shows.
    """

    def density(self, code: str, width: int, areas: dict[str, float]) -> float:
        """Square kilometres per pixel. The same for everywhere, ideally."""
        pixels = count(
            picture({code}, width=width), worldmap.PALETTE["dark"]["visited"]
        )
        assert pixels, f"{code} was not drawn at {width} px"
        return areas[code] / 1e6 / pixels

    def test_the_aspect_ratio_falls_out_of_the_constants(self) -> None:
        # Equal Earth is 1:2.05, and this is computed from A1..A4 rather than
        # written down - so a typo in a constant shows up here rather than as a
        # slightly wrong map nobody measures.
        half_width, half_height = worldmap.extent()
        assert half_width / half_height == pytest.approx(2.05, abs=0.01)

    @pytest.mark.parametrize("code,other", [("CA", "US"), ("RU", "ZA"), ("KZ", "AR")])
    def test_pixels_are_proportional_to_ground(self, code, other, areas) -> None:
        # Pairs of comparable raggedness, so what is left is the projection
        # rather than the coastline.
        mine = self.density(code, 960, areas)
        theirs = self.density(other, 960, areas)
        assert mine / theirs == pytest.approx(1.0, rel=0.06)

    def test_the_error_is_the_coastline_and_it_shrinks(self, areas) -> None:
        # Russia against Brazil is the worst pair there is: 214 polygons of
        # Arctic islands against one compact block. If the projection were
        # wrong the gap would be a constant; being a perimeter, it halves as
        # the pixels do.
        coarse = self.density("RU", 960, areas) / self.density("BR", 960, areas)
        fine = self.density("RU", 1920, areas) / self.density("BR", 1920, areas)
        assert abs(fine - 1.0) < abs(coarse - 1.0) * 0.75, (
            f"the error did not shrink with resolution ({coarse:.3f} then "
            f"{fine:.3f}), so it is not the rasterising"
        )

    def test_greenland_is_not_the_size_of_africa(self, areas) -> None:
        # The single most familiar thing Mercator gets wrong, and half the
        # reason this picture is not drawn in it. Greenland is about a
        # fourteenth of Africa; on Mercator they look the same size.
        visited = worldmap.PALETTE["dark"]["visited"]
        africa = count(
            picture({"DZ", "CD", "SD", "LY", "TD", "NE", "AO", "ML", "ZA", "ET"}),
            visited,
        )
        greenland = count(picture({"GL"}), visited)
        assert 0 < greenland < africa / 4


class TestTheThreeStates:
    def test_they_are_three_different_colours(self) -> None:
        for theme, palette in worldmap.PALETTE.items():
            assert len(set(palette.values())) == 3, theme

    def test_a_marginal_country_is_not_painted_as_visited(self) -> None:
        image = picture({"AT"}, {"ES"})
        assert count(image, worldmap.PALETTE["dark"]["visited"])
        assert count(image, worldmap.PALETTE["dark"]["marginal"])

    def test_everywhere_else_is_land(self) -> None:
        image = picture(set())
        assert count(image, worldmap.PALETTE["dark"]["land"])
        assert count(image, worldmap.PALETTE["dark"]["visited"]) == 0

    def test_the_sea_is_left_to_the_page(self) -> None:
        # Transparent rather than painted: the section behind it is a different
        # colour in the two themes and sits behind a blur.
        image = picture({"AT"})
        assert image[10, 10][3] == 0, "the sea is painted"
        assert image[..., 3].min() == 0

    def test_a_hole_is_drawn_back_out(self) -> None:
        # Lesotho is entirely inside South Africa, and South Africa's polygon
        # has a Lesotho-shaped hole in it. Fill the hole in and one of these
        # two counts goes wrong.
        visited = worldmap.PALETTE["dark"]["visited"]
        alone = count(picture({"LS"}), visited)
        around = count(picture({"ZA"}), visited)
        assert alone > 0
        assert around > alone * 20


class TestTheLegendAgreesWithThePicture:
    """A Python palette and a stylesheet, saying the same thing twice."""

    def swatches(self) -> dict[tuple[str, str], str]:
        css = (web_dir() / "src" / "style.css").read_text()
        found: dict[tuple[str, str], str] = {}
        for match in re.finditer(
            r"(?::root\[data-theme='(\w+)'\]\s+)?\.world-key-(\w+)\s*\{\s*"
            r"background:\s*(#[0-9a-fA-F]{6})",
            css,
        ):
            # `system` inside the dark media query is the dark rule again, and
            # reading it as the light one is how this test first failed.
            prefix = match.group(1)
            theme = "light" if prefix is None else "dark"
            found.setdefault((theme, match.group(2)), match.group(3).lower())
        return found

    def test_every_swatch_matches_what_is_drawn(self) -> None:
        swatches = self.swatches()
        assert swatches, "no legend swatches found in style.css"
        for theme, palette in worldmap.PALETTE.items():
            for state, rgb in palette.items():
                key = (theme, state)
                assert key in swatches, f"no swatch for {theme} {state}"
                assert swatches[key] == "#%02x%02x%02x" % rgb, (
                    f"the {theme} legend shows {swatches[key]} where the map "
                    f"draws #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                )


class TestItStaysASmallPicture:
    def test_it_is_kilobytes_rather_than_megabytes(self) -> None:
        # The measurement the whole design rests on: the polygons are 2.26 MB
        # and this is the reason they never leave the machine.
        png = worldmap.render(frozenset({"AT"}), frozenset(), "dark", 960)
        assert len(png) < 40_000, f"{len(png) / 1024:.0f} KB is not a thumbnail"

    def test_the_same_world_is_drawn_once(self) -> None:
        first = worldmap.render(frozenset({"AT"}), frozenset(), "dark", 960)
        again = worldmap.render(frozenset({"AT"}), frozenset(), "dark", 960)
        assert first is again, "cached on what it is a picture of, not redrawn"

    def test_a_theme_it_cannot_draw_is_refused(self) -> None:
        with pytest.raises(ValueError, match="sepia"):
            worldmap.render(frozenset(), frozenset(), "sepia", 960)

    def test_a_wall_poster_is_refused(self) -> None:
        with pytest.raises(ValueError, match="thumbnail"):
            worldmap.render(frozenset(), frozenset(), "dark", 9000)


class TestTheEndpoint:
    """`/api/stats/world.png`, which is a read like the figures beside it."""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from irfaran import db, stats
        from irfaran.main import app

        conn = db.open_initialised()
        conn.execute(
            "DELETE FROM settings WHERE key IN (?, ?)",
            (stats.CACHE_SETTING, stats.FINGERPRINT_SETTING),
        )
        conn.commit()
        conn.close()
        with TestClient(app) as test_client:
            yield test_client

    def test_it_is_a_png_and_needs_no_token(self, client) -> None:
        # Ungated for the same reason the figures are: it is a count of
        # somebody's own map, and the map already reads without a token.
        response = client.get("/api/stats/world.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_asking_twice_costs_nothing(self, client) -> None:
        first = client.get("/api/stats/world.png")
        etag = first.headers["etag"]
        again = client.get("/api/stats/world.png", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert not again.content

    def test_both_themes_are_drawn(self, client) -> None:
        tags = set()
        for theme in ("dark", "light"):
            response = client.get(f"/api/stats/world.png?theme={theme}")
            assert response.status_code == 200
            tags.add(response.headers["etag"])
        assert len(tags) == 2, "the two themes came back as the same picture"

    @pytest.mark.parametrize("query", ["theme=sepia", "width=9000", "width=1"])
    def test_what_it_cannot_draw_is_a_bad_request(self, client, query) -> None:
        assert client.get(f"/api/stats/world.png?{query}").status_code == 400
