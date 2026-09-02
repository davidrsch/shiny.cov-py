"""Enables pytest's own `pytester` fixture for this test suite.

`pytester` is a built-in pytest plugin, but it's disabled by default (it's
only meant for testing plugins, not general use) -- declaring it here makes
it available to any test in this directory, notably `test_plugin.py`'s
tests that run `shinycov` as a real, entry-point-registered plugin inside
a throwaway pytest invocation, rather than calling its hook functions
directly.
"""
pytest_plugins = ["pytester"]
