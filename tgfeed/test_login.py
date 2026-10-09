import importlib.util
import sys

from tgfeed import login


def test_finds_a_module_that_only_lives_in_reds_lib_dir(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "tg_fake_dependency.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert importlib.util.find_spec("tg_fake_dependency") is None
    assert login.ensure_module("tg_fake_dependency", extra_dirs=(str(lib),))
    assert importlib.util.find_spec("tg_fake_dependency") is not None


def test_reports_false_when_it_is_nowhere(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert not login.ensure_module("tg_definitely_missing_module", extra_dirs=(str(tmp_path),))


def test_env_override_dir_is_searched(tmp_path, monkeypatch):
    lib = tmp_path / "custom"
    lib.mkdir()
    (lib / "tg_fake_dependency2.py").write_text("X = 1\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("TG_LIB_DIR", str(lib))
    assert login.ensure_module("tg_fake_dependency2", extra_dirs=())
