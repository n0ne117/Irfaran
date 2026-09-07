# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guards for client behaviour that has no test runner of its own.

The TypeScript has no test runner here, and the failures it has produced are the
invisible kind: a progress bar left painted, a panel silently taking the rest of
the page down with it, a list quietly showing six of seventy. These read the
source, the way test_markup.py reads it for element ids - worth more than no
guard at all for faults nobody sees until they are using the thing.

A progress bar has to be put away by whoever painted it.

Reported after a run of reveal strokes: the fog was cleared, every point was
drawn, and the bar above the time bar stayed at about three quarters for good.

The mechanism is in `notice()`. `show()` sets a timer that hides a good-news
message after four seconds; `progress()` cancels that timer, because a bar that
vanishes mid-render is worse than one that sits there. So a caller that paints
progress owns putting the notice back into a state that ends - and the watcher
callback deliberately ignores the final poll, since there is no progress to
report once a render is finished. Nothing was left to clear the bar.

Source-level rather than behavioural, for the same reason test_markup.py is:
there is no test runner for the TypeScript, and a guard that reads the source is
worth more than no guard at all for a failure that is invisible until somebody
draws for a while and then waits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_markup import sources, web_dir

WEB = web_dir()


def source(name: str) -> str:
    path = WEB / "src" / name
    assert path.is_file(), f"{name} is missing, so this test checks nothing"
    return path.read_text()


def gated_ids(markup: str) -> set[str]:
    """Every element id that sits inside - or is - a `data-needs-token` region.

    Asked of the real tag nesting rather than of the characters nearby. The
    first version of this test looked two thousand characters back for the
    mark, which the Workout trackers section defeated by opening with more
    prose than that before its first button.
    """
    from html.parser import HTMLParser

    class Walk(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.depth: list[bool] = []
            self.found: set[str] = set()

        def _record(self, attrs: list[tuple[str, str | None]]) -> bool:
            names = {name for name, _ in attrs}
            inside = any(self.depth) or "data-needs-token" in names
            identifier = dict(attrs).get("id")
            if identifier and inside:
                self.found.add(identifier)
            return inside

        def handle_starttag(self, tag, attrs):
            self.depth.append(self._record(attrs))

        def handle_startendtag(self, tag, attrs):
            self._record(attrs)

        def handle_endtag(self, tag):
            if self.depth:
                self.depth.pop()

    walk = Walk()
    walk.feed(markup)
    return walk.found


def body_of(text: str, signature: str) -> str:
    """The text of one method, from its signature to the matching brace."""
    start = text.index(signature)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"no closing brace for {signature}")


