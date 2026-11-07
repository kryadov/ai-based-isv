from pathlib import Path

import context_engine


def test_package_imports_and_version() -> None:
    assert isinstance(context_engine.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(context_engine.__file__).parent / "py.typed").exists()
