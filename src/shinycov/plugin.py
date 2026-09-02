"""shinycov's core hook.

Blends py-shiny UI-interaction coverage into coverage.py's own line
coverage, so `pytest --> coverage.py's report()` gives one number (server
logic + browser-verified UI elements combined), the same "one number, not
two" design as the R shiny.cov package's `collect()`.

Design decision on `[tool.coverage.run] patch = ["subprocess"]`
-----------------------------------------------------------------
`coverage.Coverage()`'s constructor has no `patch=` parameter -- subprocess
measurement can only be turned on via a config file (`pyproject.toml`,
`.coveragerc`, `setup.cfg`, or `tox.ini`), because it has to be visible to
the py-shiny app subprocess before this plugin's Python code ever runs.
This plugin therefore documents `[tool.coverage.run] patch = ["subprocess"]`,
`branch = true`, and `sigterm = true` as required consumer config (see
README) rather than trying to inject it -- there's no API that would let
it. `sigterm = true` specifically: the app subprocess is torn down via
`proc.terminate()` (`SIGTERM`), and coverage.py's usual atexit-based save
never runs for a process killed by an uncaught terminating signal, so
without it the app's own server-side coverage silently never reaches disk
at all -- even though uvicorn's own shutdown log lines look graceful, the
process still dies by the raw signal underneath that.

What the plugin *does* do programmatically: if nothing has already started
coverage measurement (i.e. the consumer runs plain `pytest`, not
`coverage run -m pytest`), it starts its own `Coverage()` instance at
session start. `Coverage(config_file=True)` (the default) auto-discovers
`pyproject.toml` on its own, so the consumer's `[tool.coverage.run]` block
takes effect either way -- they never need to know or care whether they're
invoking `pytest` directly or via `coverage run`.
"""
from __future__ import annotations

import os
import pathlib
import re
import time
from typing import Any

import coverage
import pytest

from . import controllers, modules

OUTPUT_DIRNAME = ".shiny.cov"
UI_ELEMENTS_FILENAME = "ui_elements.py"
_HEADER_LINES = 3

_owns_coverage = False


def _resolve_source() -> str:
    """The coverage source tag for this run (SHINYCOV_SOURCE, default pytest).

    Mirrors the R package, where each adapter sets SHINYCOV_SOURCE so
    `collect()` can keep per-source counts. The pytest plugin is the
    "pytest" adapter, so it defaults there; a consumer can override with
    `SHINYCOV_SOURCE=...` in the environment.
    """
    return os.environ.get("SHINYCOV_SOURCE", "pytest")


def _tag_coverage_source() -> None:
    """Point coverage.py's data file at a source-tagged name.

    `COVERAGE_FILE` is the one knob coverage.py reads in both this process
    and (via environment inheritance) the py-shiny app subprocess, so
    setting it here keeps the in-process and subprocess data in
    `.coverage.<source>.*` rather than a bare `.coverage`, letting
    `source_counts()` attribute each test framework's coverage separately.

    Deliberately skipped when an external `coverage run -m pytest` is
    already active: its Coverage() was constructed before this hook ran, so
    overriding COVERAGE_FILE here would send the app subprocess's data to a
    different file than the one that external Coverage is combining. In that
    case the caller sets SHINYCOV_SOURCE/COVERAGE_FILE in the shell first.
    """
    source = _resolve_source()
    if not source or coverage.Coverage.current() is not None:
        return
    os.environ.setdefault("COVERAGE_FILE", f".coverage.{source}")
    # Parallel data files have unique pid/random suffixes and would
    # otherwise accumulate across runs, so remove any from a previous run
    # before this one starts writing (matches R's overwrite-by-fixed-name).
    for stale in pathlib.Path.cwd().glob(f".coverage.{source}*"):
        if stale.name.endswith("-journal"):
            continue
        try:
            stale.unlink()
        except OSError:
            pass


