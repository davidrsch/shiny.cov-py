"""Tests for shinycov.plugin -- both as a registered pytest plugin hook
(via the `pytester` fixture) and at the unit level for pieces that are
awkward to observe end-to-end (retry counts, the xdist guard).

`test_coverage_integration.py` exercises the coverage.py mechanics
`plugin.py` depends on by calling its helper functions directly;
`pytest_sessionfinish()` itself -- the actual registered hook, combine/
report/print included -- had no test coverage of its own before this file.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import coverage
import pytest

from shinycov import plugin


# ---------------------------------------------------------------------------
# pytest_sessionfinish() as a real, entry-point-registered hook.
#
# `runpytest_subprocess()` (not the in-process `runpytest()`) is used
# deliberately: it spawns a genuinely separate Python process, so
# `shinycov.controllers`' module-global manifest/interaction state starts
# clean rather than potentially carrying over whatever this outer test
# session has already recorded.
# ---------------------------------------------------------------------------

_MOCK_PAGE_HELPER = '''
from unittest.mock import MagicMock
from playwright.sync_api import Locator, Page


def _make_mock_page(manifest):
    loc = MagicMock(spec=Locator)
    loc.locator.return_value = loc
    page = MagicMock(spec=Page)
    page.locator.return_value = loc
    page.evaluate.return_value = manifest
    return page
'''


def test_sessionfinish_hook_writes_ui_elements_file_and_reports_hits(pytester: pytest.Pytester):
    pytester.makepyfile(
        test_app=_MOCK_PAGE_HELPER
        + '''
from shiny.playwright import controller


def test_click_go_button():
    manifest = {
        "inputs": [{"id": "go", "type": "shiny.actionButtonInput", "label": "Go"}],
        "outputs": [],
        "tabs": [], "conditional": [], "modules": [],
    }
    page = _make_mock_page(manifest)
    btn = controller.InputActionButton(page, id="go")
    btn.click()
'''
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*shiny.cov: blended coverage*(1/1 UI elements interacted with)*"])

    ui_file = pytester.path / ".shiny.cov" / "ui_elements.py"
    assert ui_file.exists()
    contents = ui_file.read_text()
    assert "id='go'" in contents
    assert "True" in contents  # `go` was hit, so its generated statement is `= True`


def test_sessionfinish_hook_distinguishes_discovery_never_ran(pytester: pytest.Pytester):
    # No controller method's page.evaluate() ever returns a manifest dict
    # here (it returns the default MagicMock, same as a mock/closed page
    # would) -- discovery never actually ran, which must print differently
    # than "ran and found zero elements."
    pytester.makepyfile(
        test_app=_MOCK_PAGE_HELPER
        + '''
from shiny.playwright import controller


def test_click_go_button():
    page = _make_mock_page(MagicMock())
    btn = controller.InputActionButton(page, id="go")
    btn.click()
'''
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    # The diagnostic line is what this test guards -- it must appear
    # whenever `manifest` is None, regardless of the final hit/total counts
    # on the summary line below it (hit_ids is computed from logged
    # interactions independently of whether a manifest was ever discovered,
    # so a call can register as a "hit" even with zero known elements to
    # attribute it to).
    result.stdout.fnmatch_lines(["*shiny.cov: no UI manifest discovered*"])
    result.stdout.fnmatch_lines(["*shiny.cov: blended coverage*UI elements interacted with)*"])


# ---------------------------------------------------------------------------
# _combine_with_retry()'s actual retry count/delay behavior.
# ---------------------------------------------------------------------------

def _fake_cov_that_fails_n_times(n: int) -> MagicMock:
    calls = {"count": 0}

    def combine(strict: bool = False, keep: bool = False) -> None:
        calls["count"] += 1
        if calls["count"] <= n:
            raise coverage.misc.CoverageException("no data to combine")

    fake_cov = MagicMock()
    fake_cov.combine.side_effect = combine
    fake_cov._calls = calls
    return fake_cov


def test_combine_with_retry_stops_as_soon_as_combine_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(plugin.time, "sleep", sleeps.append)

    fake_cov = _fake_cov_that_fails_n_times(2)  # fails on attempts 1-2, succeeds on 3
    plugin._combine_with_retry(fake_cov, attempts=5, delay=0.3)

    assert fake_cov.combine.call_count == 3
    assert sleeps == [0.3, 0.3]  # one sleep between each of the first two failed attempts


def test_combine_with_retry_prints_diagnostic_when_every_attempt_fails(monkeypatch, capsys):
    monkeypatch.setattr(plugin.time, "sleep", lambda _delay: None)

    fake_cov = _fake_cov_that_fails_n_times(n=999)  # never succeeds
    plugin._combine_with_retry(fake_cov, attempts=4, delay=0.01)

    assert fake_cov.combine.call_count == 4
    out = capsys.readouterr().out
    assert "shiny.cov: no parallel coverage data files found after 4 attempts" in out
    assert "sigterm" in out


def test_combine_with_retry_prints_nothing_when_combine_succeeds_immediately(monkeypatch, capsys):
    monkeypatch.setattr(plugin.time, "sleep", lambda _delay: None)

    fake_cov = _fake_cov_that_fails_n_times(0)
    plugin._combine_with_retry(fake_cov, attempts=5, delay=0.01)

    assert fake_cov.combine.call_count == 1
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# xdist detection.
# ---------------------------------------------------------------------------

def _fake_config(numprocesses, is_worker: bool) -> MagicMock:
    config = MagicMock()
    config.option.numprocesses = numprocesses
    if is_worker:
        config.workerinput = {}
    else:
        del config.workerinput  # MagicMock auto-creates attrs -- must be absent, not falsy
    return config


def test_warns_when_numprocesses_set_on_controller_process(capsys):
    plugin._warn_if_xdist_active(_fake_config(numprocesses=4, is_worker=False))
    out = capsys.readouterr().out
    assert "pytest-xdist" in out
    assert "-n 4" in out


def test_no_warning_when_numprocesses_not_set(capsys):
    plugin._warn_if_xdist_active(_fake_config(numprocesses=None, is_worker=False))
    assert capsys.readouterr().out == ""


def test_no_warning_from_worker_process_itself(capsys):
    # The controller process already warned once at configure time; a
    # worker's own config also carries numprocesses (inherited), so without
    # the is_worker check every worker would print the same warning again.
    plugin._warn_if_xdist_active(_fake_config(numprocesses=4, is_worker=True))
    assert capsys.readouterr().out == ""
