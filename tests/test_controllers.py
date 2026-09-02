"""Tests for the MRO-walking UiBase instrumentation in
shinycov.controllers.

Uses MagicMock(spec=Locator)/(spec=Page) rather than real Playwright
browser objects: `UiWithContainer.__init__` (a base class in py-shiny's
private `_base.py`) does `isinstance(loc_container, Locator)` checks, so a
bare MagicMock() fails construction -- `spec=` is what makes the mock pass
those isinstance() checks while still recording calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import shiny.playwright.controller as controller
from playwright.sync_api import Locator, Page
from shiny.playwright.controller._base import UiBase

from shinycov import controllers


def make_mock_page():
    loc = MagicMock(spec=Locator)
    loc.locator.return_value = loc
    page = MagicMock(spec=Page)
    page.locator.return_value = loc
    # Not a real manifest -- the wrapper must not choke on this, and must
    # not treat it as a manifest snapshot to merge in (see the isinstance
    # check in controllers._record_manifest_snapshot).
    page.evaluate.return_value = MagicMock()
    return page


@pytest.fixture(autouse=True)
def _reset_log():
    controllers.reset()
    yield
    controllers.reset()


def test_instrument_reaches_classes_outside_vars_module():
    # The regression this guards: a naive `vars(shiny.playwright.controller)`-only
    # pass misses methods defined on private mixins/bases (InputActionBase.click,
    # UiWithLabel.expect_label, ...) that aren't re-exported from the public
    # controller module namespace, so it silently under-instruments.
    all_classes = controllers._all_controller_classes()
    exported_names = {
        name for name, obj in vars(controller).items() if isinstance(obj, type)
    }
    private_only = {c.__name__ for c in all_classes if c.__name__ not in exported_names}

    assert len(all_classes) > len(exported_names)
    assert "InputActionBase" in private_only or "UiWithLabel" in private_only


def test_instrument_reaches_output_controllers_not_just_ui_base():
    # The regression this guards: OutputText (and every other output
    # controller) subclasses a completely separate _OutputBase hierarchy,
    # not UiBase -- a scan filtered by "is a UiBase subclass" silently
    # skips every output controller, including where expect_value() lives.
    all_classes = controllers._all_controller_classes()
    assert not issubclass(controller.OutputText, UiBase)
    assert controller.OutputText in all_classes
    assert any(c.__name__ == "_OutputTextValue" for c in all_classes)


def test_instrument_wraps_click_and_preserves_delegation():
    controllers.instrument()

    mock_page = make_mock_page()
    btn = controller.InputActionButton(mock_page, id="go")
    btn.click(timeout=123)

    btn.loc.click.assert_called_with(timeout=123)

    interactions = controllers.get_interactions()
    assert len(interactions) == 1
    entry = interactions[0]
    assert entry["selector"] == "#go"
    assert entry["action"] == "click"
    assert "timestamp" in entry


def test_instrument_wraps_methods_on_private_mixin_classes():
    controllers.instrument()

    mock_page = make_mock_page()
    slider = controller.InputSlider(mock_page, id="n")
    # expect_label() lives on the private UiWithLabel mixin, not on
    # InputSlider itself and not re-exported from vars(controller).
    try:
        slider.expect_label("N")
    except Exception:
        # playwright.expect() needs internals a bare mock can't fully
        # satisfy -- irrelevant to what this test checks, which is that
        # the wrapper fired *before* any such failure.
        pass

    interactions = controllers.get_interactions()
    assert any(e["action"] == "expect_label" and e["selector"] == "#n" for e in interactions)


def test_instrument_wraps_output_controller_verification_methods():
    # expect_value() lives on OutputText's private _OutputTextValue mixin,
    # under the _OutputBase hierarchy entirely separate from UiBase (see
    # controllers.py's module docstring) -- output verification calls must
    # be logged too, the same as input interactions, since shiny.cov's
    # blended coverage treats "output was verified" as equally load-bearing
    # as "input was interacted with" (mirroring shinycov_output_actions()
    # on the R side).
    controllers.instrument()

    mock_page = make_mock_page()
    output = controller.OutputText(mock_page, "clicks")
    try:
        output.expect_value("Clicked 1 times")
    except Exception:
        pass  # Same mock-vs-real-Locator caveat as expect_label() above.

    interactions = controllers.get_interactions()
    assert any(
        e["action"] == "expect_value" and e["selector"] == "#clicks" for e in interactions
    )


def test_instrument_is_idempotent():
    # Instrumentation is genuinely process-global (it monkeypatches the
    # real py-shiny classes, not a per-test fixture), so an earlier test in
    # this file may have already wrapped everything -- the count from a
    # given call can legitimately be 0. What must always hold is that a
    # second call back-to-back never finds anything new to wrap.
    controllers.instrument()
    second = controllers.instrument()
    assert second == 0


def test_reset_clears_interactions_and_manifest():
    controllers.instrument()
    mock_page = make_mock_page()
    btn = controller.InputActionButton(mock_page, id="go")
    btn.click()
    assert controllers.get_interactions()

    controllers.reset()
    assert controllers.get_interactions() == []
    assert controllers.get_manifest() is None


def test_manifest_snapshot_merges_real_dict_from_page_evaluate():
    controllers.instrument()
    mock_page = make_mock_page()
    mock_page.evaluate.return_value = {
        "inputs": [{"id": "go", "type": "shiny.actionButtonInput", "label": "Go"}],
        "outputs": [],
        "tabs": [],
        "conditional": [],
        "modules": [],
    }
    btn = controller.InputActionButton(mock_page, id="go")
    btn.click()

    manifest = controllers.get_manifest()
    assert manifest is not None
    assert manifest["inputs"][0]["id"] == "go"


def test_manifest_snapshot_is_skipped_when_evaluate_returns_non_dict():
    controllers.instrument()
    mock_page = make_mock_page()  # evaluate() returns a bare MagicMock, not a dict
    btn = controller.InputActionButton(mock_page, id="go")
    btn.click()

    assert controllers.get_manifest() is None


def test_manifest_snapshot_with_malformed_element_does_not_break_the_call():
    # discover-bindings.js runs in a live browser and its return value is
    # arbitrary JS-turned-JSON, not internally-constructed data -- a
    # malformed element reaching the module-global merge must never
    # surface as an exception out of the wrapped controller method, since
    # _record_manifest_snapshot() runs in every wrapper's finally block and
    # would otherwise turn an already-successful interaction into a raised
    # exception for the rest of the session.
    controllers.instrument()
    mock_page = make_mock_page()
    mock_page.evaluate.return_value = {
        "inputs": ["not-a-dict"],
        "outputs": [],
        "tabs": [],
        "conditional": [],
        "modules": [],
    }
    btn = controller.InputActionButton(mock_page, id="go")

    btn.click(timeout=123)  # must not raise

    interactions = controllers.get_interactions()
    assert len(interactions) == 1
    assert interactions[0]["action"] == "click"

    manifest = controllers.get_manifest()
    assert manifest is not None
    assert manifest["inputs"] == []


def test_manifest_snapshot_with_malformed_tab_entry_does_not_break_the_call():
    # Same hardening as the inputs/outputs case above, but for
    # tabs/conditional/modules -- these feed a hashability-requiring
    # dedupe step, so a non-string entry is a distinct failure mode from
    # a malformed input/output element and needs its own coverage.
    controllers.instrument()
    mock_page = make_mock_page()
    mock_page.evaluate.return_value = {
        "inputs": [],
        "outputs": [],
        "tabs": [{"weird": "dict"}],
        "conditional": [],
        "modules": [],
    }
    btn = controller.InputActionButton(mock_page, id="go")

    btn.click(timeout=123)  # must not raise

    interactions = controllers.get_interactions()
    assert len(interactions) == 1
    assert interactions[0]["action"] == "click"