class TestDrawing:
    def test_following_a_render_ends_by_setting_a_message(self) -> None:
        """The message is what re-arms the timer that hides the notice."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "onStatus(" in body, (
            "followTheRender paints progress but never puts the notice back, so "
            "the bar is left wherever the last poll found it"
        )

    def test_it_is_given_something_to_say(self) -> None:
        """A summary rather than an empty string, or the notice just disappears."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "summary" in body

    def test_both_paths_hand_it_a_summary(self) -> None:
        """Drawing a stroke and undoing one both paint progress."""
        text = source("draw.ts")
        calls = re.findall(r"this\.followTheRender\(([^)]*)\)", text)
        assert len(calls) == 2, f"expected drawing and undo, found {calls}"
        assert all(argument.strip() for argument in calls), (
            f"followTheRender called with nothing to say afterwards: {calls}"
        )

    def test_losing_track_is_said_out_loud(self) -> None:
        """Silence would leave the same stuck bar by another route."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "null" in body and "In progress" in body


class TestTheNoticeItself:
    def test_progress_cancels_the_hide_timer(self) -> None:
        """The reason a caller has to put it back. If this changes, so does that."""
        # The implementation, not the interface declaration above it - the
        # default argument is what tells them apart.
        body = body_of(source("ui.ts"), "progress(done: number, total: number, label = ")
        assert "clearTimer()" in body

    def test_show_arms_the_hide_timer(self) -> None:
        body = body_of(source("ui.ts"), "show(message: string, bad = false)")
        assert "setTimeout(hide" in body


class TestEverywhereElseThatPaintsProgress:
    @pytest.mark.parametrize("name", ["imports.ts", "progress.ts"])
    def test_it_also_settles_on_something(self, name: str) -> None:
        """Whoever calls progress() must have a terminal branch as well."""
        text = source(name)
        if ".progress(" not in text and "progress-bar" not in text:
            pytest.skip(f"{name} paints no progress bar")
        assert "textContent" in text or "show(" in text, (
            f"{name} paints progress with nothing that ends it"
        )


class TestWiringIsFaultIsolated:
    """One panel failing must not take the rest of the page with it.

    Reported as "a token is set, but the Import button does not react". The
    wiring in start() ran in a straight line, so the first component to throw
    took every handler after it - and the Import button is wired forty lines
    below the search bar. Nothing appeared on screen; the console was the only
    clue, and only if you thought to look.

    Verified by breaking one id and reloading: the banner said "search", and the
    Import button still worked.
    """

    def main_source(self) -> str:
        return source("main.ts")

    def test_every_component_is_wired_through_the_guard(self) -> None:
        text = self.main_source()
        bare = re.findall(r"\n  ([A-Za-z_]\w*)\.wire\(\)", text)
        assert not bare, (
            f"wired without the guard, so a throw here kills every handler "
            f"after it: {', '.join(sorted(set(bare)))}. Use wirePart('name', "
            "() => x.wire())."
        )

    def test_the_guard_exists_and_reports(self) -> None:
        body = body_of(self.main_source(), "function wirePart(")
        assert "catch" in body, "wirePart does not survive a failure"
        assert "console.error" in body, "a silent failure is the original bug"
        assert "wiring-error" in body, "nothing tells the person at the screen"

    def test_something_is_actually_wired_through_it(self) -> None:
        """A guard nothing uses would make the test above vacuous."""
        assert self.main_source().count("wirePart(") > 5


class TestTheDropCursor:
    """Reported as "sometimes it shows the pin with the cursor and sometimes not".

    The teardrop is a stylesheet rule on the canvas. The drawing tools write
    their cursor to the same element inline, and inline wins - so opening the
    drawing toolbar once and closing it again left `cursor: grab` on the canvas
    for the rest of the page load, and Drop a pin armed correctly while looking
    exactly like panning.

    The "sometimes" was real rather than imagined: hovering a track sets the
    cursor to pointer and leaving it sets the inline value back to empty
    string, which handed the rule its cursor back until the next tool was
    picked. So it worked, then stopped, then worked again, depending on where
    the mouse had been.

    Source-level, like everything else in this file - there is no test runner
    for the TypeScript, and a cursor is exactly the kind of fault that is
    invisible until somebody is using the thing.
    """

    def test_the_rule_it_relies_on_still_exists(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        assert "[data-dropping='true']" in css
        assert "cursor:" in css[css.index("[data-dropping='true']") :][:400]

    def test_arming_takes_the_cursor_and_remembers_it(self) -> None:
        body = body_of(source("places.ts"), "  private armDrop(): void {")
        assert "borrowedCursor" in body, "nothing remembers what it displaced"
        assert "style.cursor = ''" in body, (
            "an inline cursor left in place beats the stylesheet, which is the "
            "whole bug"
        )

    def test_disarming_hands_it_back(self) -> None:
        body = body_of(source("places.ts"), "  private disarmDrop(): void {")
        assert "style.cursor = this.borrowedCursor" in body

    def test_every_way_out_hands_it_back(self) -> None:
        # Escape and clicking the map are the two exits, and both have to
        # restore - so both go through the one method that does.
        text = source("places.ts")
        assert text.count("this.disarmDrop()") >= 2
        assert "this.dropping) this.disarmDrop()" in text

    def test_a_track_does_not_steal_it_mid_drop(self) -> None:
        text = source("trails.ts")
        for handler in ("mouseenter", "mouseleave"):
            start = text.index(f"'{handler}', HIT_LAYER")
            block = text[start : start + 500]
            assert "dataset.dropping" in block, (
                f"the {handler} handler writes the cursor without checking "
                "whether a pin is being placed"
            )


class TestTheTokenFieldOnlyClaimsWhatItKnows:
    """Reported as: one character reads as "token set in this browser".

    It stored on every keystroke. Once the interface started gating itself on
    having a token, that meant one character also lifted the entire gate.
    """

    def test_typing_stores_nothing(self) -> None:
        text = source("ui.ts")
        start = text.index("input.addEventListener('input'")
        handler = text[start : text.index("})", start)]
        assert "setToken" not in handler, "typing still stores a token"
        assert "onChange" not in handler, "typing still tells the rest of the app"

    def test_typing_clears_whatever_was_said(self) -> None:
        # Whatever the line said before the first keystroke is stale after it.
        text = source("ui.ts")
        start = text.index("input.addEventListener('input'")
        handler = text[start : text.index("})", start)]
        assert "state.textContent = ''" in handler

    def test_the_line_says_nothing_when_there_is_nothing_to_say(self) -> None:
        # Anchored inside wireTokenField: `const paint = () => {` also appears
        # in wireZoom, earlier in the file, and body_of takes the first match.
        text = source("ui.ts")
        body = body_of(text[text.index("export function wireTokenField(") :],
                       "  const paint = () => {")
        assert "state.hidden = !token" in body

    def test_apply_checks_before_it_stores(self) -> None:
        # Storing first made a wrong token the stored one for as long as the
        # round trip took - long enough for the banner to blink away and back.
        text = source("ui.ts")
        checked = text.index("{ token: candidate }")
        stored = text.index("setToken(candidate)")
        assert checked < stored, "the token is stored before the server sees it"

    def test_api_send_can_try_a_token_without_it_being_stored(self) -> None:
        # Not through body_of: apiSend's own signature contains braces, and it
        # stops at the first balanced pair it finds.
        assert "options.token ?? getToken()" in source("api.ts")


class TestOpenInSomebodyElsesMap:
    """A pin's coordinates handed to a service that knows about routing.

    The one thing to be careful about is what else goes in the URL. A query
    string is the least private place in computing, and a pin's name, label and
    who-was-there are exactly the parts worth keeping.
    """

    def test_only_the_coordinates_are_sent(self) -> None:
        body = body_of(source("maps.ts"), "export function externalUrl(")
        for private in ("name", "label", "people", "tags", "title"):
            assert private not in body, (
                f"the {private} of a pin is being put in a URL to a third party"
            )

    def test_the_link_hides_where_it_came_from(self) -> None:
        # A self-hosted instance's hostname is the one thing on the page that
        # nobody else needs to learn.
        body = body_of(source("maps.ts"), "export function externalLink(")
        assert "noreferrer" in body

    def test_it_opens_away_from_the_map(self) -> None:
        body = body_of(source("maps.ts"), "export function externalLink(")
        assert "_blank" in body

    def test_all_three_services_are_built(self) -> None:
        text = source("maps.ts")
        for host in ("openstreetmap.org", "google.com/maps", "maps.apple.com"):
            assert host in text

    def test_google_uses_the_documented_form(self) -> None:
        # api=1 is Google's promise that the parameters keep working.
        assert "api=1" in source("maps.ts")

    def test_it_is_an_anchor_rather_than_a_button(self) -> None:
        # So that middle-click and ctrl-click do what they do everywhere else.
        body = body_of(source("maps.ts"), "export function externalLink(")
        assert "createElement('a')" in body

    def test_the_link_is_offered_without_a_token(self) -> None:
        # Opening a pair of coordinates in OpenStreetMap needs no token, so it
        # has to be built before the gate that hides Edit and Delete.
        body = body_of(source("places.ts"), "  private popupFor(")
        assert body.index("externalLink(") < body.index("if (!getToken()) return root")

    def test_changing_it_repaints_the_pins(self) -> None:
        # A popup's contents are built when its marker is created, so without
        # this the setting stored correctly and every pin went on offering the
        # previous service until the next reload.
        text = source("main.ts")
        start = text.index("wirePart('maps-provider'")
        assert "places.load()" in text[start : start + 600]

    def test_the_picker_needs_no_token(self) -> None:
        assert "maps-provider" not in gated_ids((WEB / "index.html").read_text())


class TestAStrokeBelongsToTheYearOnScreen:
    """Reported as: is a stroke saved in the selected year, or somewhere else?

    Somewhere else. The year came from a text field in the settings which was
    blank by default, so `expand_layers(None)` filed the stroke in prehistory -
    the 2019 view was never re-rendered, the vector layer filtered it out, and
    the line simply vanished when the preview cleared. You drew something and
    nothing happened.

    The Timeline object was already being handed to the drawing code, and
    already exposed the getter that answers this. It was used to refresh the
    year list and nothing else.
    """

    def test_the_year_comes_from_the_time_bar(self) -> None:
        text = source("main.ts")
        assert "followTheYear(timeline.view)" in text
        assert "draw.layers = strokeLayerFor(view)" in text

    def test_changing_the_year_changes_where_a_stroke_lands(self) -> None:
        # Not read once at startup: selecting 2019 an hour in has to work too.
        text = source("main.ts")
        start = text.index("const timeline = new Timeline(")
        assert "followTheYear(view)" in text[start : start + 400]

    def test_a_year_view_maps_to_that_year(self) -> None:
        body = body_of(source("timeline.ts"), "export function strokeLayerFor(")
        assert "view.slice('year:'.length)" in body

    def test_anything_that_is_not_a_year_is_undated(self) -> None:
        # `all` is not a year, and a stroke drawn there has no date somebody
        # supplied. Prehistory is where undated things go - and the cumulative
        # view shows it either way, so nothing disappears.
        body = body_of(source("timeline.ts"), "export function strokeLayerFor(")
        assert "return PREHISTORY" in body

    def test_the_settings_field_is_gone(self) -> None:
        markup = (WEB / "index.html").read_text()
        assert 'id="draw-layers"' not in markup
        assert "draw-layers" not in source("main.ts")


class TestReadOnlyWithoutAToken:
    """Reading the map needs no token; changing anything does.

    Before this, a browser with no token got the whole interface and a 401 for
    each thing it tried, which reads as the application being broken rather
    than as read-only.
    """

    def markup(self) -> str:
        return (WEB / "index.html").read_text()

    def test_the_banner_exists_and_is_crimson(self) -> None:
        assert 'id="token-missing"' in self.markup()
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".token-banner {") :][:400]
        assert "var(--bad)" in block, "the banner is not the colour of a problem"

    def test_the_banner_is_centred_and_has_no_button(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        assert "text-align: center" in css[css.index(".token-banner {") :][:400]
        markup = self.markup()
        start = markup.index('id="token-missing"')
        assert "<button" not in markup[start : markup.index("</p>", start)]

    def test_nothing_gated_looks_live(self) -> None:
        # inert swallows a click silently, so a control that still carries a
        # pointer cursor reads as broken rather than as switched off - which is
        # how four review switches came to look enabled while doing nothing.
        css = (WEB / "src" / "style.css").read_text()
        assert "cursor: not-allowed" in css[css.index("[data-needs-token][inert]") :][:600]

    def test_a_checkbox_is_gated_by_its_label(self) -> None:
        # Marking the input alone dims a twelve-pixel box and leaves the words
        # beside it at full brightness, which is what "not really locked" was.
        markup = self.markup()
        for which in ("workout", "overland", "owntracks", "ha"):
            index = markup.index(f'id="review-{which}"')
            label = markup.rindex("<label", 0, index)
            assert "data-needs-token" in markup[label : markup.index(">", label)], which

    def test_drawing_and_track_settings_are_gated(self) -> None:
        # Neither can do anything without a token: drawing is refused, and the
        # trail ramp is baked into tiles the server renders.
        gated = gated_ids(self.markup())
        for locked in ("draw-radius", "trail-ramp",
                       "trail-style", "trail-popups", "heat-opacity"):
            assert locked in gated, f"{locked} is not behind the gate"

    def test_some_of_the_view_is_still_yours(self) -> None:
        # The borders and the scale bar are drawn by this browser and stay
        # adjustable without a token.
        #
        # The two opacity sliders are not in this list, and that is worth
        # knowing rather than assuming: fog thickness and trail strength are
        # also browser-only, but they sit inside the Fog and Tracks sections,
        # which are gated because the fog *colour* and the trail *ramp* are
        # baked into tiles the server renders. Gated by association, and only
        # separable by splitting those sections in two.
        gated = gated_ids(self.markup())
        for free in ("show-borders", "show-scale"):
            assert free not in gated, f"{free} is gated and needs no token"
        for swept_up in ("fog-opacity", "heat-opacity"):
            assert swept_up in gated

    def test_it_is_above_the_tabs_so_every_tab_shows_it(self) -> None:
        markup = self.markup()
        assert markup.index('id="token-missing"') < markup.index('id="tabs"')

    def test_the_gate_uses_inert_rather_than_walking_the_tree(self) -> None:
        # inert takes a subtree out of pointer and keyboard reach in one
        # attribute, and keeps covering controls rendered later - the
        # live-tracking switches are built after the page loads.
        body = body_of(source("main.ts"), "function applyTokenGate(")
        assert ".inert = " in body
        assert "[data-needs-token]" in body

    def test_the_two_map_buttons_go_away(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        assert "body[data-token='missing'] #draw-toggle" in css
        assert "#review-badge" in css[css.index("body[data-token='missing']") :][:400]

    def test_the_security_tab_is_never_gated(self) -> None:
        # It is where the token is entered. Gating it behind having one is a
        # locked door with the key inside.
        gated = gated_ids(self.markup())
        assert "settings-token" not in gated and "token-apply" not in gated

    def test_it_re_runs_when_a_token_arrives(self) -> None:
        # A token can arrive at any moment - the setup screen, the Security
        # tab, a password manager filling a field. Reading it once at startup
        # would be wrong for the rest of the session.
        assert "onTokenChange(() => applyTokenGate())" in source("main.ts")

    def test_setting_a_token_tells_the_watchers(self) -> None:
        body = body_of(source("api.ts"), "export function setToken(")
        assert "watchers" in body

    def test_one_failing_watcher_does_not_stop_the_others(self) -> None:
        body = body_of(source("api.ts"), "export function setToken(")
        assert "catch" in body

    def test_things_that_write_are_marked(self) -> None:
        # A sample across the tabs, each of which reaches the server. Asked of
        # the real tag nesting rather than of the characters nearby: the
        # Workout trackers section opens two thousand characters of prose
        # before its first button, which a text window quietly failed.
        gated = gated_ids(self.markup())
        for writes in (
            "fog-colour-apply",
            "label-add",
            "search-pins",
            "gaz-place-build",
            "export-start",
            "import-button",
            "intervals-save",
            "progress-start",
            "history-clear",
            "review-overland",
            "place-drop",
        ):
            assert writes in gated, (
                f"{writes} writes to the server and is not behind the gate"
            )

    def test_reading_is_never_gated(self) -> None:
        # Refreshing history, copying diagnostics and entering the token are
        # all things a browser with no token must be able to do.
        gated = gated_ids(self.markup())
        for free in ("settings-token", "token-apply", "history-refresh",
                     "diagnostics-copy", "maps-provider"):
            assert free not in gated, f"{free} needs no token and is gated anyway"

    def test_a_pin_popup_offers_nothing_it_cannot_do(self) -> None:
        body = body_of(source("places.ts"), "  private popupFor(")
        assert "if (!getToken()) return root" in body


class TestNoticesStackRatherThanOverlap:
    """Reported as: the track count sits on top of the drawing progress bar.

    They were four boxes pinned to the same coordinates - `bottom: 5.5rem`,
    centred - so any two of them visible at once occupied the same space.
    """

    def test_the_map_notices_share_one_column(self) -> None:
        markup = (WEB / "index.html").read_text()
        start = markup.index('<div class="notices">')
        end = markup.index("</div>", start)
        column = markup[start:end]
        for one in ("wiring-error", "map-error", "trail-notice", "draw-status"):
            assert f'id="{one}"' in column, f"{one} is not in the column"

    def test_the_progress_bar_is_last(self) -> None:
        # The column is anchored by its bottom edge, so the last child is the
        # one that does not move when something appears above it - and the
        # progress bar is the one being watched.
        markup = (WEB / "index.html").read_text()
        start = markup.index('<div class="notices">')
        column = markup[start : markup.index("</div>", start)]
        assert column.rindex('id="draw-status"') > column.rindex('id="trail-notice"')

    def test_they_are_laid_out_rather_than_stacked(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".notices {") :][:500]
        assert "flex-direction: column" in block

    def test_a_notice_in_the_column_stops_positioning_itself(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".notices .notice {") :][:300]
        assert "position: static" in block

    def test_the_gaps_between_them_are_still_map(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".notices {") :][:600]
        assert "pointer-events: none" in block
        assert "pointer-events: auto" in css[css.index(".notices .notice {") :][:300]

    def test_settings_notices_are_left_alone(self) -> None:
        # `.notice` is also used inside the settings sheet, where it is
        # positioned against the sheet rather than the map. Only the ones in
        # the column change.
        markup = (WEB / "index.html").read_text()
        start = markup.index('<div class="notices">')
        column = markup[start : markup.index("</div>", start)]
        for elsewhere in ("fog-colour-status", "review-gates-status"):
            assert elsewhere not in column


class TestTheTrackCountCanBeSilenced:
    def test_it_is_a_preference(self) -> None:
        text = source("trails.ts")
        assert "export function getTrailCapNotice(" in text
        assert 'id="trail-cap-notice"' in (WEB / "index.html").read_text()

    def test_it_only_gates_the_saying_not_the_drawing(self) -> None:
        # The cap is a bound on the response, not a preference. Switching this
        # off must not change what is fetched or drawn.
        text = source("trails.ts")
        assert "collection.truncated && getTrailCapNotice()" in text
        body = body_of(text, "  async refresh(): Promise<void> {")
        assert "getTrailCapNotice" in body
        assert "cap=" not in body and "capNotice" not in body.replace("getTrailCapNotice", "")


class TestTheScaleBar:
    """The one thing a map is expected to have that this did not.

    MapLibre's own control, against the grain of the rest of this chrome - the
    zoom slider is hand-built because MapLibre has no vertical one. A scale bar
    is a scale bar, and that one already knows a degree of longitude is shorter
    in Vienna than at the equator.
    """

    def test_it_is_added_once_and_in_metres(self) -> None:
        text = source("map.ts")
        assert text.count("new ScaleControl(") == 1
        assert "unit: 'metric'" in text, "everything else in this app is metres"

    def test_it_sits_in_the_one_free_corner(self) -> None:
        # Top left is settings and search, top right the map tools, mid-left
        # the zoom, bottom left the version and the review badge, and the
        # middle is the time bar.
        body = source("map.ts")
        start = body.index("new ScaleControl(")
        assert "'bottom-right'" in body[start : start + 200]

    def test_it_follows_the_theme_rather_than_shipping_a_default(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".maplibregl-ctrl-scale {") :][:600]
        for token in ("var(--line)", "var(--muted)", "var(--panel)"):
            assert token in block, f"the scale bar hard-codes a colour, not {token}"

    def test_it_can_be_switched_off(self) -> None:
        text = source("map.ts")
        assert "export function getScaleVisible(" in text
        assert "export function setScaleVisible(" in text
        markup = (WEB / "index.html").read_text()
        assert 'id="show-scale"' in markup

    def test_the_choice_survives_a_reload(self) -> None:
        body = body_of(source("map.ts"), "export function getScaleVisible(")
        assert "localStorage" in body

    def test_off_beats_the_theming_rule(self) -> None:
        # The rule that themes it needs #map to win at all, so the rule that
        # hides it needs one more step than that.
        css = (WEB / "src" / "style.css").read_text()
        assert "#map.map-scale-off .maplibregl-ctrl-scale" in css

    def test_it_survives_a_restyle_without_being_reapplied(self) -> None:
        # A class on the container, not a style layer - so unlike the borders it
        # does not have to be put back every time the map is restyled.
        body = body_of(source("map.ts"), "export function applyScale(")
        assert "getContainer()" in body and "classList" in body

    def test_it_keeps_maplibres_open_topped_bracket(self) -> None:
        # The bar reads as something measuring the ground underneath it rather
        # than as a label in a box, and that is the missing top border.
        css = (WEB / "src" / "style.css").read_text()
        block = css[css.index(".maplibregl-ctrl-scale {") :][:600]
        assert "border-top: none" in block


class TestHidingEveryPin:
    """The button beside Places, and the thing it must not fight with.

    Two mechanisms hide pins: this button, and a sidebar that needs the map to
    itself while imported pins are reviewed. Sharing one class would mean
    closing that sidebar switches the pins back on over somebody who had
    deliberately switched them off.
    """

    def test_the_button_is_next_to_the_places_one(self) -> None:
        markup = (WEB / "index.html").read_text()
        places = markup.index('id="places-toggle"')
        pins = markup.index('id="pins-toggle"')
        assert pins > places, "the pin toggle sits before the Places button"
        # In the same row, not somewhere else on the map.
        tools = markup.index('class="map-tools"')
        assert tools < places < pins

    def test_the_two_reasons_to_hide_a_pin_are_separate(self) -> None:
        text = source("places.ts")
        suspend = body_of(text, "  suspend(on: boolean): void {")
        preference = body_of(text, "  applyVisible(): void {")
        assert "map-pins-suspended" in suspend
        assert "map-pins-off" in preference
        assert "map-pins-off" not in suspend, (
            "the review sidebar shares the button's class, so closing it "
            "overrides a deliberate choice"
        )

    def test_both_classes_actually_hide_something(self) -> None:
        css = (WEB / "src" / "style.css").read_text()
        for name in ("map-pins-off", "map-pins-suspended"):
            assert f".{name} .place-pin" in css

    def test_the_choice_survives_a_reload(self) -> None:
        body = body_of(source("places.ts"), "export function getPinsVisible(")
        assert "localStorage" in body

    def test_it_asks_the_server_for_nothing(self) -> None:
        for name in ("getPinsVisible(", "setPinsVisible("):
            body = body_of(source("places.ts"), f"export function {name}")
            for call in ("apiGet", "apiSend", "fetch("):
                assert call not in body, f"{name} talks to the server"


class TestThePinImportStaysHidden:
    """Asked for as a feature nobody is invited to use.

    Noted in the release notes and nowhere in the interface: it is a one-off
    for one person with a database of pins from another application. Guarded
    because "add a button for it" is the obvious, friendly, wrong change - and
    because the requirement lives in a conversation rather than in the code.
    """

    def import_panel(self) -> str:
        """The visible markup of the Import tab, and only that."""
        markup = (WEB / "index.html").read_text()
        start = markup.index('<div class="tab-panel" data-tab="import"')
        end = markup.index('<div class="tab-panel"', start + 1)
        return markup[start:end]

    def test_the_import_page_says_nothing_about_it(self) -> None:
        # Words a person can read, not attributes. The accept list on the
        # hidden input does name the extension, and has to.
        visible = re.sub(r"<[^>]*>", " ", self.import_panel()).lower()
        for word in ("places database", "pin", "sqlite", ".db"):
            assert word not in visible, (
                f"the Import page tells the reader about {word!r}, which makes "
                "this a feature rather than a one-off nobody was told about"
            )

    def test_the_picker_still_accepts_the_file(self) -> None:
        # The one visible change, and it is not visible: an extension in an
        # attribute on a hidden input.
        markup = (WEB / "index.html").read_text()
        line = next(row for row in markup.splitlines() if 'id="import-file"' in row)
        assert ".db" in line

    def test_there_is_no_tab_or_button_for_it(self) -> None:
        markup = (WEB / "index.html").read_text()
        tabs = re.findall(r'data-tab="([a-z-]+)"', markup)
        assert not [tab for tab in tabs if "pin" in tab]
        # The sidebar's own controls are the only pin-import buttons, and they
        # only exist once it is open.
        aside = markup.index('id="pin-import-page"')
        for match in re.finditer(r'id="(pin-import-[a-z-]+)"', markup):
            assert match.start() >= aside, (
                f"{match.group(1)} sits outside the sidebar, so something "
                "outside it can be clicked to get here"
            )


class TestStagedPinEditsCannotOvertakeEachOther:
    """Same shape as the holding pen, and the same fix, applied up front."""

    def test_a_save_carries_the_whole_pin(self) -> None:
        body = body_of(source("pinimport.ts"), "private async saveEdits(")
        for field in ("name", "people", "label_id", "prominence"):
            assert field in body

    def test_saves_are_chained(self) -> None:
        body = body_of(source("pinimport.ts"), "private queue(")
        assert "this.chain" in body and ".then(" in body

    def test_keeping_a_pin_sends_the_last_edit_first(self) -> None:
        body = body_of(source("pinimport.ts"), "private async decide(")
        assert "await this.flush()" in body


class TestReviewingImportedPinsHidesTheRest:
    def test_the_existing_pins_go_away_too(self) -> None:
        # Not just the fog and the tracks: three hundred pins already on the
        # map is exactly what makes one more unreadable.
        text = source("main.ts")
        assert "places.suspend(!visible)" in text

    def test_and_come_back_when_the_sidebar_closes(self) -> None:
        body = body_of(source("pinimport.ts"), "private leave(")
        assert "setRestVisible(true)" in body

    def test_closing_sideways_is_heard_about(self) -> None:
        assert "pins.closed()" in source("main.ts")


class TestReFogIsOnlyALabel:
    """The button was renamed in 0.18.1. What it writes was not.

    It was called Eraser and it does not erase a track - it puts the fog back
    and leaves the line that cleared it alone. Reported as "the eraser only
    deletes the fog... is this a bug?", which it was not: the label was.

    The op stays `erase`, because it is in every archive and every backup ever
    exported from one. So the label and the wire are allowed to disagree, and
    this says so - the obvious tidy-up is to make them match, and that one is
    a migration for no gain.
    """

    def test_the_tool_still_writes_an_erase(self) -> None:
        assert "eraser: 'erase'" in source("draw.ts")

    def test_the_button_no_longer_claims_to_erase(self) -> None:
        markup = (WEB / "index.html").read_text()
        assert "Re-Fog" in markup
        assert ">Eraser<" not in markup

    def test_the_hint_says_the_track_survives(self) -> None:
        # The one thing somebody reaching for this tool needs to know.
        hint = re.search(r"eraser: '([^']*)'", source("main.ts"))
        assert hint, "the tool has no hint at all"
        assert "track" in hint.group(1).lower()


class TestTheReviewBadge:
    """What is waiting has to still be there when you come back to the tab.

    The one thing the badge must not be is a notice: notice() arms a timer
    that hides good news after four seconds, and something asking for a
    decision that quietly removes itself is the same as no badge at all.
    """

    def test_the_badge_is_not_a_self_hiding_notice(self) -> None:
        text = source("review.ts") + source("main.ts")
        assert "notice('review-badge')" not in text
        assert 'notice("review-badge")' not in text

    def test_the_markup_does_not_make_it_one_either(self) -> None:
        markup = (WEB / "index.html").read_text()
        line = next(
            row for row in markup.splitlines() if 'id="review-badge"' in row
        )
        assert "notice" not in line, "a .notice is styled and treated as transient"

    def test_it_is_hidden_when_nothing_can_be_decided(self) -> None:
        # Reading the map needs no token. Offering a decision that the server
        # will refuse is worse than not mentioning it.
        body = body_of(source("review.ts"), "private paintBadge(")
        assert "getToken()" in body
        assert "hidden = true" in body


class TestTheReviewPreview:
    """Trimming redraws as the slider moves, so it cannot ask the server."""

    def test_drawing_the_preview_makes_no_request(self) -> None:
        body = body_of(source("review.ts"), "private redraw(")
        for call in ("apiGet", "apiSend", "fetch("):
            assert call not in body, (
                f"redraw() calls {call}, so every pixel of slider travel is a "
                "round trip"
            )

    def test_the_edit_itself_is_debounced(self) -> None:
        body = body_of(source("review.ts"), "private later(")
        assert "setTimeout" in body and "clearTimeout" in body

    def test_a_slider_drag_redraws_locally_first(self) -> None:
        text = source("review.ts")
        assert "this.redraw()" in text and "this.later()" in text


class TestAcceptingATrackShowsOnTheBar:
    """Reported as: accepting a track draws the map and the bar never moves.

    Drawing has followed the queue since strokes stopped rendering inline.
    Accepting a reviewed track deferred exactly the same render and told
    nobody, so the map redrew itself in silence.
    """

    def test_there_is_one_follower_and_the_review_uses_it(self) -> None:
        text = source("main.ts")
        assert "async function followTheQueue(" in text
        assert "void followTheQueue(drawStatus, summary)" in text

    def test_it_watches_the_queue_rather_than_guessing(self) -> None:
        body = body_of(source("main.ts"), "async function followTheQueue(")
        assert "watchRender(" in body

    def test_it_puts_a_bar_up_before_the_first_poll(self) -> None:
        # A small accept can finish before a poll comes back, and a bar that
        # never appears is indistinguishable from one that is broken.
        body = body_of(source("main.ts"), "async function followTheQueue(")
        assert "status.progress(0, 0" in body

    def test_it_ends_on_a_message_not_on_a_bar(self) -> None:
        # Painting progress cancels the timer that hides a notice, which is how
        # a bar came to sit at three quarters for good.
        body = body_of(source("main.ts"), "async function followTheQueue(")
        assert "status.show(" in body

    def test_only_one_notice_owns_that_element(self) -> None:
        # notice() replaces the element's contents, so two of them over the
        # same id quietly take each other's children away.
        assert source("main.ts").count("notice('draw-status')") == 1


class TestHandingAGapToTheDrawingTools:
    """The review does not get its own brush; the real one gets a door."""

    def test_the_review_asks_rather_than_draws(self) -> None:
        text = source("review.ts")
        assert "this.onDrawGap(" in text
        for own in ("addSource('irfaran-draw", "pointerdown", "undoStack"):
            assert own not in text, "the review is growing its own drawing tools"

    def test_it_saves_before_leaving(self) -> None:
        # A trim or a rename made a moment ago must survive the detour.
        body = body_of(source("review.ts"), "  private handOver(")
        assert "this.flush()" in body

    def test_only_a_cut_gap_offers_it(self) -> None:
        body = body_of(source("review.ts"), "  private paintGaps(")
        assert "if (gap.cut) {" in body

    def test_the_tools_can_be_armed_and_put_away(self) -> None:
        text = source("main.ts")
        assert "arm(tool: Tool)" in text and "putAway()" in text

    def test_the_camera_arrives_before_the_tool_is_armed(self) -> None:
        # Drawing is locked out below z14, so easing there and arming would
        # arm nothing.
        text = source("main.ts")
        assert "map.jumpTo(" in text
        assert "Math.max(MIN_DRAW_ZOOM" in text

    def test_it_comes_back_afterwards(self) -> None:
        text = source("main.ts")
        assert "backFromDrawing" in text
        assert "sheets.open('review-page')" in text

    def test_the_layer_is_put_back(self) -> None:
        # The hand-drawn piece goes into the track's year, and the field the
        # person had set is theirs, not ours.
        text = source("main.ts")
        assert "draw.layers = before" in text


class TestWhatThePhoneSaidIsShown:
    def test_a_gap_says_the_accuracy_and_the_motion(self) -> None:
        body = body_of(source("review.ts"), "function describeEnds(")
        assert "accuracy_before" in body and "motion_before" in body

    def test_and_says_nothing_when_the_source_said_nothing(self) -> None:
        body = body_of(source("review.ts"), "function describeEnds(")
        assert "parts.join" in body, "it has to be able to come back empty"


class TestWhereADayBreaksInTheReview:
    """A cut has to be visible, reversible, and the same one that lands."""

    def test_the_gaps_carry_their_own_numbers(self) -> None:
        body = body_of(source("review.ts"), "  private paintGaps(")
        for shown in ("metres", "seconds", "ratio"):
            assert shown in body, (
                f"a gap is shown without its {shown}, so the threshold that "
                "decided it cannot be judged"
            )

    def test_a_cut_can_be_undone_and_a_join_made(self) -> None:
        body = body_of(source("review.ts"), "  private async toggleGap(")
        assert "this.cuts" in body and "this.joins" in body

    def test_the_two_are_kept_apart(self) -> None:
        # Undoing the rule's cut is a join; undoing your own is deleting the
        # cut. Collapsing them would make a join evaporate the moment the
        # thresholds changed.
        body = body_of(source("review.ts"), "  private async toggleGap(")
        assert "gap.reason !== ''" in body

    def test_a_save_carries_them(self) -> None:
        body = body_of(source("review.ts"), "private async saveNow(")
        assert "cuts:" in body and "joins:" in body

    def test_parts_are_named_by_where_they_start(self) -> None:
        # Not by ordinal: adding a cut renumbers every part after it, and a
        # drop that quietly moved to the next part is worse than no control.
        text = source("review.ts")
        assert "String(segment.begin)" in text
        assert "String(segment.index)" not in text


class TestReviewEditsCannotOvertakeEachOther:
    """Two edits in flight together lost one of them.

    The server reads the current edits, applies the change and writes them
    back. Sending the trim and the unticked part as separate requests meant
    both read the same starting point and the second silently undid the first
    - with the panel still showing both. Found by unticking a part and then
    renaming the batch: the part came back.
    """

    def test_a_save_carries_the_whole_decision(self) -> None:
        body = body_of(source("review.ts"), "private async saveNow(")
        for field in ("title", "from", "to", "dropped"):
            assert field in body, (
                f"a save without {field} is a fragment applied to whatever the "
                "server happens to hold"
            )

    def test_saves_are_chained(self) -> None:
        body = body_of(source("review.ts"), "private queue(")
        assert "this.chain" in body and ".then(" in body

    def test_accepting_sends_the_last_edit_first(self) -> None:
        body = body_of(source("review.ts"), "private async approve(")
        assert "await this.flush()" in body, (
            "a trim made a quarter of a second before pressing the button "
            "would not be part of what is accepted"
        )


class TestLeavingAReviewPutsTheMapBack:
    """The sidebar hides the fog and the trails while it is open.

    Every way of closing it has to restore them - the close button, the Escape
    key, and opening Places or Settings, none of which the sidebar hears about
    on its own.
    """

    def test_the_sheets_report_every_change(self) -> None:
        body = body_of(source("ui.ts"), "export class Sheets {")
        assert body.count("this.onChange()") >= 2, (
            "open() and close() must both report, or closing sideways leaves "
            "the map blank"
        )

    def test_main_listens_and_tells_the_review(self) -> None:
        text = source("main.ts")
        assert "review.closed()" in text
        assert "'review-page'" in text, "the sidebar is not one of the sheets"

    def test_closing_restores_what_was_hidden(self) -> None:
        body = body_of(source("review.ts"), "private closeOne(")
        assert "setRestVisible(true)" in body


class TestTheImportLog:
    """It shows what fitted, and scrolls for the rest.

    It used to keep the last six outcomes, which on a seventy-file drop threw
    away most of the answer. The cap existed so a long list would not grow past
    the panel; the panel scrolling is the better answer to that, and it took
    measuring at three window heights - 4 rows at 560px, 13 at 800, 24 at 1100 -
    to know the layout actually yields.
    """

    def test_no_fixed_number_of_rows_is_kept(self) -> None:
        text = source("imports.ts")
        body = body_of(text, "private report(")
        capped = re.search(r"slice\(\s*-\s*\d+\s*\)", body)
        assert capped is None, (
            f"the import log is capped again at {capped.group(0)}, so most of a "
            "large import is thrown away rather than scrolled"
        )

    def test_the_log_scrolls_and_can_shrink(self) -> None:
        """Without min-height: 0 it pushes the section out instead of scrolling."""
        css = source("style.css")
        block = css[css.index(".import-log {") : css.index(".import-log:empty")]
        assert "overflow-y: auto" in block
        assert "min-height: 0" in block
        assert "flex: 1 1 auto" in block

    def test_an_empty_log_takes_no_room(self) -> None:
        """Before the first import, the section should not reserve a screenful."""
        assert ".import-log:empty" in source("style.css")

    def test_only_the_import_tab_is_stretched(self) -> None:
        """The other tabs are cards that end where their content does."""
        css = source("style.css")
        assert ".tab-panel[data-tab='import']" in css
        # `\n  height` rather than `height`, or max-height on the section
        # inside the same block counts as a second stretched tab.
        stretched = sorted(
            set(re.findall(r"\.tab-panel\[data-tab='(\w+)'\][^{]*\{[^}]*\n  height: 100%", css))
        )
        assert stretched == ["import"], f"stretched tabs: {stretched}"

    def test_the_section_keeps_a_floor(self) -> None:
        """On a short window the log must not collapse to nothing."""
        css = source("style.css")
        block = css[css.index(".tab-panel[data-tab='import'] > section") :]
        assert "min-height" in block[:400]


class TestTypeAhead:
    """Suggestions as you type, without travelling and without racing itself.

    Two properties matter more than the feature. Typing must not move the map: an
    intermediate coordinate parses perfectly well - "27.74367, -1" is a real
    place off the coast of Africa - so flying there on the way to somewhere else
    is worse than not moving at all. And answers can come back out of order, so a
    slow reply to "cao" must not overwrite the results for "caorle".

    Measured in the browser: typing a six-letter word costs one request, the map
    stayed at z2.0 throughout, and Enter afterwards dropped the marker that
    typing had not.
    """

    def source_of(self) -> str:
        return source("search.ts")

    def test_typing_is_debounced(self) -> None:
        text = self.source_of()
        assert "'input'" in text, "nothing listens for typing"
        body = text[text.index("addEventListener('input'") :][:600]
        assert "clearTimeout" in body and "setTimeout" in body, (
            "typing is not debounced, so a six-letter word is six requests"
        )

    def test_short_queries_are_not_sent(self) -> None:
        assert "MIN_QUERY" in self.source_of()

    def test_suggesting_does_not_move_the_map(self) -> None:
        """The whole distinction: suggest looks, go travels."""
        body = body_of(self.source_of(), "private async suggest(")
        assert "flyTo" not in body and "fitBounds" not in body, (
            "suggesting moves the map, so typing towards a coordinate flies "
            "through the wrong places on the way"
        )
        assert "this.go(" not in body

    def test_going_is_what_enter_does(self) -> None:
        body = body_of(self.source_of(), "private async run(")
        assert "this.go(" in body

    def test_stale_answers_are_dropped(self) -> None:
        """Out-of-order replies would otherwise show the wrong query's results."""
        body = body_of(self.source_of(), "private async suggest(")
        assert "this.asked" in body, "no guard against an answer arriving late"
        assert body.count("!== this.asked") >= 2, (
            "the late-answer guard has to cover the failure path as well"
        )