def _warn_if_xdist_active(config: pytest.Config) -> None:
    # `_manifest`/`_interactions`/`_owns_coverage` are plain module-level
    # globals, per-process -- under pytest-xdist each worker would
    # independently start its own Coverage(), independently combine, and
    # independently overwrite the same output file with only its own share
    # of the suite. `pytest_configure` runs once in the controller process,
    # where xdist sets `config.option.numprocesses` as soon as `-n`/`-n auto`
    # is passed (before any worker exists yet) -- that's the earliest point
    # parallel execution is knowable, so the warning is checked for and
    # printed from the controller only, not once per worker.
    is_worker = hasattr(config, "workerinput")
    numprocesses = getattr(config.option, "numprocesses", None)
    if numprocesses and not is_worker:
        print(
            "shiny.cov: pytest-xdist parallel workers requested (-n "
            f"{numprocesses}) -- this plugin does not support them yet. "
            "Coverage/manifest state is per-process, so each worker would "
            "independently overwrite the same output file and report only "
            "its own share of the suite. Run without -n for accurate "
            "blended coverage."
        )


def pytest_configure(config: pytest.Config) -> None:
    controllers.instrument()
    _warn_if_xdist_active(config)
    _tag_coverage_source()
    modules.install(_output_dir(config))


def pytest_sessionstart(session: pytest.Session) -> None:
    global _owns_coverage
    if coverage.Coverage.current() is None:
        cov = coverage.Coverage()
        cov.start()
        _owns_coverage = True


def _output_dir(config: pytest.Config) -> pathlib.Path:
    out = pathlib.Path(config.rootpath) / OUTPUT_DIRNAME
    out.mkdir(parents=True, exist_ok=True)
    return out


def _safe_identifier(element_id: str, seen: set[str]) -> str:
    # UI element ids can contain characters that aren't valid in a bare
    # Python name (e.g. "-", or the module-qualified ids Shiny modules
    # produce like "mod1-slider") and can collide once sanitized, so this
    # both sanitizes and de-duplicates against everything written so far
    # in the same file.
    ident = re.sub(r"\W", "_", element_id) or "_"
    if ident[0].isdigit():
        ident = f"_{ident}"
    candidate = ident
    n = 2
    while candidate in seen:
        candidate = f"{ident}_{n}"
        n += 1
    seen.add(candidate)
    return candidate


def _hit_ids(interactions: list[dict[str, Any]]) -> set[str]:
    """ids that were the target of at least one logged controller call.

    Unlike the R/Cypress adapters, py-shiny's controller classes don't
    share one fixed action-verb vocabulary split into
    input-vs-output-actions (see `shinycov_input_actions()` in
    `shiny.cov-r/R/utils.R`) -- `InputSlider.set()`, `OutputText.expect_value()`,
    `InputActionButton.click()`, etc. each have their own verb. Any logged
    call at all counts as "interacted with" here, which is coarser than
    the R side's allowlist but avoids hardcoding py-shiny's much larger,
    still-growing method vocabulary.
    """
    hits: set[str] = set()
    for entry in interactions:
        selector = entry.get("selector") or ""
        if selector.startswith("#"):
            hits.add(selector[1:])
    return hits


def _write_ui_elements_file(
    path: pathlib.Path, manifest: dict[str, Any] | None, hit_ids: set[str]
) -> list[str]:
    """Write one Python statement per discovered UI element; return the ids in file order.

    coverage.py's `CoverageData.add_lines()` is purely additive -- there's
    no operation to mark an already-covered line back to uncovered. That
    rules out reusing the widget's own definition line in app.py the way
    the R side's `merge_ui_coverage()` does (see that function's
    docstring): the UI-building code in app.py runs unconditionally at
    startup, so that line is always already covered regardless of whether
    the widget was ever interacted with, and there's no covr-style
    "min-reduce distinct synthetic entries sharing a line" mechanism in
    coverage.py's line-based data model to fix that up after the fact --
    `add_lines()` can only raise a line's status, never lower it. A small
    generated file with one statement per element, entirely separate from
    app.py, gives each element a line that starts uncovered and only
    becomes covered if this session's `add_lines()` call says so.
    """
    elements: list[dict[str, Any]] = []
    if manifest:
        elements.extend(manifest.get("inputs", []))
        elements.extend(manifest.get("outputs", []))

    seen_ids: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for el in sorted(elements, key=lambda e: e.get("id") or ""):
        el_id = el.get("id")
        if not el_id or el_id in seen_ids:
            continue
        seen_ids.add(el_id)
        ordered.append(el)

    lines = [
        "# GENERATED by shiny.cov -- one statement per discovered UI element.\n",
        "# Not imported; exists only so coverage.py has a real, parseable file to\n",
        "# attribute per-element interaction coverage to. Safe to delete between runs.\n",
    ]
    used_names: set[str] = set()
    ids_in_order: list[str] = []
    for el in ordered:
        el_id = el["id"]
        name = _safe_identifier(el_id, used_names)
        tested = el_id in hit_ids
        lines.append(
            f"{name} = {tested!r}  "
            f"# id={el_id!r} type={el.get('type', '')!r} module={el.get('module', '')!r}\n"
        )
        ids_in_order.append(el_id)

    path.write_text("".join(lines), encoding="utf-8")
    return ids_in_order


