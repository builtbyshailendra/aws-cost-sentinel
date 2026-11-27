"""Pull request comment client, exercised against an in-memory fake of the GitHub API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_sentinel.github import (
    COMMENT_MARKER,
    MAX_COMMENT_CHARS,
    GitHubClient,
    GitHubError,
    HttpResponse,
    client_from_environment,
    pr_number_from_event,
    prepare_body,
)


class FakeGitHub:
    """Records requests and serves issue comments for one pull request."""

    def __init__(self, comments: list[dict[str, object]] | None = None) -> None:
        self.comments = comments or []
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse:
        assert headers["Authorization"] == "Bearer test-token"
        payload = json.loads(body) if body else None
        self.requests.append((method, url, payload))
        if method == "GET":
            return HttpResponse(200, json.dumps(self.comments).encode(), {})
        if method == "POST":
            return HttpResponse(201, b'{"html_url": "https://github.example/c/1"}', {})
        if method == "PATCH":
            return HttpResponse(200, b'{"html_url": "https://github.example/c/7"}', {})
        return HttpResponse(405, b"unsupported", {})


def test_creates_a_comment_when_none_exists() -> None:
    fake = FakeGitHub([{"id": 3, "body": "LGTM"}])
    client = GitHubClient("test-token", "quillfern/infra", "https://api.example", transport=fake)
    action, url = client.upsert_report_comment(12, "# Report")
    assert (action, url) == ("created", "https://github.example/c/1")
    method, request_url, payload = fake.requests[-1]
    assert method == "POST"
    assert request_url == "https://api.example/repos/quillfern/infra/issues/12/comments"
    assert payload is not None
    assert str(payload["body"]).startswith(COMMENT_MARKER)


def test_updates_the_existing_report_comment() -> None:
    fake = FakeGitHub([{"id": 7, "body": f"{COMMENT_MARKER}\nold report"}])
    client = GitHubClient("test-token", "quillfern/infra", transport=fake)
    action, _ = client.upsert_report_comment(12, "# New report")
    assert action == "updated"
    method, request_url, _ = fake.requests[-1]
    assert method == "PATCH"
    assert request_url.endswith("/repos/quillfern/infra/issues/comments/7")


def test_api_errors_are_raised_with_status() -> None:
    def denied(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        return HttpResponse(403, b'{"message": "Resource not accessible by integration"}', {})

    client = GitHubClient("test-token", "quillfern/infra", transport=denied)
    with pytest.raises(GitHubError, match="403"):
        client.upsert_report_comment(1, "x")


def test_client_validation_and_environment_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GitHubError, match="token"):
        GitHubClient("", "quillfern/infra")
    with pytest.raises(GitHubError, match="owner/name"):
        GitHubClient("t", "not-a-repo")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "quillfern/env-repo")
    client = client_from_environment()
    assert client.repository == "quillfern/env-repo"
    assert client.api_url == "https://api.github.com"


def test_long_reports_are_truncated_on_a_line_boundary() -> None:
    body = prepare_body("| row |\n" * 20_000)
    assert len(body) <= MAX_COMMENT_CHARS
    assert body.startswith(COMMENT_MARKER)
    assert "truncated" in body
    assert "| row |\n\n_Report truncated" in body


def test_pr_number_from_event_payload(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")
    assert pr_number_from_event(event) == 42
    event.write_text(json.dumps({"ref": "refs/heads/main"}), encoding="utf-8")
    assert pr_number_from_event(event) is None
    assert pr_number_from_event(tmp_path / "missing.json") is None
    assert pr_number_from_event(None) is None
