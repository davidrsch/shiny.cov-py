# shiny.cov

A pytest plugin that reports real end-to-end coverage for [py-shiny](https://shiny.posit.co/py/)
apps: server-side line coverage (via `coverage.py`) blended with
browser-verified UI coverage (which inputs/outputs were actually discovered
and interacted with during a Playwright-driven test run) into **one
percentage**, rather than two separate numbers. Python counterpart to the
shiny.cov R package: an independent implementation with no runtime
dependency on R or the R package, vendoring a byte-for-byte copy of its
browser-side UI-discovery script and porting its manifest-merge algorithm
to Python.

The importable module is `shinycov` (PyPI distribution name `shiny.cov`);
it doesn't follow the PyPI `pytest-*` naming convention, but it *is* a
pytest plugin: pytest discovers and auto-loads plugins entirely via the
`pytest11` entry-point group, never by matching a package's own name
against a `pytest-*` prefix, so auto-loading works identically regardless
of the package name.

## Install

```bash
pip install shiny.cov
playwright install chromium  # if not already installed
```

The plugin registers itself automatically via pytest's `pytest11` entry
point (`shinycov = shinycov.plugin`) -- no `pytest_plugins = (...)`
needed in your `conftest.py`.

## Required consumer configuration

Add this to your project's `pyproject.toml` (or `.coveragerc`/`setup.cfg`/`tox.ini`):

```toml
[tool.coverage.run]
patch = ["subprocess"]
branch = true
sigterm = true
```

`patch = ["subprocess"]` is what makes coverage.py measure the py-shiny app
subprocess that `shiny.run`/`create_app_fixture` launches, not just the
pytest process itself -- without it, only whatever runs in-process (nothing,
in the typical Playwright-driven-app pattern) gets measured. Requires
`coverage>=7.10.3` (earlier versions had a bug where this setting didn't
fully propagate to the subprocess).

`sigterm = true` is required too, and easy to miss: `create_app_fixture()`
tears the app down with `proc.terminate()` (`SIGTERM`), and coverage.py's
normal atexit-based save never runs for a process killed by an uncaught
terminating signal. Without `sigterm = true`, the app's own server-side
coverage silently never reaches disk, even though uvicorn's own shutdown
log lines look perfectly graceful -- the process still dies by the raw
signal underneath that. `sigterm = true`
makes coverage.py install a `SIGTERM` handler that saves data before the
process dies.

**Why the plugin can't set this for you programmatically**: `coverage.Coverage()`'s
constructor has no `patch=` parameter -- subprocess measurement can only be
turned on via a config file, because that config has to be visible to the
py-shiny app *subprocess* before any of this plugin's Python code has had a
chance to run in the parent process. There's no API path around that.

What the plugin *does* do automatically: if nothing has already started
coverage measurement (i.e. you run plain `pytest`, not `coverage run -m
pytest`), it starts its own `Coverage()` instance for you at session start.
`Coverage()`'s default `config_file=True` auto-discovers `pyproject.toml` on
its own, so your `[tool.coverage.run]` block takes effect either way -- you
never need to choose between invoking `pytest` directly or via `coverage
run`.

## What happens automatically

Once installed, nothing else is required in your test files:

- **UI discovery and interaction logging are transparent.** Every public
  method on py-shiny's `shiny.playwright.controller` classes (`InputSlider`,
  `InputActionButton`, etc.) is instrumented at plugin-load time. Just write
  normal Playwright/py-shiny tests using those controllers -- there's no
  parallel `AppDriver`-style class to subclass or opt into.
- **UI manifest discovery happens as a side effect of interaction**, not on
  a separate schedule: each time an instrumented controller method is
  called, the plugin re-runs the same UI-discovery script the R package uses
  (`shiny.cov-r/inst/js/discover-bindings.js`, vendored byte-for-byte) against
  that call's page, and merges the result into a running manifest using the
  same more-specific-type-wins/label-fill-in algorithm as the R side's
  `merge_manifest_snapshots()`. A test that never calls any controller
  method on a given page won't discover that page's UI at all -- this is a
  known limitation, not an oversight.
- **Tests written against the `.expect` property escape hatch aren't
  logged.** py-shiny's controller classes expose a documented `expect`
  property (`slider.expect.to_be_visible()`) that returns a raw
  `playwright.expect()` object for assertions the named wrapper methods
  (`expect_label()`, `expect_value()`, ...) don't cover. Instrumentation
  deliberately skips properties, only wrapping methods, so `.expect`'s
  return value is never instrumented -- an interaction written against it
  instead of a named wrapper method is invisible to interaction logging.
  This is the same class of limitation as the point above, just triggered
  by *how* a page is interacted with rather than *whether* it is.
