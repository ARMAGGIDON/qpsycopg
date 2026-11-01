"""Enable native adaptation classes without enabling native pq generators."""

from __future__ import annotations

from types import ModuleType


def enableNativeAdapters() -> ModuleType | None:
    """Use native adapters while leaving qt-process on the Python generator layer."""
    from . import _adapters_map

    try:
        import psycopg_c._psycopg as native
    except ImportError:
        try:
            import psycopg_binary._psycopg as native
        except ImportError:
            return None

    _adapters_map._psycopg = native
    return native
