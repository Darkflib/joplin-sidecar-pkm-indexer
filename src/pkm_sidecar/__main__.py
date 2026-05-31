"""Allow ``python -m pkm_sidecar`` to invoke the Typer CLI."""

from pkm_sidecar.cli import app

if __name__ == "__main__":
    app()