- **At session end** (`pytest_sessionfinish`), the plugin:
  1. Combines the parallel `.coverage.*` files `patch = ["subprocess"]`
     produces.
  2. Writes a small generated file, `.shiny.cov/ui_elements.py`, with
     one Python statement per discovered UI element.
  3. Calls `CoverageData.add_lines()` to mark the elements that were
     interacted with as "covered."
  4. Calls `coverage.Coverage.report()` once, over both your real server
     code and the generated UI-elements file, for one blended percentage.

## Cypress (no pytest)

You can drive a py-shiny app with Cypress instead of pytest/Playwright. The
browser-side UI discovery is identical (py-shiny ships the same
`Shiny.inputBindings`/`Shiny.outputBindings` registry as R Shiny), so the
`shiny.cov-cypress` adapter's `plugin.js`/`support.js` work unchanged.

1. Launch the app under coverage measurement (so the app process is measured
   and, with `patch = ["subprocess"]`, any subprocess it spawns too):

   ```bash
   coverage run -m shiny run app.py --port 3333 --host 127.0.0.1 &
   ```

2. Configure Cypress the same way as for R Shiny: add
   `shiny.cov-cypress/plugin` to `setupNodeEvents`, load
   `shiny.cov-cypress/support`, set `env.shinyCovAppDir` to the app dir, and
   log interactions with `cy.shinyCovInteract()`.

3. Run Cypress, then collect and report with the standalone `shinycov` command
   (it combines the `.coverage.*` data with `.shiny.cov/manifest.json` +
   `.shiny.cov/interactions.json` and emits the same blended report the pytest
   plugin produces):

   ```bash
   shinycov .
   # or: python -m shinycov.collect .
   ```

## pytest-xdist is not supported

`shiny.cov` tracks manifest/interaction state in plain module-level
globals, one `coverage.Coverage()` instance per process. Under
`pytest-xdist` (`-n auto`/`-n <N>`), each worker is a separate process: it
would independently combine and report, and each would overwrite the same
`.shiny.cov/ui_elements.py` with only its own share of the suite's
interactions, silently understating the blended percentage. The plugin
prints a warning at `pytest_configure` time when it detects `-n` is in use,
but does not attempt to merge across workers -- run without `-n` for an
accurate blended coverage number.

## Why UI coverage needs its own generated file

The R package's `merge_ui_coverage()` (`shiny.cov-r/R/collect.R`) attributes
each UI element's synthetic coverage entry to the *same line* the widget is
defined on in `app.R`, using a covr-specific trick (a distinct sentinel in
the synthetic entry's srcref) to keep that entry separate from the real,
always-covered entry for that line.

`coverage.py` has no equivalent trick available: `CoverageData.add_lines()`
is purely additive -- there's no way to mark an already-covered line back to
uncovered, and coverage.py has no per-line "multiple entries, reduce by
minimum" mechanism the way covr does. Since a widget's own definition line
in `app.py` is unconditionally executed once at app startup (building the UI
requires running that statement) regardless of whether it was ever
interacted with, reusing that line would make every element show as
"covered" from the moment the app starts, which defeats the entire point.
Attributing each element to its own line in a small generated file, kept
uncovered until `add_lines()` says otherwise, avoids that.

A **restrictive** `[tool.coverage.run]`/`[report] include=` (or `source=`)
would otherwise silently drop this generated file from the report the same
way it would drop any file outside your configured source tree -- the
plugin extends `report_include` itself before calling `report()` so you
don't have to carve out an exception for a file whose path you don't
control.

## Package layout

```
src/shinycov/
  plugin.py        pytest_sessionfinish hook: combine, blend, report
  controllers.py    UiBase instrumentation (interaction logging + manifest discovery)
  merge.py           Python port of merge_manifest_snapshots()
  discover.py         page.evaluate() wrapper around the vendored JS
  vendor/
    discover_bindings_source.py   generated -- do not edit by hand
scripts/
  sync_discover_bindings.py       re-run after editing the canonical JS source
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
pytest
```

`tests/test_vendor_sync.py`, and one drift-check test inside
`tests/test_merge.py`, compare vendored copies against the sibling R
package's source at `../shiny.cov-r/` -- they skip automatically outside a
monorepo checkout that has that sibling present, same as
`shiny.cov-cypress`'s equivalent tests.
