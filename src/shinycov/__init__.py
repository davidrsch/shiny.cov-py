"""shinycov: real end-to-end coverage for py-shiny apps under pytest.

Combines server-side line coverage (via coverage.py's subprocess support)
with browser-verified UI coverage (which inputs/outputs were actually
discovered and interacted with during a Playwright-driven test run) into
one blended percentage, the same "one number, not two" design as the R
shiny.cov package.
"""

# Keep in sync with the `version` field in pyproject.toml.
__version__ = "0.0.0.dev0"

from .collect import (  # noqa: E402
    collect,
    render_report_html,
    source_counts,
    source_coverage,
    to_cobertura,
)

__all__ = [
    "collect",
    "render_report_html",
    "source_counts",
    "source_coverage",
    "to_cobertura",
    "__version__",
]
