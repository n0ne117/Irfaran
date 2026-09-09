# SPDX-License-Identifier: AGPL-3.0-or-later
"""The phone layout, and the promise that it did not touch the desktop one.

The desktop layout has been used and adjusted for twenty releases. The whole
mobile pass was written on the condition that it changes none of it, which is
a promise about *where* the rules are as much as what they say: everything is
inside one media query, and a rule that escapes it is the failure this file
exists to catch.

It was also checked the other way, in a browser: 289 elements measured at
1280x800 before and after, zero differences. That is the real test and it
cannot live here, so this holds the structure the measurement depended on.
"""

from __future__ import annotations

import json
import re

import pytest

from .test_markup import web_dir

WEB = web_dir()

#: Where the phone section starts. Everything after it must be nested.
MARKER = "a phone"


def stylesheet() -> str:
    return (WEB / "src" / "style.css").read_text()


def phone_section() -> str:
    css = stylesheet()
    at = css.index(f"= {MARKER}")
    return css[at:]


class TestNothingEscapedTheMediaQuery:
    def test_the_section_exists_and_is_last(self) -> None:
        css = stylesheet()
        assert f"= {MARKER}" in css, "the phone section is gone"
        # Last, so "everything after the marker" is a complete statement.
        assert css.index(f"= {MARKER}") > len(css) * 0.8

    def test_every_rule_in_it_is_nested(self) -> None:
        """Brace depth never returns to zero except to close the query.

        A rule written after the marker but outside the `@media` block would
        apply to every screen, which is the one thing this section promised
        not to do - and it would look exactly like the rules around it.
        """
        section = phone_section()
        section = section[section.index("@media") :]
        depth = 0
        opened = False
        for index, character in enumerate(section):
            if character == "{":
                depth += 1
                opened = True
            elif character == "}":
                depth -= 1
                assert depth >= 0, "unbalanced braces in the phone section"
                if depth == 0:
                    rest = section[index + 1 :].strip()
                    assert not rest, (
                        "there is CSS after the phone media query closes, so "
                        f"it applies to every screen: {rest[:120]!r}"
                    )
        assert opened and depth == 0

    def test_it_uses_the_breakpoint_the_sheets_already_used(self) -> None:
        section = phone_section()
        assert "@media (max-width: 46rem)" in section


class TestFingersRatherThanPointers:
    def test_the_map_chrome_is_a_thumb_wide(self) -> None:
        section = phone_section()
        block = section[section.index(".icon-button {") :][:200]
        assert "width: 2.75rem" in block and "height: 2.75rem" in block

    def test_so_are_the_zoom_and_time_buttons(self) -> None:
        section = phone_section()
        block = section[section.index(".zoom-control button,") :][:260]
        assert "2.75rem" in block

    def test_panel_controls_are_raised_but_not_to_44(self) -> None:
        # A settings page of 44px everything is a settings page you scroll for
        # a minute. 40 for a labelled row, 44 for an icon over a map.
        section = phone_section()
        assert "min-height: 2.5rem" in section

    def test_a_checkbox_keeps_its_own_size(self) -> None:
        # It is the one control whose box is the graphic.
        section = phone_section()
        assert "input:not([type='checkbox'])" in section

    def test_the_search_field_does_not_zoom_ios_in(self) -> None:
        # Any font under 16px in a focused field makes Safari zoom the page.
        section = phone_section()
        block = section[section.index(".search-bar input {") :][:220]
        assert "font-size: 1rem" in block


