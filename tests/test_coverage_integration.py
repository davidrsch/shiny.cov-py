"""Locks in the coverage.py mechanics `plugin.py` depends on, as real
regression tests rather than one-off scripts.

Two separate things are verified here, since they're both load-bearing and
neither is obvious from coverage.py's docs:

1. Mutating a live `Coverage` instance's `CoverageData` via
   `add_lines()` is reflected in a subsequent `report()` call on the same
   instance, with no explicit save/reload round-trip.
2. `add_lines()` can register an entirely new file that was never part of
   the original measured/traced set (the synthetic per-UI-element file
   `plugin.py` generates) -- but only if `report()`'s file selection isn't
   filtered out by a restrictive `[report]`/`[run] include=`, which is why
   `plugin.py` patches `cov.config.report_include` before calling
   `report()`.
"""
from __future__ import annotations

import coverage
import coverage.report_core as report_core
import pytest

from shinycov.plugin import _extend_report_filters, _write_ui_elements_file


@pytest.fixture()
def sample_module(tmp_path):
    # `def foo/bar/baz` lines are themselves statements that execute (and
    # so count as "covered") the moment the module is loaded, regardless of
    # whether the function is ever called -- only each function's *body*
    # line depends on an actual call. 6 statements total; only foo() is
    # called, so baseline is 4/6 covered (both `def` lines plus foo's own
    # body) = 66.67%, with baz() left deliberately untouched by
    # add_lines() so the "treated" percentage below isn't just a ceiling
    # of 100%.
    path = tmp_path / "sample.py"
    path.write_text(
        "def foo():\n"
        "    return 1\n"
        "\n"
        "def bar():\n"
        "    return 2\n"
        "\n"
        "def baz():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    return path


def _run_and_measure(sample_module):
    cov = coverage.Coverage(include=[str(sample_module)])
    cov.start()
    ns: dict = {}
    exec(compile(sample_module.read_text(), str(sample_module), "exec"), ns)
    ns["foo"]()  # bar()/baz() are deliberately never called
    cov.stop()
    cov.save()
    return cov


def test_add_lines_is_reflected_without_reload(sample_module):
    cov = _run_and_measure(sample_module)

    baseline = cov.report()
    assert baseline == pytest.approx(66.67, abs=0.01)

    # No cov.load()/new Coverage() instance -- mutate the same live object.
    cov.get_data().add_lines({str(sample_module): [5]})  # bar()'s "return 2"

    treated = cov.report()
    assert treated > baseline
    assert treated == pytest.approx(83.33, abs=0.01)


def test_synthetic_file_is_dropped_by_a_restrictive_include_unless_patched(tmp_path, sample_module):
    synthetic = tmp_path / "ui_elements.py"
    synthetic.write_text("a = True\nb = False\n", encoding="utf-8")

    cov = _run_and_measure(sample_module)
    cov.get_data().add_lines({str(synthetic): [1]})

    def synthetic_file_is_reported():
        names = {
            fr.filename
            for fr, _analysis in report_core.get_analysis_to_report(cov, None)
        }
        return any(name.endswith("ui_elements.py") for name in names)

    # Regression guard for the actual mechanism: report_include here is
    # restrictive (mirrors a consumer's [tool.coverage.run] include=/source=),
    # and unless something extends it, coverage.py silently drops the
    # synthetic file from the report entirely, understating the blended
    # percentage rather than raising an error.
    assert not synthetic_file_is_reported()

    _extend_report_filters(cov, str(synthetic))
    assert synthetic_file_is_reported()


def test_synthetic_file_included_in_report_when_include_is_unrestricted(tmp_path):
    real = tmp_path / "real_app.py"
    real.write_text("def foo():\n    return 1\n", encoding="utf-8")
    synthetic = tmp_path / "ui_elements.py"

    cov = coverage.Coverage(data_file=str(tmp_path / ".coverage"))
    cov.start()
    ns: dict = {}
    exec(compile(real.read_text(), str(real), "exec"), ns)
    ns["foo"]()
    cov.stop()
    cov.save()

    manifest = {
        "inputs": [
            {"id": "alpha", "type": "shiny.actionButtonInput", "label": ""},
            {"id": "beta", "type": "shiny.numericInput", "label": ""},
        ],
        "outputs": [],
        "tabs": [], "conditional": [], "modules": [],
    }
    element_ids = _write_ui_elements_file(synthetic, manifest, hit_ids={"alpha"})
    assert element_ids == ["alpha", "beta"]

    cov.get_data().add_lines({str(synthetic): [4]})  # "alpha" is the first element line

    percent = cov.report()
    # real_app.py: 2 statements, both covered. ui_elements.py: 2 statements
    # (one per element), 1 covered ("alpha"). Blended: 3/4 = 75%.
    assert percent == pytest.approx(75.0)
