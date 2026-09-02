"""Cross-language equivalence: shinycov.merge.merge_manifest_snapshots()
is an independent implementation of the same algorithm as R's
merge_manifest_snapshots() (shiny.cov-r/R/utils.R) and JS's mergeManifests()
(shiny.cov-cypress/src/plugin.js). All three are asserted here against the
same fixture's `expected` value rather than against each other's live
output, so agreement doesn't depend on a runtime cross-language bridge --
see shiny.cov-r/tests/testthat/test-merge-manifest-snapshots.R and
shiny.cov-cypress/test/merge-manifest-equivalence.test.js for the other two.

The fixture is vendored into tests/fixtures/ rather than read from the R
package's copy directly, so these merge-algorithm assertions run for
anyone testing this package on its own, independent of whether a sibling
shiny.cov-r/ checkout happens to be present. When it is present, a separate
test below compares the two copies to catch drift between them.
"""
from __future__ import annotations

import functools
import json
import pathlib

import pytest

from shinycov.merge import merge_manifest_snapshots

FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "manifest-merge-cases.json"

CANONICAL_FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "shiny.cov-r" / "tests" / "testthat" / "fixtures" / "manifest-merge-cases.json"
)


def _normalize(manifest):
    """Sort elements/arrays into a canonical order before comparing.

    Element order within inputs/outputs is not semantically meaningful --
    see the R and JS equivalence tests' identical normalize helpers for why
    (dict/list iteration order differs enough between implementations,
    especially for numeric-looking string ids, that raw order can't be
    asserted on directly).
    """
    def sort_by_id(els):
        return sorted(els or [], key=lambda e: str(e.get("id", "")))

    def sort_strings(arr):
        return sorted(arr or [])

    return {
        "inputs": sort_by_id(manifest.get("inputs")),
        "outputs": sort_by_id(manifest.get("outputs")),
        "tabs": sort_strings(manifest.get("tabs")),
        "conditional": sort_strings(manifest.get("conditional")),
        "modules": sort_strings(manifest.get("modules")),
    }


def _load_fixture_cases():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data["cases"]


@pytest.mark.parametrize("case", _load_fixture_cases(), ids=lambda c: c["name"])
def test_merge_matches_shared_fixture(case):
    actual = merge_manifest_snapshots(case["old"], case["new"])
    assert _normalize(actual) == _normalize(case["expected"]), case["name"]


def test_fixture_file_is_actually_being_read():
    # A parametrize list built from a broken/empty read would silently
    # collect zero test cases and this whole module would "pass" without
    # ever exercising the merge algorithm -- guard against that directly.
    assert len(_load_fixture_cases()) >= 5


@pytest.mark.skipif(
    not CANONICAL_FIXTURE_PATH.exists(),
    reason=(
        "sibling shiny.cov-r/ package not present -- this check only runs "
        "meaningfully when developing with the monorepo layout available; "
        "not required for this package's own CI."
    ),
)
def test_vendored_fixture_matches_the_canonical_r_package_copy():
    vendored = FIXTURE_PATH.read_text(encoding="utf-8")
    canonical = CANONICAL_FIXTURE_PATH.read_text(encoding="utf-8")
    assert vendored == canonical, (
        "tests/fixtures/manifest-merge-cases.json has drifted from "
        "shiny.cov-r's copy -- re-copy it from "
        "shiny.cov-r/tests/testthat/fixtures/manifest-merge-cases.json."
    )


# ---- N-way reduce over a snapshot array, mirroring the R side's
# extended test-merge-manifest-snapshots.R coverage for the same scenario
# (Track A's Playwright adapter writes N per-worker snapshots, not just 2) ----

def _snapshot(inputs=None, outputs=None, tabs=None, conditional=None, modules=None):
    return {
        "inputs": inputs or [],
        "outputs": outputs or [],
        "tabs": tabs or [],
        "conditional": conditional or [],
        "modules": modules or [],
    }


THREE_WORKER_SNAPSHOTS = [
    _snapshot(
        inputs=[{"id": "picker_1", "type": "shiny.selectInput", "label": ""}],
        tabs=["Main"], conditional=["panel_a"],
    ),
    _snapshot(
        inputs=[
            {"id": "picker_1", "type": "shinyWidgets.pickerInput", "label": "Picker 1"},
            {"id": "only_worker_2", "type": "shiny.textInput", "label": "Only worker 2"},
        ],
        tabs=["Settings"], modules=["mod1"],
    ),
    _snapshot(
        inputs=[{"id": "picker_1", "type": "shiny.selectInput", "label": ""}],
        outputs=[{"id": "out", "type": "shiny.textOutput", "label": ""}],
        tabs=["About"], conditional=["panel_b"], modules=["mod1", "mod2"],
    ),
]


def _reduce(snapshots):
    return functools.reduce(merge_manifest_snapshots, snapshots, None)


