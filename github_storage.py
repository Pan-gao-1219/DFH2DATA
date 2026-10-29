"""Durable GitHub storage: atomic commits on a dedicated data branch."""
import base64
import copy
import hashlib
import json
import re
import uuid
from datetime import date
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from observations import GROUPS, TASK_BY_ID, now_china
from storage import Store, validate_submission


class GitHubError(OSError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class GitHubAPI:
    def __init__(self, repository, token=""):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("GitHub仓库请填写为 owner/repository。")
        self.repository = repository
        self._token = token
        self.can_write = bool(token)

    def request(self, method, path, payload=None, raw=False):
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "DFH2DATA-observation-portal"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"https://api.github.com/repos/{self.repository}/{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                result = response.read()
        except HTTPError as exc:
            messages = {401: "GitHub令牌无效或已过期，请更新网站Secrets。",
                403: "GitHub拒绝访问，请检查令牌的Contents读写权限或API调用额度。",
                404: "GitHub仓库、分支或材料不存在，请检查仓库配置和令牌访问范围。",
                409: "GitHub数据正在更新，请重试。", 422: "GitHub数据更新冲突，请重试。"}
            raise GitHubError(exc.code, messages.get(exc.code, f"GitHub暂时返回HTTP {exc.code}，尚未确认保存成功。")) from None
        except (URLError, TimeoutError, OSError):
            raise GitHubError(0, "连接GitHub失败，尚未确认保存成功。请保留材料并重试。") from None
        return result if raw else json.loads(result)


class GitHubStore:
    # Reuse download packaging and filename handling; these methods do not use SQLite.
    clean_name = staticmethod(Store.clean_name)
    bundle = Store.bundle
    latest = Store.latest
    storage_label = "GitHub仓库保存"

    def __init__(self, repository, token="", branch="observation-data", client=None):
        if branch in {"main", "master"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", branch):
            raise ValueError("请使用独立的数据分支，例如observation-data，不能使用main/master。")
        self.repository, self.branch = repository, branch
        self.api = client or GitHubAPI(repository, token)
        self.can_write = self.api.can_write
        self.repository_url = f"https://github.com/{repository}/tree/{branch}"

    def _request(self, method, path, payload=None, raw=False):
        return self.api.request(method, path, payload, raw)

    def _require_write(self):
        if not self.can_write:
            raise ValueError("尚未配置GitHub写入令牌。请在Streamlit的Secrets中填写github.token。")

    def _head(self):
        try:
            return self._request("GET", f"git/ref/heads/{self.branch}")["object"]["sha"]
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def _blob(self, content):
        result = self._request("POST", "git/blobs", {"encoding": "base64", "content": base64.b64encode(content).decode("ascii")})
        expected = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        if result["sha"] != expected:
            raise ValueError("GitHub文件校验失败，提交未完成。")
        return result["sha"]

    @staticmethod
    def _json_entry(path, value):
        return {"path": path, "mode": "100644", "type": "blob", "content": json.dumps(value, ensure_ascii=False, indent=2)}

    def ensure_branch(self):
        self._require_write()
        head = self._head()
        if head:
            return head
        tree = self._request("POST", "git/trees", {"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
            "content": "# 东方红2观测材料\n\n此分支保存网站上传的原始材料、逐次提交记录和按日期生成的索引。\n\n网站源码位于main分支。请保留本数据分支，不要合并到main。\n"}]})
        commit = self._request("POST", "git/commits", {"message": "Initialize observation data storage", "tree": tree["sha"], "parents": []})
        try:
            self._request("POST", "git/refs", {"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]})
            return commit["sha"]
        except GitHubError as exc:
            if exc.status in (409, 422):
                head = self._head()
                if head:
                    return head
            raise

    @lru_cache(maxsize=64)
    def _read_json(self, path, head):
        # Commit SHAs are immutable; cache only snapshots, never mutable branch contents.
        try:
            raw = self._request("GET", f"contents/{quote(path, safe='/')}?{urlencode({'ref': head})}", raw=True)
            return json.loads(raw)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def _records_at(self, observation_date, head):
        return copy.deepcopy(self._read_json(f"indexes/{observation_date}.json", head) or []) if head else []

    def list_records(self, observation_date):
        date.fromisoformat(observation_date)
        return sorted(self._records_at(observation_date, self._head()), key=lambda r: r["sequence"], reverse=True)

    def get(self, submission_id):
        submission_id = str(uuid.UUID(submission_id))
        head = self._head()
        return copy.deepcopy(self._read_json(f"records/{submission_id}.json", head)) if head else None

    def _update_day(self, observation_date, update, entries, message):
        self._require_write()
        self.ensure_branch()
        for _ in range(6):
            head = self._head()
            records = self._records_at(observation_date, head)
            result, changed = update(records)
            if not changed:
                return result
            parent = self._request("GET", f"git/commits/{head}")
            changes = entries + [self._json_entry(f"records/{result['id']}.json", result), self._json_entry(f"indexes/{observation_date}.json", records)]
            tree = self._request("POST", "git/trees", {"base_tree": parent["tree"]["sha"], "tree": changes})
            commit = self._request("POST", "git/commits", {"message": message, "tree": tree["sha"], "parents": [head]})
            try:
                self._request("PATCH", f"git/refs/heads/{self.branch}", {"sha": commit["sha"], "force": False})
                return result
            except GitHubError as exc:
                if exc.status not in (409, 422):
                    raise
                # Another group submitted first. Reload and replay against the new head.
        raise ValueError("其他小组正在同时提交，请稍后重试；你的原始文件不会被覆盖。")

    def submit(self, *, observation_date, task_id, reporter, actual_time, station="", notes="", files, submission_id=None):
        submission_id = validate_submission(observation_date, task_id, reporter, actual_time, station, notes, files, submission_id)
        self._require_write()
        existing = self.get(submission_id)
        if existing:
            if existing["observation_date"] != observation_date or existing["task_id"] != task_id:
                raise ValueError("重复提交编号与任务不符。")
            return existing
        # Upload file objects first. They remain unpublished if any subsequent step fails.
        record = {"id": submission_id, "observation_date": observation_date, "task_id": task_id, "reporter": reporter,
            "actual_time": actual_time, "station": station.strip(), "notes": notes.strip(), "created_at": now_china().isoformat(timespec="seconds"),
            "reported_at": None, "reported_by": None, "files": []}
        entries = []
        for i, (name, content, category) in enumerate(files, 1):
            file_id, name = str(uuid.uuid4()), self.clean_name(name)
            path = f"uploads/{observation_date}/{task_id}/{submission_id}/{category}/{i:02d}_{name}"
            blob = self._blob(content)
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
            record["files"].append({"id": file_id, "submission_id": submission_id, "name": name, "category": category,
                "relative_path": path, "blob_sha": blob, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})

        def append(records):
            existing = next((row for row in records if row["id"] == submission_id), None)
            if existing:
                if existing["task_id"] != task_id:
                    raise ValueError("重复提交编号与任务不符。")
                return existing, False
            record["sequence"] = max((row["sequence"] for row in records), default=0) + 1
            records.append(copy.deepcopy(record))
            return record, True
        return self._update_day(observation_date, append, entries, f"Save {observation_date} {task_id} materials [{submission_id[:8]}]")

    def mark_reported(self, submission_id, reporter):
        record = self.get(submission_id)
        if not record:
            raise ValueError("找不到这条提交。")
        task = TASK_BY_ID[record["task_id"]]
        if reporter not in GROUPS[task.group]["members"]:
            raise ValueError("请选择负责小组的汇报确认人。")

        def update(records):
            latest = max((r for r in records if r["task_id"] == task.id), key=lambda r: r["sequence"])
            if latest["id"] != submission_id:
                raise ValueError("已有更新的材料，请刷新后确认最新一条的群汇报。")
            if latest["reported_at"]:
                return latest, False
            latest["reported_at"], latest["reported_by"] = now_china().isoformat(timespec="seconds"), reporter
            return latest, True
        return self._update_day(record["observation_date"], update, [], f"Confirm group report [{submission_id[:8]}]")

    def read_file(self, file):
        blob = file.get("blob_sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", blob):
            raise ValueError("GitHub材料编号无效。")
        content = self._request("GET", f"git/blobs/{blob}", raw=True)
        if len(content) != file["size"] or hashlib.sha256(content).hexdigest() != file["sha256"]:
            raise ValueError("材料校验未通过，请检查GitHub存储。")
        return content
