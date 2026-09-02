"""Minimal py-shiny app for shinycov's own end-to-end verification.

One input (never interacted with by the e2e test) and one input that is
interacted with, so the blended coverage percentage can visibly move
between "untested" and "tested" for a known interaction pattern -- the
same before/after assertion shiny.cov-r's own R test suite makes for the
original package.
"""
from shiny import App, render, ui

app_ui = ui.page_fluid(
    ui.input_action_button("go", "Go"),
    ui.input_text("unused", "Never touched by the test", value=""),
    ui.output_text("clicks"),
)


def server(input, output, session):
    @render.text
    def clicks():
        return f"Clicked {input.go()} times"


app = App(app_ui, server)