class TestStatisticsIsItsOwnPage:
    """Off the toolbar rather than out of Settings.

    It shipped as the thirteenth tab of a settings sheet, which is not where
    anybody looks for what their archive adds up to - and none of it is a
    setting: there is nothing on that page to change, only counts of what is
    already there.
    """

    def test_there_is_a_button_beside_the_pin_switch(self) -> None:
        markup = (WEB / "index.html").read_text()
        tools = markup.index('class="map-tools"')
        pins = markup.index('id="pins-toggle"')
        stats = markup.index('id="stats-toggle"')
        assert tools < pins < stats, "the statistics button left the map tools"

    def test_it_is_a_lower_case_i(self) -> None:
        assert 'data-icon="info"' in (WEB / "index.html").read_text()
        # icon() throws for a name it does not know, which would take out
        # hydrateIcons and every icon in the static chrome with it.
        assert "\n  info: [" in source("icons.ts")

    def test_it_is_no_longer_a_settings_tab(self) -> None:
        markup = (WEB / "index.html").read_text()
        assert 'data-tab="statistics"' not in markup, (
            "the page is in two places at once; wireTabs hides every "
            ".tab-panel in the document, including one it has no button for"
        )

    def test_the_sheet_closes_the_others(self) -> None:
        # Sheets is what makes them mutually exclusive, and what Escape and
        # the close buttons go through. A sheet outside that list stays open
        # underneath whatever is opened next.
        text = source("main.ts")
        line = next(l for l in text.splitlines() if "'places-page', 'review-page'" in l)
        assert "'stats-page'" in line

    def test_the_figures_are_fetched_when_it_is_opened(self) -> None:
        # Reading every fog blob in the archive is not something to do on
        # startup for a page nobody has asked for.
        body = body_of(source("main.ts"), "  wirePart('stats', () => {")
        assert "sheets.toggle('stats-page')" in body
        assert "stats.watch(" in body

    def test_it_needs_no_token(self) -> None:
        markup = (WEB / "index.html").read_text()
        gated = gated_ids(markup)
        for name in ("stats-toggle", "stats-page", "stat-area", "stats-refresh"):
            assert name in markup, f"{name} is gone from the markup"
            assert name not in gated, (
                f"{name} is behind the token gate. These are counts of "
                "somebody's own map, and the map reads without a token."
            )