def test_reduce_over_n_snapshots_matches_manual_pairwise_reduce():
    actual = _reduce(THREE_WORKER_SNAPSHOTS)
    manual = merge_manifest_snapshots(
        merge_manifest_snapshots(THREE_WORKER_SNAPSHOTS[0], THREE_WORKER_SNAPSHOTS[1]),
        THREE_WORKER_SNAPSHOTS[2],
    )
    assert _normalize(actual) == _normalize(manual)

    picker = next(el for el in actual["inputs"] if el["id"] == "picker_1")
    assert picker["type"] == "shinyWidgets.pickerInput"
    assert picker["label"] == "Picker 1"
    assert set(actual["tabs"]) == {"Main", "Settings", "About"}
    assert set(actual["modules"]) == {"mod1", "mod2"}


def test_reduce_is_order_independent_when_specific_type_is_in_middle_snapshot():
    orderings = [
        [0, 1, 2],
        [2, 1, 0],
        [1, 2, 0],
        [2, 0, 1],
    ]
    merged = [
        _normalize(_reduce([THREE_WORKER_SNAPSHOTS[i] for i in order]))
        for order in orderings
    ]
    for m in merged[1:]:
        assert m == merged[0]

    picker = next(el for el in merged[0]["inputs"] if el["id"] == "picker_1")
    assert picker["type"] == "shinyWidgets.pickerInput"
    assert picker["label"] == "Picker 1"


def test_reduce_over_single_snapshot_returns_it_unchanged():
    only = THREE_WORKER_SNAPSHOTS[1]
    assert _reduce([only]) == only


def test_reduce_over_empty_list_returns_none():
    assert _reduce([]) is None


# ---- Malformed elements from page.evaluate() output ----
# discover-bindings.js runs in a real browser and its return value is
# arbitrary JS-turned-JSON, not internally-constructed data -- an element
# that isn't a well-formed dict must be skipped, not raise, since this
# merge runs on every instrumented controller call (see controllers.py's
# _record_manifest_snapshot()).

def test_merge_skips_non_dict_element_instead_of_raising():
    old = {
        "inputs": [{"id": "go", "type": "shiny.actionButtonInput", "label": "Go"}],
        "outputs": [], "tabs": [], "conditional": [], "modules": [],
    }
    bad_snapshot = {
        "inputs": ["not-a-dict"],
        "outputs": [], "tabs": [], "conditional": [], "modules": [],
    }

    merged = merge_manifest_snapshots(old, bad_snapshot)

    # The malformed element contributes nothing, but the valid element
    # already in `old` survives the merge untouched.
    assert merged["inputs"] == [{"id": "go", "type": "shiny.actionButtonInput", "label": "Go"}]


def test_merge_keeps_valid_elements_alongside_malformed_ones_in_same_snapshot():
    old = None
    new = {
        "inputs": [
            "not-a-dict",
            {"id": "go", "type": "shiny.actionButtonInput", "label": "Go"},
            {"id": 123, "type": "shiny.actionButtonInput", "label": "Bad id type"},
            {"type": "shiny.actionButtonInput", "label": "Missing id"},
        ],
        "outputs": [], "tabs": [], "conditional": [], "modules": [],
    }

    merged = merge_manifest_snapshots(old, new)

    assert merged["inputs"] == [{"id": "go", "type": "shiny.actionButtonInput", "label": "Go"}]


def test_merge_tolerates_non_list_array_fields():
    old = {"inputs": [], "outputs": [], "tabs": ["Main"], "conditional": [], "modules": []}
    new = {"inputs": "not-a-list", "outputs": None, "tabs": "Settings", "conditional": [], "modules": []}

    merged = merge_manifest_snapshots(old, new)

    assert merged["inputs"] == []
    assert merged["outputs"] == []
    assert merged["tabs"] == ["Main"]


def test_merge_skips_malformed_elements_inside_tabs_conditional_modules():
    # tabs/conditional/modules feed into dict.fromkeys() for deduping,
    # which requires every entry to be hashable -- a dict or list entry
    # (unlike inputs/outputs elements, these are meant to be plain
    # strings) must be dropped the same way a malformed input/output
    # element is, not raise.
    old = {
        "inputs": [], "outputs": [],
        "tabs": ["Main"], "conditional": ["panel_a"], "modules": ["mod1"],
    }
    new = {
        "inputs": [], "outputs": [],
        "tabs": [{"weird": "dict"}, "Settings", None, ""],
        "conditional": [["nested", "list"], "panel_b"],
        "modules": [123, "mod2"],
    }

    merged = merge_manifest_snapshots(old, new)

    assert merged["tabs"] == ["Main", "Settings"]
    assert merged["conditional"] == ["panel_a", "panel_b"]
    assert merged["modules"] == ["mod1", "mod2"]
