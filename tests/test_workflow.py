import io
import json
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observations import GROUPS, TASKS, TASK_BY_ID, report_text, task_status
from storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "observations")


def submit(store, task_id="water_02", **changes):
    task = TASK_BY_ID[task_id]
    values = dict(observation_date="2026-10-30", task_id=task_id,
        reporter=GROUPS[task.group]["leader"], actual_time=f"{task.hour:02d}:05",
        files=[("盐度记录.csv", b"sample,salinity\nA1,31.2", "data")])
    values.update(changes)
    return store.submit(**values)


def test_confirmed_schedule():
    assert len(TASKS) == 17
    assert len({t.id for t in TASKS}) == 17
    assert [t.hour for t in TASKS if t.project == "trawl"] == [2, 5, 8]
    assert [(TASK_BY_ID[f"ctd_{h:02}"].group, TASK_BY_ID[f"water_{h:02}"].group) for h in range(2, 9)] == [(1, 2)] * 4 + [(3, 4)] * 3
    assert sum(len(group["members"]) for group in GROUPS.values()) == 24
    assert all(t.hour not in (5.2, 5.3) for t in TASKS)


def test_upload_survives_new_connection_and_report_is_separate(store):
    row = submit(store)
    reopened = Store(store.root)
    saved = reopened.get(row["id"])
    assert task_status(saved) == "待群报"
    assert reopened.read_file(saved["files"][0]).endswith(b"31.2")
    assert "材料已上传：1 个文件" in report_text(saved)
    reopened.mark_reported(row["id"], "孙嘉远")
    assert task_status(store.get(row["id"])) == "已完成"
    assert store.latest("2026-10-31") == {}


def test_ctd_requires_both_synchronous_materials(store):
    with pytest.raises(ValueError, match="同时上传"):
        submit(store, "ctd_02", files=[("cast.cnv", b"CTD", "ctd")])
    assert store.list_records("2026-10-30") == []
    row = submit(store, "ctd_02", files=[("cast.cnv", b"CTD", "ctd"), ("气象.csv", b"weather", "weather")])
    assert len(row["files"]) == 2


@pytest.mark.parametrize("files", [[], [("empty.csv", b"", "data")]])
def test_missing_and_empty_materials_are_not_recorded(store, files):
    with pytest.raises(ValueError):
        submit(store, files=files)
    assert store.list_records("2026-10-30") == []


def test_wrong_group_and_invalid_date_are_rejected(store):
    with pytest.raises(ValueError):
        submit(store, reporter="马刚")
    with pytest.raises(ValueError):
        submit(store, observation_date="../../escape")
    assert store.latest("2026-10-30") == {}


def test_duplicate_names_safe_paths_and_bundle(store):
    row = submit(store, files=[("../../record.csv", b"one", "data"), ("C:\\temp\\record.csv", b"two", "data")])
    assert [file["name"] for file in row["files"]] == ["record.csv", "record.csv"]
    assert [store.read_file(file) for file in row["files"]] == [b"one", b"two"]
    with zipfile.ZipFile(io.BytesIO(store.bundle(row["id"]))) as archive:
        assert archive.read("data/01_record.csv") == b"one"
        assert archive.read("data/02_record.csv") == b"two"
        assert json.loads(archive.read("提交记录.json"))["id"] == row["id"]


def test_resubmission_preserves_old_material_and_resets_reporting(store):
    old = submit(store)
    store.mark_reported(old["id"], "孙嘉远")
    new = submit(store, files=[("盐度记录.csv", b"corrected", "data")])
    assert len(store.list_records("2026-10-30")) == 2
    assert store.latest("2026-10-30")["water_02"]["id"] == new["id"]
    assert task_status(store.get(new["id"])) == "待群报"
    assert store.read_file(old["files"][0]) != store.read_file(new["files"][0])
    with pytest.raises(ValueError, match="更新"):
        store.mark_reported(old["id"], "孙嘉远")


def test_retry_is_idempotent_and_concurrent_uploads_survive(store):
    token = str(uuid.uuid4())
    one = submit(store, submission_id=token)
    two = submit(store, submission_id=token)
    assert one["id"] == two["id"]
    assert len(store.list_records("2026-10-30")) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: submit(store), range(4)))
    assert len({r["id"] for r in rows}) == 4
    assert len(store.list_records("2026-10-30")) == 5


def test_file_integrity_checked(store):
    row = submit(store)
    file = row["files"][0]
    (store.root / file["relative_path"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="校验"):
        store.read_file(file)


def test_dashboard_filter_and_upload_form(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MARINE_DATA_DIR", str(tmp_path / "ui"))
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=20).run()
    assert not app.exception
    assert app.metric[0].value == "17"
    assert any('class="intro-screen"' in item.value for item in app.html)
    assert len([button for button in app.button if button.key and button.key.startswith("task_")]) == 17
    app.selectbox(key="group_filter").select(4).run()
    assert not app.exception
    assert not any('class="intro-screen"' in item.value for item in app.html)
    assert app.metric[0].value == "04"
    app.button(key="task_water_06").click().run()
    assert not app.exception
    assert len(app.get("file_uploader")) == 1
    app.form_submit_button[0].click().run()
    assert app.error
    assert Store(tmp_path / "ui").list_records("2026-10-30") == []


def test_library_and_report_confirmation(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("MARINE_DATA_DIR", str(tmp_path / "ui"))
    store = Store(tmp_path / "ui")
    row = submit(store)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=20)
    from datetime import date
    app.session_state["observation_date"] = date(2026, 10, 30)
    app.session_state["workspace"] = "数据资料库"
    app.run()
    assert not app.exception
    assert app.metric[1].value == "01"
    assert app.metric[2].value == "01"
    app.button(key=f"confirm_library_{row['id']}").click().run()
    assert not app.exception
    assert app.metric[3].value == "01"
    assert store.get(row["id"])["reported_at"]
