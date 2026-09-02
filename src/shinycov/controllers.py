"""Transparent instrumentation of py-shiny's Playwright controller classes.

py-shiny ships `shiny.playwright.controller` -- `InputSlider`,
`InputActionButton`, `OutputText`, and friends -- as the documented way
test authors drive and verify a live app. Wrapping every public method on
every controller class means interaction logging happens automatically for
any test written against those controllers, with no parallel
`AppDriver`-style class hierarchy for test authors to opt into (unlike the
R package, which has to subclass shinytest2's `AppDriver` because R has no
equivalent of Python's open class objects).

This module deliberately avoids two approaches that look simpler but don't
reach the real target set, since `shiny.playwright.controller`'s own module
docstring flags its `Protocol`-based mixin design as deliberate:

- `UiBase.__init_subclass__` only fires for classes defined *after* it's
  patched. py-shiny's own controller classes are already fully defined by
  the time this module's `instrument()` runs (it runs at plugin load,
  necessarily after `import shiny.playwright.controller` has completed),
  so hooking `__init_subclass__` would silently instrument nothing real.
  A load-time pass over already-defined classes is the only approach that
  reaches them.
- Iterating only `vars(shiny.playwright.controller)` misses classes that
  are real controller base classes but not re-exported from that module's
  public namespace -- py-shiny keeps shared interaction methods like
  `.click()` and `.expect_label()` on private mixin/base classes
  (`InputActionBase`, `UiWithLabel`, etc.) that only appear by walking each
  exported class's `__mro__`, not in `vars(controller)` directly.
- Input and output controllers do **not** share a common base class --
  `InputSlider` etc. and `OutputText` etc. descend from two separate,
  unrelated hierarchies, so filtering the module scan by "is a subclass
  of [the input base]" would silently skip every output controller and
  everything output-verification methods (`expect_value()`,
  `expect_text()`, ...) live on. (py-shiny's internal module layout for
  these two hierarchies has changed between versions -- naming the
  current base classes/module paths here would go stale against the
  version floor this package supports, so the scan below is written to
  not assume a specific name or path at all.) Every exported name in
  `vars(controller)` is a controller class with nothing else mixed in
  (no stray functions/constants), so the scan walks the MRO of every
  exported class, whichever hierarchy it belongs to, rather than
  filtering by a base class up front.
"""
from __future__ import annotations

import inspect
import time
from typing import Any, Callable

import shiny.playwright.controller as controller

from . import merge
from .discover import discover_manifest

_WRAPPED_MARKER = "_shinycov_wrapped"

_interactions: list[dict[str, Any]] = []
_manifest: dict[str, Any] | None = None


def _all_controller_classes() -> set[type]:
    """Every class reachable via the MRO of any exported controller class.

    Reaching into the MRO (rather than stopping at `vars(controller)`) is
    what picks up private mixins/bases like `InputActionBase.click` or
    `_OutputTextValue.expect_value` -- see the module docstring.
    """
    classes: set[type] = set()
    for _name, obj in vars(controller).items():
        if not inspect.isclass(obj):
            continue
        for klass in obj.__mro__:
            if klass is not object:
                classes.add(klass)
    return classes


def _first_value(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return args[0]
    if kwargs:
        return next(iter(kwargs.values()))
    return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _record_interaction(instance: Any, action: str, args: tuple[Any, ...],
                         kwargs: dict[str, Any]) -> None:
    element_id = getattr(instance, "id", None)
    selector = f"#{element_id}" if element_id else type(instance).__name__
    _interactions.append({
        "selector": selector,
        "action": action,
        "value": _json_safe(_first_value(args, kwargs)),
        "timestamp": time.time(),
    })


def _record_manifest_snapshot(instance: Any) -> None:
    # A real interaction re-discovers the live UI manifest from the same
    # page the interaction happened on, and merges it into the running
    # manifest via the same order-independent merge
    # `merge_manifest_snapshots()` uses on the R side -- the scenario that
    # merge function is designed for (a widget's DOM can look different
    # after interaction than it did at load time). A page that isn't a
    # real, live Playwright page (e.g. a mock in unit tests, or a page
    # that's already closed) must never break the test itself, so any
    # failure here is swallowed.
    global _manifest
    page = getattr(instance, "page", None)
    if page is None:
        return
    try:
        snapshot = discover_manifest(page)
        if not isinstance(snapshot, dict):
            return
        _manifest = merge.merge_manifest_snapshots(_manifest, snapshot)
    except Exception:
        return


def _make_wrapper(name: str, orig: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Recorded in `finally`, not after a successful return: assertion
        # methods (expect_label(), expect_value(), ...) are a large share
        # of real usage and legitimately raise when the assertion fails --
        # that's still a real interaction with the element, and should
        # still count as "verified" for coverage purposes even though the
        # test itself may go on to fail.
        try:
            return orig(self, *args, **kwargs)
        finally:
            _record_interaction(self, name, args, kwargs)
            _record_manifest_snapshot(self)

    setattr(wrapper, _WRAPPED_MARKER, True)
    wrapper.__name__ = getattr(orig, "__name__", name)
    wrapper.__wrapped__ = orig
    return wrapper


def instrument() -> int:
    """Wrap every public method on every controller class, once.

    Safe to call more than once (e.g. if the plugin is loaded into more
    than one pytest session in the same process) -- already-wrapped
    methods are skipped via `_WRAPPED_MARKER`, so re-running never
    double-wraps.

    Returns the number of methods newly wrapped by this call.
    """
    wrapped_count = 0
    for klass in _all_controller_classes():
        for attr_name, attr_val in list(vars(klass).items()):
            if attr_name.startswith("_"):
                continue
            if isinstance(attr_val, property):
                continue
            if not inspect.isfunction(attr_val):
                continue
            if getattr(attr_val, _WRAPPED_MARKER, False):
                continue
            setattr(klass, attr_name, _make_wrapper(attr_name, attr_val))
            wrapped_count += 1
    return wrapped_count


def get_interactions() -> list[dict[str, Any]]:
    return list(_interactions)


def get_manifest() -> dict[str, Any] | None:
    return _manifest


def reset() -> None:
    """Clear recorded interactions/manifest. Test isolation only."""
    global _manifest
    _interactions.clear()
    _manifest = None