def _combine_with_retry(cov: coverage.Coverage, attempts: int = 5, delay: float = 0.3) -> None:
    """Merge parallel `.coverage.*` files, tolerating the app subprocess's
    shutdown race.

    py-shiny's `ShinyAppProc.close()` (what test fixtures like
    `create_app_fixture()` use for teardown) calls `proc.terminate()` and
    returns immediately, without waiting for the subprocess to actually
    exit. That subprocess only writes its coverage data from a `SIGTERM`
    handler (see the `sigterm = true` requirement in README.md) as it shuts
    down -- so at the moment `pytest_sessionfinish` runs, the app process
    may still be mid-shutdown and its data file may not exist on disk yet.
    Without a retry, `combine()` silently finds nothing and the app's own
    server-side coverage is dropped from the report, not raised as an
    error. A short bounded retry closes this race for realistic local
    shutdown latencies without introducing an unbounded wait.
    """
    # `combine()`'s default `strict=False` never raises for "zero parallel
    # files found" -- it treats that as a normal no-op, which is correct for
    # a run with no subprocess at all but means the retry loop below would
    # otherwise have no way to tell "found nothing on this attempt, worth
    # trying again" apart from "found nothing and never will." `strict=True`
    # makes that distinction observable via `CoverageException` without
    # changing what a successful combine does.
    found_any = False
    for attempt in range(attempts):
        try:
            # keep=True so the per-source data files survive for source_counts()/
            # source_coverage()/render_report_html()/to_cobertura() to re-read
            # after collect() has already reported the blended number.
            cov.combine(strict=True, keep=True)
            found_any = True
            break
        except coverage.misc.CoverageException:
            pass  # No parallel `.coverage.*` files yet -- may still show up on a later attempt.
        if attempt < attempts - 1:
            time.sleep(delay)

    if not found_any:
        # Genuinely no `.coverage.*` files ever appeared after every retry,
        # not just "found some on a later attempt" -- the exact symptom a
        # missing `sigterm = true` produces (the app subprocess's coverage
        # data never reaches disk), though it's also what a run with no
        # subprocess coverage at all looks like. Can't tell those apart from
        # here, so name the likely cause rather than silently reporting
        # server-only coverage as if nothing were missing.
        print(
            "shiny.cov: no parallel coverage data files found after "
            f"{attempts} attempts -- if you expected subprocess coverage "
            "from the app under test, check that `sigterm = true` is set "
            "in [tool.coverage.run] (see README.md)."
        )


def _extend_report_filters(cov: coverage.Coverage, ui_file: str) -> None:
    # A restrictive [report]/[run] include= would otherwise silently drop
    # the generated file from the report the same way it would drop any
    # file outside the configured source tree -- extend it rather than
    # relying on the consumer to know to special-case a generated file
    # they don't control the path of. An unset (falsy) include is already
    # unrestricted, so it's left alone.
    if cov.config.report_include:
        cov.config.report_include = list(cov.config.report_include) + [ui_file]


