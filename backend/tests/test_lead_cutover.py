import json
from pathlib import Path

import pytest


def test_freeze_requires_stopped_file_backend_and_empty_queues(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import lead_cutover

    monkeypatch.setattr(lead_cutover, "ROOT", tmp_path)
    monkeypatch.setattr(lead_cutover.os, "chown", lambda *_: None)
    logs = tmp_path / "backend/logs"
    logs.mkdir(parents=True)
    (logs / lead_cutover.LEADS).write_text('{"company_id":"clinic_a"}\n')
    (logs / lead_cutover.ARCHIVE).write_text('{"company_id":"clinic_b"}\n')
    outbox = logs / lead_cutover.OUTBOX
    outbox.write_text('{"delivery_id":"one","status":"pending"}\n')

    monkeypatch.setattr(lead_cutover, "inspect_backend", lambda: ("running", "time", "files"))
    with pytest.raises(lead_cutover.CutoverError, match="stop_file_backend"):
        lead_cutover.freeze_files(tmp_path)
    monkeypatch.setattr(lead_cutover, "inspect_backend", lambda: ("exited", "time", "files"))
    with pytest.raises(lead_cutover.CutoverError, match="file_deliveries_still_waiting"):
        lead_cutover.freeze_files(tmp_path)
    with outbox.open("a") as handle:
        handle.write('{"delivery_id":"one","status":"sent"}\n')
    telegram = logs / lead_cutover.TELEGRAM
    telegram.write_text('{"status":"pending"}\n')
    with pytest.raises(lead_cutover.CutoverError, match="file_deliveries_still_waiting"):
        lead_cutover.freeze_files(tmp_path)
    assert not list(tmp_path.glob("chatbot-cutover.*"))
    telegram.write_text("")

    report = lead_cutover.freeze_files(tmp_path)
    folder = Path(report["snapshot"])
    assert report["file_leads_by_company"] == {"clinic_a": 1, "clinic_b": 1}
    assert report["file_outbox"] == {"sent": 1}
    hashes = json.loads((folder / "manifest.json").read_text())
    assert set(hashes) == {lead_cutover.LEADS, lead_cutover.ARCHIVE}
    for name, checksum in hashes.items():
        assert (folder / name).read_bytes() == (logs / name).read_bytes()
        assert lead_cutover.digest(folder / name) == checksum
