"""Server-side storage configuration. Credentials never enter browser state."""
import os
import shutil
import subprocess

import streamlit as st

from github_storage import GitHubStore
from storage import Store


@st.cache_resource
def _cli_token():
    if not shutil.which("gh"):
        return ""
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=10)
        return result.stdout.decode().strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


@st.cache_resource
def _github_store(repository, branch, token):
    return GitHubStore(repository, token=token, branch=branch)


def get_store():
    try:
        settings = st.secrets.to_dict()
    except FileNotFoundError:
        settings = {}
    backend = os.environ.get("MARINE_STORAGE", settings.get("storage_backend", "github"))
    if backend == "local":
        return Store()
    if backend != "github":
        raise ValueError("存储模式只能是github或local。")
    config = settings.get("github", {})
    repository = os.environ.get("MARINE_GITHUB_REPOSITORY", config.get("repository", "Pan-gao-1219/DFH2DATA"))
    branch = os.environ.get("MARINE_GITHUB_BRANCH", config.get("branch", "observation-data"))
    token = os.environ.get("MARINE_GITHUB_TOKEN") or config.get("token") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token and config.get("use_gh_cli", True):
        token = _cli_token()
    return _github_store(repository, branch, token)
