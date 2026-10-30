import base64
import copy
import hashlib
import io
import json
import uuid
import zipfile
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from github_storage import GitHubError, GitHubStore


class FakeGitHub:
    """A commit/ref model to exercise real atomic-write and race behavior offline."""
    can_write = True

    def __init__(self):
        self.head = None
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.before_publish = None
        self.fail_publish = False

    def blob(self, content):
        sha = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        self.blobs[sha] = content
        return sha

    def request(self, method, path, payload=None, raw=False):
        if path.startswith("git/ref/heads/"):
            if not self.head:
                raise GitHubError(404, "missing")
            return {"object": {"sha": self.head}}
        if method == "POST" and path == "git/blobs":
            return {"sha": self.blob(base64.b64decode(payload["content"]))}
        if method == "GET" and path.startswith("git/blobs/"):
            return self.blobs[path.rsplit("/", 1)[1]]
        if method == "POST" and path == "git/trees":
            tree = copy.deepcopy(self.trees.get(payload.get("base_tree"), {}))
            for entry in payload["tree"]:
                tree[entry["path"]] = entry.get("sha") or self.blob(entry["content"].encode())
            sha = hashlib.sha1(json.dumps(tree, sort_keys=True).encode()).hexdigest()
            self.trees[sha] = tree
            return {"sha": sha}
        if method == "POST" and path == "git/commits":
            sha = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = copy.deepcopy(payload)
            return {"sha": sha}
        if method == "GET" and path.startswith("git/commits/"):
            commit = self.commits[path.rsplit("/", 1)[1]]
            return {**commit, "tree": {"sha": commit["tree"]}}
        if method == "POST" and path == "git/refs":
            if self.head:
                raise GitHubError(422, "already exists")
            self.head = payload["sha"]
            return {"object": {"sha": self.head}}
        if method == "PATCH" and path.startswith("git/refs/heads/"):
            if self.fail_publish:
                raise GitHubError(503, "failed")
            if self.before_publish:
                callback, self.before_publish = self.before_publish, None
                callback()
            commit = self.commits[payload["sha"]]
            if commit["parents"] != [self.head] or payload["force"]:
                raise GitHubError(422, "non-fast-forward")
            self.head = payload["sha"]
            return {"object": {"sha": self.head}}
        if method == "GET" and path.startswith("contents/"):
            parts = urlsplit(path)
            filepath = unquote(parts.path.removeprefix("contents/"))
            commit = self.commits[parse_qs(parts.query)["ref"][0]]
            tree = self.trees[commit["tree"]]
            if filepath not in tree:
                raise GitHubError(404, "not found")
            return self.blobs[tree[filepath]]
        raise AssertionError((method, path))


def submit(store, task_id="water_02", **overrides):
    values = dict(observation_date="2026-10-30", task_id=task_id, reporter="孙嘉远", actual_time="02:05",
        files=[("记录.csv", b"station,salinity\nS1,31.2", "data")])
    values.update(overrides)
    return store.submit(**values)


@pytest.fixture
def remote():
    api = FakeGitHub()
    return GitHubStore("owner/data", client=api), api


def test_remote_upload_and_restart_keep_source_files_and_status(remote):
    store, api = remote
    record = submit(store)
    reopened = GitHubStore("owner/data", client=api)
    loaded = reopened.latest("2026-10-30")["water_02"]
    assert loaded["id"] == record["id"]
    assert reopened.read_file(loaded["files"][0]).endswith(b"31.2")
    reopened.mark_reported(record["id"], "孙嘉远")
    assert store.get(record["id"])["reported_at"]
    with zipfile.ZipFile(io.BytesIO(store.bundle(record["id"]))) as archive:
        assert archive.read("data/01_记录.csv").endswith(b"31.2")
    assert store.list_records("2026-10-31") == []


def test_conflicting_groups_are_merged_without_losing_either_submission(remote):
    store, api = remote
    store.ensure_branch()
    other = GitHubStore("owner/data", client=api)
    api.before_publish = lambda: submit(other, "water_06", reporter="李彦吉", actual_time="06:05")
    first = submit(store)
    rows = store.list_records("2026-10-30")
    assert len(rows) == 2
    assert {r["task_id"] for r in rows} == {"water_02", "water_06"}
    assert first["sequence"] == 2


