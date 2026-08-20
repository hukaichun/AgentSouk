"""`funduq.migrate` is deliberately both the submodule and, after this import,
the function bound over it — `funduq.migrate()` is the documented entry, and
`python -m funduq.migrate` keeps resolving to the module."""

from funduq.migrate import migrate

__all__ = ["migrate"]