class TestTheCountryListStopsGrowing:
    """Seventeen countries is a page that pushes everything else off the end.

    The list is one section of six, and it is the only one that grows without
    limit as more of the world is walked.
    """

    def block(self) -> str:
        css = source("style.css")
        return css[css.index(".country-list {") : css.index(".country-area {")]

    def test_it_scrolls_on_its_own(self) -> None:
        block = self.block()
        assert "overflow-y: auto" in block
        assert "max-height:" in block

    def test_it_stops_at_fifteen(self) -> None:
        assert "--country-rows: 15;" in self.block()

    def test_the_cap_is_arithmetic_rather_than_a_guess(self) -> None:
        # A height in rem picked to look about right is a height that is wrong
        # at another font size. The rows are told what they are.
        block = self.block()
        assert "height: var(--country-row);" in block
        assert "box-sizing: border-box" in block
        assert "var(--country-rows)" in block and "var(--country-gap)" in block

    def test_a_long_name_shortens_rather_than_wrapping(self) -> None:
        # Fixed-height rows clip a second line mid-letter.
        block = self.block()
        assert "text-overflow: ellipsis" in block


class TestGlobeOrMercator:
    """A round world when you are looking at the world.

    MapLibre's `globe` is not "always a sphere" - it transitions to Mercator on
    the way in, so the world is round at world zoom and flat by the time it is
    a street. Everything Irfaran draws is a stock raster, line or vector layer,
    which is why this costs one style property rather than a rewrite: a custom
    WebGL layer is the thing that does not survive a reprojection.
    """

    def markup(self) -> str:
        return (WEB / "index.html").read_text()

    def test_it_is_a_style_property_not_only_a_live_call(self) -> None:
        # applyMapTheme is setStyle with `diff: false`, which resets everything
        # the style owns. A projection set only on the live map is lost with
        # the rest of it the first time somebody switches theme.
        body = body_of(source("map.ts"), "export function buildStyle(")
        assert "projection:" in body and "getMapProjection()" in body

    def test_the_globe_is_the_default(self) -> None:
        body = body_of(source("map.ts"), "export function getMapProjection(")
        assert "'mercator'" in body
        # Read as "mercator or else the globe", so a stored value from a
        # version that offered something else lands somewhere that exists.
        assert body.index("? 'mercator'") < body.index(": 'globe'")

    def test_only_the_two_that_maplibre_can_draw_are_types(self) -> None:
        # Three projections are compiled into MapLibre's shaders: mercator,
        # globe and vertical-perspective. Equal Earth is not one of them, so
        # nothing may be able to store it.
        text = source("map.ts")
        assert "export type MapProjection = 'mercator' | 'globe'" in text

    def test_equal_earth_is_shown_and_switched_off(self) -> None:
        markup = self.markup()
        start = markup.index('id="map-projection"')
        block = markup[start : markup.index("</div>", start)]
        assert 'data-value="equal-earth"' in block, (
            "the placeholder is gone; an absent option cannot say why it is "
            "absent"
        )
        # A disabled button dispatches no click, so radioGroup cannot pick it.
        after = block[block.index('data-value="equal-earth"') :]
        assert "disabled" in after[: after.index(">")], (
            "the Equal Earth button is live, so it can be selected and will "
            "store a projection MapLibre cannot draw"
        )

    def test_switching_does_not_restyle(self) -> None:
        # Nothing about the fog, the tracks or the basemap changes - only how
        # the same tiles are laid out on the screen.
        body = body_of(source("map.ts"), "export function applyProjection(")
        assert "setProjection(" in body
        assert "setStyle" not in body

    def test_a_click_before_the_style_is_ready_is_not_lost(self) -> None:
        # setProjection throws while the style is still being parsed.
        body = body_of(source("map.ts"), "export function applyProjection(")
        assert "catch" in body and "once('load'" in body

    def test_it_is_a_browser_choice_and_needs_no_token(self) -> None:
        for name in ("getMapProjection(", "setMapProjection("):
            body = body_of(source("map.ts"), f"export function {name}")
            assert "localStorage" in body
            for call in ("apiGet", "apiSend", "fetch("):
                assert call not in body, f"{name} talks to the server"
        assert "map-projection" not in gated_ids(self.markup()), (
            "the projection is drawn by this browser and needs no token"
        )

    def test_the_choice_survives_a_reload(self) -> None:
        body = body_of(source("map.ts"), "export function setMapProjection(")
        assert "localStorage.setItem" in body


