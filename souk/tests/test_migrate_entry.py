import importlib

import souk
import souk.migrate


def test_souk_dot_migrate_is_the_documented_callable():
    assert souk.migrate is importlib.import_module("souk.migrate").migrate


def test_importing_the_submodule_does_not_shadow_the_function_back():
    importlib.import_module("souk")
    import souk.migrate  # noqa: F811

    assert callable(souk.migrate)
