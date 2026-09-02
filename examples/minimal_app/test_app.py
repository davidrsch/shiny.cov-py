"""End-to-end verification that shinycov's blended coverage moves
between "untested" and "tested" for a known interaction pattern: `go` (the
action button) is clicked, `unused` (the text input) initially isn't.

The before/after comparison happens inside a single test rather than across
two, because the plugin's own printed "blended coverage" percentage is only
computed once, at the very end of the whole pytest session
(`pytest_sessionfinish` runs once, not per test) -- there's no separate
per-test percentage to compare across two test functions within one
session. `shinycov.controllers`' interaction log is available at any
point, though, and is what the final percentage is built from, so asserting
directly on it (via the same `_hit_ids()` helper `plugin.py` itself uses)
gives a real, programmatic before/after within one test, the same as the R
suite's equivalent assertion.
"""
from playwright.sync_api import Page
from shiny.playwright import controller
from shiny.pytest import create_app_fixture
from shiny.run import ShinyAppProc

from shinycov import controllers
from shinycov.plugin import _hit_ids

app = create_app_fixture("app.py")


def test_click_go_button(page: Page, app: ShinyAppProc):
    page.goto(app.url)

    go = controller.InputActionButton(page, "go")
    go.click()

    clicks = controller.OutputText(page, "clicks")
    clicks.expect_value("Clicked 1 times")


def test_blended_coverage_moves_when_previously_untested_input_is_touched(
    page: Page, app: ShinyAppProc
):
    # Deliberately doesn't call controllers.reset(): the interaction log is
    # session-global (see module docstring), and clearing it here would
    # also erase test_click_go_button's already-recorded hits from the
    # final session-end report -- this test only needs `unused` itself to
    # start untested, which holds regardless of what ran before it, since
    # no other test in this module touches `unused`.
    page.goto(app.url)

    hits_before = _hit_ids(controllers.get_interactions())
    assert "unused" not in hits_before

    unused = controller.InputText(page, "unused")
    unused.set("hello")

    hits_after = _hit_ids(controllers.get_interactions())
    assert hits_after == hits_before | {"unused"}