def test_failed_github_commit_never_appears_as_uploaded(remote):
    store, api = remote
    store.ensure_branch()
    api.fail_publish = True
    with pytest.raises(GitHubError):
        submit(store)
    assert store.list_records("2026-10-30") == []


def test_remote_retry_is_idempotent_and_revisions_preserve_history(remote):
    store, api = remote
    token = str(uuid.uuid4())
    first = submit(store, submission_id=token)
    assert submit(store, submission_id=token)["id"] == first["id"]
    assert len(store.list_records("2026-10-30")) == 1
    store.mark_reported(first["id"], "孙嘉远")
    second = submit(store, files=[("记录.csv", b"new version", "data")])
    assert store.latest("2026-10-30")["water_02"]["id"] == second["id"]
    assert not second["reported_at"]
    assert store.read_file(first["files"][0]).endswith(b"31.2")
    with pytest.raises(ValueError, match="更新"):
        store.mark_reported(first["id"], "孙嘉远")


def test_ctd_validation_and_readonly_mode_do_not_write(remote):
    store, api = remote
    with pytest.raises(ValueError, match="单独"):
        submit(store, "ctd_02", reporter="马刚", files=[("cast.cnv", b"CTD", "ctd"), ("weather.csv", b"weather", "weather")])
    assert api.head is None
    api.can_write = False
    readonly = GitHubStore("owner/data", client=api)
    with pytest.raises(ValueError, match="令牌"):
        submit(readonly)
    assert not api.blobs


def test_github_saves_ctd_and_weather_in_separate_task_records(remote):
    store, api = remote
    ctd = submit(store, "ctd_03", reporter="马刚", files=[("cast.cnv", b"CTD", "ctd")])
    assert set(store.latest("2026-10-30")) == {"ctd_03"}
    weather = submit(store, "weather_03", reporter="马刚", files=[("weather.csv", b"weather", "weather")])
    reopened = GitHubStore("owner/data", client=api)
    assert set(reopened.latest("2026-10-30")) == {"ctd_03", "weather_03"}
    assert len(reopened.get(ctd["id"])["files"]) == 1
    assert reopened.read_file(reopened.get(weather["id"])["files"][0]) == b"weather"


def test_checksum_detects_damaged_remote_material(remote):
    store, api = remote
    record = submit(store)
    file = record["files"][0]
    api.blobs[file["blob_sha"]] = b"wrong contents"
    with pytest.raises(ValueError, match="校验"):
        store.read_file(file)


def test_data_cannot_be_written_into_code_branch():
    with pytest.raises(ValueError, match="独立"):
        GitHubStore("owner/data", branch="main")


def test_github_record_and_report_confirmation_in_ui(remote, monkeypatch):
    from pathlib import Path
    from datetime import date
    from streamlit.testing.v1 import AppTest
    import runtime_store
    store, api = remote
    record = submit(store)
    monkeypatch.setattr(runtime_store, "get_store", lambda: store)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=20)
    app.session_state["observation_date"] = date(2026, 10, 30)
    app.session_state["workspace"] = "数据资料库"
    app.run()
    assert not app.exception
    assert app.metric[1].value == "01"
    app.button(key=f"confirm_library_{record['id']}").click().run()
    assert not app.exception
    assert app.metric[3].value == "01"
    assert GitHubStore("owner/data", client=api).get(record["id"])["reported_at"]


def test_missing_token_disables_upload_in_ui(remote, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import runtime_store
    store, api = remote
    store.can_write = False
    monkeypatch.setattr(runtime_store, "get_store", lambda: store)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "streamlit_app.py"), default_timeout=20).run()
    assert not app.exception
    assert app.warning
    app.button(key="task_water_02").click().run()
    assert not app.exception
    assert app.error
    assert not app.form_submit_button
    assert not api.blobs
