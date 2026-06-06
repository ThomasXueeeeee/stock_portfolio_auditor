# SPDX-License-Identifier: MIT
"""Command-line entry point for stock_portfolio_auditor."""

from __future__ import annotations

from pathlib import Path

import typer

from stock_portfolio_auditor import __version__

app = typer.Typer(
    name="spa",
    help="Parse broker statements and build local portfolio audit reports.",
    no_args_is_help=True,
)


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="Show package version and exit."),
) -> None:
    """Stock Portfolio Auditor CLI."""
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def gui() -> None:
    """Print the Streamlit command used to launch the GUI."""
    typer.echo("streamlit run -m stock_portfolio_auditor.gui_streamlit")


cache_app = typer.Typer(help="Inspect and clear local caches.")
app.add_typer(cache_app, name="cache")


@cache_app.command("list")
def cache_list(cache_dir: Path = Path("data_cache")) -> None:
    """List cache location. Detailed cache management is implemented in later tasks."""
    typer.echo(f"Cache directory: {cache_dir.resolve()}")


@cache_app.command("clear")
def cache_clear(cache_dir: Path = Path("data_cache"), yes: bool = False) -> None:
    """Placeholder cache clear command."""
    if not yes:
        typer.echo("Pass --yes to clear caches once cache management is implemented.")
        raise typer.Exit(code=1)
    typer.echo(f"Cache clearing will target: {cache_dir.resolve()}")
