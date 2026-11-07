from pathlib import Path

import agent_runtime


def test_package_imports_and_version() -> None:
    assert isinstance(agent_runtime.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(agent_runtime.__file__).parent / "py.typed").exists()
