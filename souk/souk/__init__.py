"""`souk.migrate` is deliberately both the submodule and, after this import,
the function bound over it — `souk.migrate()` is the documented entry, and
`python -m souk.migrate` keeps resolving to the module."""

from souk.migrate import migrate

__all__ = ["migrate"]
