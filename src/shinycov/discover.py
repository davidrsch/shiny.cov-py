"""UI-discovery: run the vendored discover-bindings.js against a live page.

Playwright-Python counterpart to shiny.cov-cypress's `support.js`
(`win.eval(discoverBindingsSource)`) and the R side's
`AppDriver$get_js()` call -- all three run the identical vendored source
against a live Shiny page's `Shiny.inputBindings`/`Shiny.outputBindings`
registry, so discovery logic never needs to be reimplemented per adapter.
"""
from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

from .vendor.discover_bindings_source import DISCOVER_BINDINGS_SOURCE


def discover_manifest(page: Page) -> dict[str, Any]:
    """Discover the live UI manifest from a Playwright `Page`.

    The vendored source is already a self-invoking expression (an IIFE
    ending in `()`) that evaluates to the manifest object, so it's passed
    to `page.evaluate()` directly -- Playwright evaluates a string argument
    that isn't a function declaration as a plain expression, with no
    `() => { ... }` wrapper needed. An extra wrapper would in fact break
    this: the IIFE's return value would become an unused
    statement-completion inside the wrapper body rather than the wrapper's
    own return value.
    """
    return page.evaluate(DISCOVER_BINDINGS_SOURCE)
