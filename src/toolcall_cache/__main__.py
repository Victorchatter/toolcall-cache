"""Allow running the package with `python -m toolcall_cache`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
