from pathlib import Path

import bootstrap


def test_package_imports_and_version() -> None:
    assert isinstance(bootstrap.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(bootstrap.__file__).parent / "py.typed").exists()
