from solidworks_automation.operations import find_template_file


def make(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_prefers_this_release_part_template(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    make(tmp_path / "SOLIDWORKS" / "SOLIDWORKS 2025" / "templates" / "Part.prtdot")
    make(tmp_path / "SOLIDWORKS" / "SOLIDWORKS 2026" / "templates" / "gb_part.prtdot")
    wanted = make(tmp_path / "SOLIDWORKS" / "SOLIDWORKS 2026" / "templates" / "Part.prtdot")
    assert find_template_file(".prtdot", 2026, None) == wanted


def test_falls_back_to_newest_other_release(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    make(tmp_path / "SOLIDWORKS" / "SOLIDWORKS 2024" / "templates" / "Assembly.asmdot")
    newest = make(tmp_path / "SOLIDWORKS" / "SOLIDWORKS 2025" / "templates" / "Assembly.asmdot")
    assert find_template_file(".asmdot", 2026, None) == newest


def test_falls_back_to_install_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path / "empty"))
    tutorial = make(tmp_path / "install" / "lang" / "english" / "Tutorial" / "part.prtdot")
    assert find_template_file(".prtdot", 2026, str(tmp_path / "install")) == tutorial


def test_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    assert find_template_file(".prtdot", 2026, None) is None
