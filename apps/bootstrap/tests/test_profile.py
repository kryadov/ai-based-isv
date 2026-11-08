from pathlib import Path

import yaml
from bootstrap.profile import Profile, load, save


def test_profile_defaults() -> None:
    p = Profile()
    assert p.target == "laptop-k3d"
    assert p.tracker == "deploy-openproject"
    assert p.llm == "host-ollama"
    assert p.demo is True


def test_profile_roundtrip(tmp_path: Path) -> None:
    p = Profile(tracker="connect-existing", tracker_existing_url="https://op.lab.local", demo=False)
    path = save(p, tmp_path / "env.local.yaml")
    loaded = load(path)
    assert loaded == p
    raw = path.read_text()
    assert "connect-existing" in raw


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "env.local.yaml"
    save(Profile(), deep)
    assert deep.exists()


def test_yaml_is_human_readable(tmp_path: Path) -> None:
    path = save(Profile(demo=False), tmp_path / "env.local.yaml")
    text = path.read_text()
    assert "demo: false" in text
    assert yaml.safe_load(text)["tracker"] == "deploy-openproject"
