"""Module entrypoint supporting `python -m worktrack.cli`."""

from __future__ import annotations

from worktrack.cli.parser import main

if __name__ == "__main__":
    raise SystemExit(main())
