from pathlib import Path

import mcp_servers


def test_package_imports_and_version() -> None:
    assert isinstance(mcp_servers.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(mcp_servers.__file__).parent / "py.typed").exists()
