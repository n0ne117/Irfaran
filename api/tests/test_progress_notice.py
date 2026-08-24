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

from .test_markup import web_dir

WEB = web_dir()


def source(name: str) -> str:
    path = WEB / "src" / name
    assert path.is_file(), f"{name} is missing, so this test checks nothing"
    return path.read_text()


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