class TestTheBottomEdge:
    """Four things want it: attribution, time bar, review badge, notices."""

    def test_the_stack_is_computed_from_one_place(self) -> None:
        section = phone_section()
        assert "--above-timeline:" in section
        for selector in (".zoom-control", ".review-badge", ".notices"):
            # Some of these appear in a shared rule too - the one that turns
            # off the long-press callout - so every block is checked rather
            # than the first one found.
            blocks = [
                section[at : at + 320]
                for at in range(len(section))
                if section.startswith(f"  {selector} {{", at)
            ]
            assert blocks, f"{selector} is not positioned for a phone at all"
            assert any("var(--above-timeline)" in block for block in blocks), selector

    def test_the_time_bar_clears_the_attribution(self) -> None:
        section = phone_section()
        block = section[section.index("  .timeline {") :][:320]
        assert "var(--attrib-room)" in block

    def test_the_safe_area_is_a_variable_with_a_zero_default(self) -> None:
        # env() with no fallback is invalid on a browser without notches, and
        # the whole block would be dropped.
        section = phone_section()
        for name in ("--safe-top", "--safe-bottom", "--safe-left", "--safe-right"):
            line = re.search(rf"{name}: env\(safe-area-inset-\w+, 0px\)", section)
            assert line, f"{name} has no zero fallback"


class TestTheToolbarThatHungOffTheScreen:
    def test_it_gets_a_line_of_its_own(self) -> None:
        section = phone_section()
        block = section[section.index("  .draw-stack {") :][:260]
        assert "order: 1" in block and "100%" in block

    def test_the_empty_half_of_the_row_does_not_eat_taps(self) -> None:
        # The row spans the width now, so its empty end sits over Settings and
        # Search. Invisibly, because there is nothing to see.
        section = phone_section()
        block = section[section.index("  .map-tools {") :][:700]
        assert "pointer-events: none" in block
        assert "pointer-events: auto" in block


class TestAddedToTheHomeScreen:
    def markup(self) -> str:
        return (WEB / "index.html").read_text()

    def test_there_is_a_manifest_and_it_is_valid(self) -> None:
        path = WEB / "public" / "manifest.webmanifest"
        assert path.is_file(), "no web app manifest"
        manifest = json.loads(path.read_text())
        for key in ("name", "start_url", "display", "icons", "theme_color"):
            assert key in manifest, key
        assert manifest["display"] == "standalone"
        assert manifest["icons"], "a manifest with no icon installs a blank square"

    def test_the_icon_is_there_and_is_drawn_not_binary(self) -> None:
        icon = WEB / "public" / "icon.svg"
        assert icon.is_file()
        assert icon.read_text().lstrip().startswith(("<!--", "<svg"))

    def test_the_page_points_at_both(self) -> None:
        markup = self.markup()
        assert 'rel="manifest"' in markup
        assert 'href="/icon.svg"' in markup
        assert 'rel="apple-touch-icon"' in markup

    def test_the_status_bar_follows_the_theme(self) -> None:
        markup = self.markup()
        for scheme in ("light", "dark"):
            assert f'media="(prefers-color-scheme: {scheme})"' in markup

    def test_the_map_goes_under_the_notch(self) -> None:
        assert "viewport-fit=cover" in self.markup()


class TestWhatWasTakenAway:
    def test_the_version_corner_is_hidden_but_not_lost(self) -> None:
        # Four things already want the bottom edge. The version is still on
        # the About tab, with the release notes link beside it.
        section = phone_section()
        block = section[section.index("  .version-corner {") :][:120]
        assert "display: none" in block
        markup = (WEB / "index.html").read_text()
        assert 'id="version-link"' in markup

    def test_the_zoom_slider_goes_and_the_buttons_stay(self) -> None:
        # Pinch is how a phone zooms; the buttons are the only way to move by
        # exactly one level, which the z14 trail threshold cares about.
        section = phone_section()
        block = section[section.index("  .zoom-slider,") :][:160]
        assert "display: none" in block
        assert "#zoom-in" not in block and "#zoom-out" not in block


@pytest.mark.parametrize("path", ["manifest.webmanifest", "icon.svg"])
def test_the_static_files_are_where_vite_will_find_them(path: str) -> None:
    """`public/` is copied into the build verbatim. Anywhere else is a 404."""
    assert (WEB / "public" / path).is_file()
