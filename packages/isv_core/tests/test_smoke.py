from pathlib import Path

import isv_core


def test_package_imports_and_version() -> None:
    assert isinstance(isv_core.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(isv_core.__file__).parent / "py.typed").exists()
