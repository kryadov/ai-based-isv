from pathlib import Path

import evals


def test_package_imports_and_version() -> None:
    assert isinstance(evals.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(evals.__file__).parent / "py.typed").exists()