class TestNothingShowsThroughTheEarth:
    """Pins on the far side of the globe, which MapLibre fades but does not hide.

    A covered marker is drawn at 20% opacity by default, so a globe centred on
    Europe carried a column of ghost pins over the Pacific - Austria, seen
    through the planet. The library knows they are occluded; it just does not
    do anything final about it.
    """

    def test_every_marker_and_popup_comes_from_one_place(self) -> None:
        # Five call sites across four files, and the next one will not
        # remember. Constructing them anywhere else means a pin that shows
        # through the earth again.
        loose = []
        for path in sources():
            if path.name == "markers.ts":
                continue
            text = path.read_text()
            if "new Marker(" in text or "new Popup(" in text:
                loose.append(path.name)
        assert not loose, (
            f"{', '.join(loose)} builds its own marker or popup, so it does "
            "not know about the globe. Use mapMarker/mapPopup from markers.ts."
        )

    def test_a_covered_pin_is_gone_rather_than_faded(self) -> None:
        text = source("markers.ts")
        assert "const COVERED = 0" in text
        assert "opacityWhenCovered: COVERED" in text
        assert "locationOccludedOpacity: COVERED" in text

    def test_a_call_site_can_still_say_what_it_wants(self) -> None:
        # The spread goes last, so a marker that needs its own element or
        # colour is not overwritten by the default it is being given.
        text = source("markers.ts")
        for line in ("opacityWhenCovered: COVERED", "locationOccludedOpacity: COVERED"):
            at = text.index(line)
            assert "...options" in text[at : text.index("}", at)], line

    def test_an_invisible_pin_does_not_swallow_a_click(self) -> None:
        # The half of "not shown" that opacity does not cover: a marker at
        # zero opacity is still a target, so a pin in New Zealand would eat a
        # click meant for the Atlantic in front of it.
        css = source("style.css")
        block = css[css.index(".maplibregl-marker-covered {") :][:200]
        assert "pointer-events: none" in block


class TestStillCollectingIsAboutToday:
    """The badge on a waiting batch asked the wrong question.

    Reported from the live instance: the 5th and the 6th were waiting, and on
    the 7th the 5th still said *still collecting*. It read `sealed`, and a
    batch stays unsealed until somebody opens it - so every day nobody had
    reviewed claimed to be filling up.
    """

    def test_the_badge_asks_whether_it_is_still_filling(self) -> None:
        body = body_of(source("review.ts"), "  private paintList(): void {")
        assert "item.collecting" in body
        assert "!item.sealed" not in body, (
            "unsealed means joinable, not filling - every unreviewed day is "
            "unsealed"
        )

    def test_the_server_decides_which_day_is_today(self) -> None:
        # The day keys are UTC. At one in the morning in Vienna the browser is
        # already on tomorrow while the batch taking points is still today's,
        # so this comparison cannot be made in the browser.
        import inspect

        from irfaran import review

        text = inspect.getsource(review)
        assert '"collecting"' in text and "_today()" in text

        # And the browser does not work it out for itself.
        body = body_of(source("review.ts"), "  private paintList(): void {")
        assert "Date" not in body, (
            "the list is comparing dates in the browser, which is the local "
            "day rather than the day the batches are keyed by"
        )
