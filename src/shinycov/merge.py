"""Python port of the R shiny.cov package's manifest-merge algorithm.

Straight port of `merge_manifest_snapshots()` in
`shiny.cov-r/R/utils.R` -- see that function's docstring for the full
rationale (some widget libraries, e.g. shinyWidgets::pickerInput(), mutate
their own element's class list after interaction, so a snapshot taken only
after interactions can lose a more-specific binding type that a load-time
snapshot still had). This is an independent implementation, not a shared
library with the R side, but `tests/test_merge.py` asserts it against the
same JSON fixture the R and JS/Cypress ports already use, proving all
three agree on the same `expected` outputs.
"""
from __future__ import annotations

from typing import Any

Element = dict[str, Any]
Manifest = dict[str, Any]

_EMPTY_MANIFEST: Manifest = {
    "inputs": [],
    "outputs": [],
    "tabs": [],
    "conditional": [],
    "modules": [],
}


def _is_more_specific(candidate_type: str | None, current_type: str | None) -> bool:
    current_is_base = not current_type or current_type.startswith("shiny.")
    candidate_is_base = not candidate_type or candidate_type.startswith("shiny.")
    return current_is_base and not candidate_is_base


def _as_list(value: Any) -> list[Any]:
    # Manifest snapshots come from page.evaluate() JS output, not
    # internally-constructed data, so a field that's supposed to be an
    # array can arrive as any JSON shape. Coercing a wrong-shaped value to
    # an empty list rather than iterating it as-is (a string would iterate
    # character-by-character, a dict key-by-key) keeps every downstream
    # loop over "the elements" operating on elements, not stray shrapnel.
    return value if isinstance(value, list) else []


def _element_id(el: Any) -> str | None:
    # A well-formed element is a dict with a non-empty string id; anything
    # else (a bare string, a number, an id that isn't a plain string) can't
    # be merged in a meaningful way. Returning None here is how a
    # malformed element degrades to "skip it" rather than raising out of
    # the merge -- the same class of externally-sourced-input hardening
    # R's merge_ui_coverage() applies to interaction log entries.
    if not isinstance(el, dict):
        return None
    el_id = el.get("id")
    return el_id if isinstance(el_id, str) and el_id else None


def _string_entries(values: list[Any]) -> list[str]:
    # tabs/conditional/modules are meant to be plain strings, but like
    # every other manifest field they come from page.evaluate() JS
    # output -- an entry that isn't a non-empty string can't be a dict
    # key for dict.fromkeys() below, so drop it the same way a malformed
    # input/output element gets dropped rather than raising.
    return [v for v in _as_list(values) if isinstance(v, str) and v]


def _union_dedupe(old: list[str], new: list[str]) -> list[str]:
    # dict.fromkeys() preserves first-occurrence order, matching R's
    # union(old, new) semantics (old's elements first, then new's that
    # weren't already present).
    return list(dict.fromkeys([*_string_entries(old), *_string_entries(new)]))


def _merge_elements(old_els: list[Element], new_els: list[Element]) -> list[Element]:
    by_id: dict[str, Element] = {}
    for el in _as_list(old_els):
        el_id = _element_id(el)
        if el_id:
            by_id[el_id] = dict(el)

    for el in _as_list(new_els):
        el_id = _element_id(el)
        if not el_id:
            continue
        existing = by_id.get(el_id)
        if existing is None:
            by_id[el_id] = dict(el)
            continue

        merged = dict(existing)
        if _is_more_specific(el.get("type"), existing.get("type")):
            merged["type"] = el.get("type")
        if not merged.get("label") and el.get("label"):
            merged["label"] = el.get("label")
        by_id[el_id] = merged

    return list(by_id.values())


def merge_manifest_snapshots(old: Manifest | None, new: Manifest) -> Manifest:
    """Merge two UI manifest snapshots taken at different points in a test.

    `old=None` (or an empty manifest) merges `new` against an empty
    manifest rather than passing it through untouched -- `new` is
    page.evaluate() JS output, so even a first snapshot needs the same
    per-element validation an ordinary merge gets, otherwise a malformed
    element could sit in the accumulated manifest (and reach a later
    consumer like the generated ui_elements.py writer, which also assumes
    well-formed elements) until the next call happened to merge it away.
    """
    old = old or _EMPTY_MANIFEST

    return {
        "inputs": _merge_elements(old.get("inputs", []), new.get("inputs", [])),
        "outputs": _merge_elements(old.get("outputs", []), new.get("outputs", [])),
        "tabs": _union_dedupe(old.get("tabs", []), new.get("tabs", [])),
        "conditional": _union_dedupe(old.get("conditional", []), new.get("conditional", [])),
        "modules": _union_dedupe(old.get("modules", []), new.get("modules", [])),
    }
