"""T-28: nightly zip of data/, newest BACKUP_KEEP kept."""
import zipfile
from datetime import datetime


def test_backup_zips_data_once_a_day_and_keeps_newest(tmp_path, monkeypatch):
    import agent.backup as b
    data, dest = tmp_path / "data", tmp_path / "backups"
    (data / "models").mkdir(parents=True)
    (data / "models" / "big.onnx").write_text("x")
    (data / "lessons.json").write_text("[]")
    (data / "skills" / "a").mkdir(parents=True)
    (data / "skills" / "a" / "SKILL.md").write_text("steps")
    monkeypatch.setattr(b, "DATA", data)
    monkeypatch.setattr(b, "KEEP", 3)
    monkeypatch.setattr(b, "_facts_json", lambda: '[{"text": "likes tea"}]')

    out = b.make(datetime(2026, 11, 19, 2, 5), dest)
    names = set(zipfile.ZipFile(out).namelist())
    assert names == {"data/lessons.json", "data/skills/a/SKILL.md", "mongo_facts.json"}
    assert b.make(datetime(2026, 11, 19, 23, 0), dest) is None       # once a day

    for d in (20, 21, 22):
        b.make(datetime(2026, 11, d, 2, 0), dest)
    assert sorted(p.name for p in dest.iterdir()) == [
        "enzo-data-2026-11-20.zip", "enzo-data-2026-11-21.zip", "enzo-data-2026-11-22.zip"]


def test_backup_dir_inside_data_is_not_zipped_into_itself(tmp_path, monkeypatch):
    import agent.backup as b
    (tmp_path / "x.json").write_text("{}")
    monkeypatch.setattr(b, "DATA", tmp_path)
    monkeypatch.setattr(b, "_facts_json", lambda: "[]")
    dest = tmp_path / "bk"
    b.make(datetime(2026, 11, 18, 3, 0), dest)
    out = b.make(datetime(2026, 11, 19, 3, 0), dest)
    assert set(zipfile.ZipFile(out).namelist()) == {"data/x.json", "mongo_facts.json"}


def test_facts_json_drops_embeddings(monkeypatch):
    import json
    import agent.backup as b
    import agent.facts as f
    monkeypatch.setattr(f, "_all", lambda: [{"text": "t", "emb": [0.1], "_vec": object()}])
    assert json.loads(b._facts_json()) == [{"text": "t"}]
