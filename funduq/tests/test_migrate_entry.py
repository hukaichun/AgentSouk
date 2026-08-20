import importlib

import funduq
import funduq.migrate


def test_funduq_dot_migrate_is_the_documented_callable():
    assert funduq.migrate is importlib.import_module("funduq.migrate").migrate


def test_importing_the_submodule_does_not_shadow_the_function_back():
    importlib.import_module("funduq")
    import funduq.migrate  # noqa: F811

    assert callable(funduq.migrate)
