"""Post (or update) the Markdown report as a pull request comment.

Only the standard library is used so the GitHub Action needs nothing beyond the
package itself.  The comment carries a hidden marker; when a comment with that
marker already exists on the pull request it is edited in place, so repeated
pushes keep one up-to-date report instead of a growing thread.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cost_sentinel.models import CostSentinelError

logger = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- aws-cost-sentinel-report -->"
DEFAULT_API_URL = "https://api.github.com"
MAX_COMMENT_CHARS = 65_000
"""GitHub rejects comment bodies above 65,536 characters; keep a margin for the marker."""
TRUNCATION_NOTE = (
    "\n\n_Report truncated to fit GitHub's comment size limit; "
    "download the full report from the workflow artifacts._\n"
)
COMMENTS_PER_PAGE = 100
MAX_PAGES = 20


class GitHubError(CostSentinelError):
    """The GitHub API call failed or the pull request could not be identified."""


@dataclass(frozen=True)
class HttpResponse:
    """The parts of an HTTP response the client needs."""

    status: int
    body: bytes
    headers: dict[str, str]


Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]
"""``(method, url, headers, body) -> HttpResponse``; injectable for tests."""


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    """Default transport built on :mod:`urllib.request`."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return HttpResponse(response.status, response.read(), dict(response.headers))
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read(), dict(exc.headers or {}))
    except urllib.error.URLError as exc:
        raise GitHubError(f"cannot reach GitHub at {url}: {exc.reason}") from exc


def prepare_body(markdown: str) -> str:
    """Prefix the marker and truncate the report to GitHub's size limit."""
    body = f"{COMMENT_MARKER}\n{markdown}"
    if len(body) <= MAX_COMMENT_CHARS:
        return body
    cut = MAX_COMMENT_CHARS - len(TRUNCATION_NOTE)
    head = body[:cut]
    newline = head.rfind("\n")
    if newline > len(COMMENT_MARKER):
        head = head[:newline]
    return head + TRUNCATION_NOTE


def pr_number_from_event(event_path: str | Path | None) -> int | None:
    """Read the pull request number from a GitHub Actions event payload file."""
    if not event_path:
        return None
    path = Path(event_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cannot read GitHub event payload %s: %s", path, exc)
        return None
    pull_request = payload.get("pull_request") or {}
    number = pull_request.get("number") or payload.get("number")
    return int(number) if number else None


class GitHubClient:
    """Tiny REST client covering the two issue-comment endpoints the action needs."""

    def __init__(
        self,
        token: str,
        repository: str,
        api_url: str = DEFAULT_API_URL,
        transport: Transport | None = None,
    ) -> None:
        if not token:
            raise GitHubError("a GitHub token is required (set GITHUB_TOKEN or pass --token)")
        if repository.count("/") != 1:
            raise GitHubError(f"repository must look like owner/name, got {repository!r}")
        self._token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self._transport = transport or urllib_transport

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aws-cost-sentinel",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._transport(method, url, headers, data)
        if response.status >= 400:
            detail = response.body.decode("utf-8", errors="replace")[:500]
            raise GitHubError(f"GitHub {method} {path} returned {response.status}: {detail}")
        if not response.body:
            return None
        return json.loads(response.body.decode("utf-8"))

    def find_report_comment(self, pr_number: int) -> int | None:
        """Id of the existing report comment on the pull request, if any."""
        for page in range(1, MAX_PAGES + 1):
            comments = self._request(
                "GET",
                f"/repos/{self.repository}/issues/{pr_number}/comments"
                f"?per_page={COMMENTS_PER_PAGE}&page={page}",
            )
            if not comments:
                return None
            for comment in comments:
                if COMMENT_MARKER in (comment.get("body") or ""):
                    return int(comment["id"])
            if len(comments) < COMMENTS_PER_PAGE:
                return None
        return None

    def upsert_report_comment(self, pr_number: int, markdown: str) -> tuple[str, str]:
        """Create or update the report comment; returns ``(action, html_url)``."""
        body = prepare_body(markdown)
        existing = self.find_report_comment(pr_number)
        if existing is None:
            created = self._request(
                "POST",
                f"/repos/{self.repository}/issues/{pr_number}/comments",
                {"body": body},
            )
            action = "created"
            url = (created or {}).get("html_url", "")
        else:
            updated = self._request(
                "PATCH",
                f"/repos/{self.repository}/issues/comments/{existing}",
                {"body": body},
            )
            action = "updated"
            url = (updated or {}).get("html_url", "")
        logger.info("%s report comment on %s#%d", action, self.repository, pr_number)
        return action, url


def client_from_environment(
    token: str | None = None,
    repository: str | None = None,
    api_url: str | None = None,
    transport: Transport | None = None,
) -> GitHubClient:
    """Build a client from explicit values, falling back to the GitHub Actions variables."""
    resolved_repo = repository or os.environ.get("GITHUB_REPOSITORY", "")
    if not resolved_repo:
        raise GitHubError("no repository given (pass --repo or set GITHUB_REPOSITORY)")
    return GitHubClient(
        token=token or os.environ.get("GITHUB_TOKEN", ""),
        repository=resolved_repo,
        api_url=api_url or os.environ.get("GITHUB_API_URL", DEFAULT_API_URL),
        transport=transport,
    )
