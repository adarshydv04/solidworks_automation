import pytest

from solidworks_automation.safety import PolicyError, resolve_write_path


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setenv("SW_MCP_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("SW_MCP_ALLOWED_DIRS", raising=False)
    return tmp_path / "out"


def test_bare_name_goes_to_output_dir(out):
    path = resolve_write_path("bracket.sldprt", allowed_suffixes={".sldprt"}, overwrite=False)
    assert path == (out / "bracket.sldprt").resolve()


def test_outside_allowed_dirs_is_refused(out, tmp_path):
    with pytest.raises(PolicyError, match="not allowed"):
        resolve_write_path(str(tmp_path / "elsewhere" / "a.step"), allowed_suffixes={".step"}, overwrite=False)


def test_parent_traversal_is_refused(out):
    with pytest.raises(PolicyError, match="not allowed"):
        resolve_write_path("../escape.step", allowed_suffixes={".step"}, overwrite=False)


def test_extra_allowed_dir(out, tmp_path, monkeypatch):
    monkeypatch.setenv("SW_MCP_ALLOWED_DIRS", str(tmp_path / "projects"))
    path = resolve_write_path(str(tmp_path / "projects" / "a.step"), allowed_suffixes={".step"}, overwrite=False)
    assert path.parent == (tmp_path / "projects").resolve()


def test_existing_file_needs_overwrite(out):
    out.mkdir(parents=True)
    (out / "a.stl").write_bytes(b"x")
    with pytest.raises(PolicyError, match="already exists"):
        resolve_write_path("a.stl", allowed_suffixes={".stl"}, overwrite=False)
    assert resolve_write_path("a.stl", allowed_suffixes={".stl"}, overwrite=True).name == "a.stl"


def test_wrong_suffix_is_refused(out):
    with pytest.raises(PolicyError, match="Unsupported file type"):
        resolve_write_path("a.exe", allowed_suffixes={".stl"}, overwrite=False)