def _blend_ui_coverage(
    cov: coverage.Coverage,
    manifest: dict[str, Any] | None,
    interactions: list[dict[str, Any]],
    out_dir: pathlib.Path,
) -> tuple[list[str], set[str], pathlib.Path]:
    """Merge UI element coverage into `cov`; return (element_ids, hit_ids, ui_file).

    The returned tuple is what `finalize()` and `to_cobertura()` both need:
    which elements exist (in generated-file order), which were interacted
    with, and the generated file those synthetic lines live in.
    """
    hit_ids = _hit_ids(interactions)

    ui_file = out_dir / UI_ELEMENTS_FILENAME
    element_ids = _write_ui_elements_file(ui_file, manifest, hit_ids)

    if element_ids:
        hit_lines = [
            i + _HEADER_LINES + 1
            for i, el_id in enumerate(element_ids)
            if el_id in hit_ids
        ]
        if hit_lines:
            data = cov.get_data()
            if data.has_arcs():
                # With `branch = true` (the documented required consumer
                # config), CoverageData stores arcs, not plain lines --
                # add_lines() and add_arcs() are mutually exclusive against
                # the same underlying data (mixing them raises `DataError:
                # Can't add line measurements to existing branch data`). A
                # self-loop arc (line, line) is coverage.py's own
                # convention for "this line executed, no real branch
                # information" -- lines() derives its result from the set
                # of positive line numbers appearing in any arc, so a
                # self-loop is sufficient to mark the line covered without
                # asserting a branch that doesn't exist.
                data.add_arcs({str(ui_file): [(line, line) for line in hit_lines]})
            else:
                data.add_lines({str(ui_file): hit_lines})
        else:
            # add_lines() with no lines still registers the file as
            # measured (0 hits), so it shows up as 0% instead of being
            # silently absent from the report.
            cov.get_data().touch_file(str(ui_file))
        _extend_report_filters(cov, str(ui_file))

    return element_ids, hit_ids, ui_file


def finalize(
    cov: coverage.Coverage,
    manifest: dict[str, Any] | None,
    interactions: list[dict[str, Any]],
    out_dir: pathlib.Path,
) -> float | None:
    """Blend UI coverage into `cov` and report it.

    Shared by the pytest plugin's `pytest_sessionfinish` hook and the
    standalone `shinycov` collector (used for Cypress-driven runs, where no
    pytest session exists). Returns the blended coverage percentage, or None
    if the report itself failed.
    """
    boundaries = modules.read_boundaries(out_dir)
    if manifest is not None and boundaries:
        manifest = modules.attach_boundaries(manifest, boundaries)

    element_ids, hit_ids, _ = _blend_ui_coverage(cov, manifest, interactions, out_dir)

    try:
        percent = cov.report()
    except coverage.misc.CoverageException as e:
        print(f"shiny.cov: coverage report failed: {e}")
        return None

    # Also emit an HTML report with per-line hit counts (coverage.py's
    # equivalent of covr::report()'s annotated source view), written under
    # .shiny.cov/htmlcov/ so CI can upload it as an artifact alongside the
    # terminal/XML report.
    try:
        cov.html_report(directory=str(out_dir / "htmlcov"))
    except coverage.misc.CoverageException as e:
        print(f"shiny.cov: html report failed: {e}")

    if manifest is None:
        # No UI interaction was ever logged, so UI discovery never ran --
        # distinct from an app that was tested but genuinely has zero UI
        # elements, which would leave `manifest` as a real (if empty) dict
        # instead of None. Both would otherwise print an identical "0/0"
        # line with nothing to tell them apart.
        print(
            "shiny.cov: no UI manifest discovered -- no controller/command "
            "logged a UI interaction, so UI coverage wasn't tracked. If your "
            "tests interact with the app via raw locators instead of the "
            "controller classes, this is expected; otherwise check that your "
            "tests actually exercise the app."
        )

    print(
        f"shiny.cov: blended coverage {percent:.1f}% "
        f"({len(hit_ids)}/{len(element_ids)} UI elements interacted with)"
    )
    return percent


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _owns_coverage
    cov = coverage.Coverage.current()
    if cov is None:
        # Neither this plugin nor a `coverage run` wrapper started
        # measurement -- nothing to blend UI coverage into.
        return

    if _owns_coverage:
        cov.stop()
        cov.save()
        _owns_coverage = False

    _combine_with_retry(cov)

    finalize(
        cov,
        controllers.get_manifest(),
        controllers.get_interactions(),
        _output_dir(session.config),
    )
