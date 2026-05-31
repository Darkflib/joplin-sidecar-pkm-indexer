"""Command-line interface (Typer).

Scaffold stub: only the ``--version`` callback is wired so that the
``pkm-sidecar`` console-script entry point resolves after ``uv sync``. The real
subcommands (serve, index rebuild, index sync, status, doctor, db path) are
implemented by the CLI subsystem in a later step (PRD §15).
"""

from __future__ import annotations

import typer

from pkm_sidecar import __version__

app = typer.Typer(
    name="pkm-sidecar",
    help="Local-first read-only Joplin PKM indexer and dashboard.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pkm-sidecar {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """pkm-sidecar root command."""


@app.command()
def serve() -> None:
    """Start the local FastAPI server (not yet implemented)."""
    raise NotImplementedError("serve is implemented in a later build step (PRD §15.1).")
