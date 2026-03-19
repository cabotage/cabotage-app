import logging

import requests
from flask import current_app
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- cabotage-branch-deploy -->"

_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
}

_retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "POST", "PATCH"],
)
_adapter = HTTPAdapter(max_retries=_retry_strategy)

github_session = requests.Session()
github_session.mount("https://", _adapter)


def _github_headers(access_token):
    return {**_GITHUB_HEADERS, "Authorization": f"token {access_token}"}


def cabotage_url(application, path=""):
    """Build an external URL to a cabotage page for the given application.

    ``path`` is appended after the application base, e.g.
    ``cabotage_url(app, f"images/{image.id}")`` →
    ``https://cabotage.example.com/projects/org/proj/applications/app/images/123``
    """
    scheme = current_app.config["EXT_PREFERRED_URL_SCHEME"]
    server = current_app.config["EXT_SERVER_NAME"]
    org_slug = application.project.organization.slug
    project_slug = application.project.slug
    base = (
        f"{scheme}://{server}/projects/{org_slug}/{project_slug}"
        f"/applications/{application.slug}"
    )
    if path:
        return f"{base}/{path}"
    return base


def post_deployment_status_update(
    access_token, status_url, state, description, environment_url=None
):
    if access_token is None:
        return
    try:
        payload = {"state": state, "description": description}
        if environment_url:
            payload["environment_url"] = environment_url
        github_session.post(
            status_url,
            headers={
                "Accept": "application/vnd.github.ant-man-preview+json",
                "Authorization": f"token {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException:
        logger.exception(
            "Failed to post deployment status update to %s (state=%s)",
            status_url,
            state,
        )


class CheckRun:
    """Wrapper around a GitHub check run.

    Handles creation, progress updates, and completion.  All methods are
    no-ops when the check run could not be created (missing token, API
    error, etc.) so callers never need guard clauses.
    """

    def __init__(
        self, access_token, repo, application, check_run_id=None, app_env=None
    ):
        self.access_token = access_token
        self.repo = repo
        self.application = application
        self.check_run_id = check_run_id
        self.app_env = app_env

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        access_token,
        repo,
        head_sha,
        name,
        application,
        details_url=None,
        app_env=None,
    ):
        """Create a new check run on GitHub. Returns a CheckRun instance."""
        check_run_id = None
        if access_token:
            try:
                payload = {
                    "name": name,
                    "head_sha": head_sha,
                    "status": "in_progress",
                }
                if details_url:
                    payload["details_url"] = details_url
                resp = github_session.post(
                    f"https://api.github.com/repos/{repo}/check-runs",
                    headers=_github_headers(access_token),
                    json=payload,
                    timeout=10,
                )
                resp.raise_for_status()
                check_run_id = resp.json().get("id")
            except requests.exceptions.RequestException:
                logger.exception("Failed to create check run on %s", repo)
        return cls(access_token, repo, application, check_run_id, app_env=app_env)

    @classmethod
    def from_metadata(cls, metadata, app_env):
        """Restore a CheckRun from pipeline metadata (image/release/deploy).

        ``app_env`` is an ApplicationEnvironment (or None).  The application
        and repository are derived from it.
        """
        from cabotage.server import github_app

        application = app_env.application if app_env else None
        repo = application.github_repository if application else None
        if not metadata or not repo:
            return cls(None, repo, application, app_env=app_env)
        check_run_id = metadata.get("check_run_id")
        installation_id = metadata.get("installation_id")
        if not check_run_id or not installation_id:
            return cls(None, repo, application, app_env=app_env)
        access_token = github_app.fetch_installation_access_token(installation_id)
        return cls(access_token, repo, application, check_run_id, app_env=app_env)

    @property
    def active(self):
        """True if this check run was successfully created / restored."""
        return self.check_run_id is not None and self.access_token is not None

    # ------------------------------------------------------------------
    # URL / summary helpers
    # ------------------------------------------------------------------

    def _url(self, path=""):
        return cabotage_url(self.application, path)

    def _links(self, **resources):
        """Build a list of markdown links from keyword label=path pairs.

        Always appends an Application link at the end.
        """
        parts = []
        for label, path in resources.items():
            if path:
                parts.append(f"[{label}]({self._url(path)})")
        parts.append(f"[Application]({self._url()})")
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notify_pr(self):
        """Update the PR comment if this check run is for a branch deploy."""
        if self.app_env is None:
            return
        from cabotage.celery.tasks.branch_deploy import (
            maybe_update_pr_comment_for_app_env,
        )

        maybe_update_pr_comment_for_app_env(self.app_env)

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _patch(self, payload):
        if not self.active:
            return
        try:
            github_session.patch(
                f"https://api.github.com/repos/{self.repo}"
                f"/check-runs/{self.check_run_id}",
                headers=_github_headers(self.access_token),
                json=payload,
                timeout=10,
            )
        except requests.exceptions.RequestException:
            logger.exception(
                "Failed to update check run %s on %s", self.check_run_id, self.repo
            )

    def progress(self, title, detail="", details_url=None, **link_resources):
        """Update the check run output while it stays in_progress."""
        links = self._links(**link_resources) if link_resources else ""
        summary = f"**{title}**"
        if detail:
            summary += f"\n\n{detail}"
        if links:
            summary += f"\n\n{links}"
        payload = {
            "output": {"title": "In progress", "summary": summary},
        }
        if details_url:
            payload["details_url"] = details_url
        self._patch(payload)
        self._notify_pr()

    def succeed(
        self,
        title="Deployment complete!",
        detail="",
        details_url=None,
        **link_resources,
    ):
        """Complete the check run with success."""
        links = self._links(**link_resources) if link_resources else ""
        summary = f"**{title}**"
        if detail:
            summary += f"\n\n{detail}"
        if links:
            summary += f"\n\n{links}"
        payload = {
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "Success", "summary": summary},
        }
        if details_url:
            payload["details_url"] = details_url
        self._patch(payload)
        self._notify_pr()

    def fail(self, title, detail="", details_url=None, **link_resources):
        """Complete the check run with failure."""
        links = self._links(**link_resources) if link_resources else ""
        summary = f"**{title}**"
        if detail:
            summary += f"\n\n{detail}"
        if links:
            summary += f"\n\n{links}"
        payload = {
            "status": "completed",
            "conclusion": "failure",
            "output": {"title": "Failure", "summary": summary},
        }
        if details_url:
            payload["details_url"] = details_url
        self._patch(payload)
        self._notify_pr()


def find_or_create_pr_comment(access_token, repo, pr_number, body):
    """Find an existing bot comment by marker and update it, or create a new one."""
    if access_token is None:
        return
    headers = _github_headers(access_token)
    body_with_marker = f"{COMMENT_MARKER}\n{body}"

    try:
        page = 1
        while True:
            resp = github_session.get(
                f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=10,
            )
            resp.raise_for_status()
            comments = resp.json()
            if not comments:
                break
            for comment in comments:
                if COMMENT_MARKER in (comment.get("body") or ""):
                    github_session.patch(
                        f"https://api.github.com/repos/{repo}/issues/comments/{comment['id']}",
                        headers=headers,
                        json={"body": body_with_marker},
                        timeout=10,
                    )
                    return
            page += 1

        github_session.post(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            headers=headers,
            json={"body": body_with_marker},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        logger.exception(
            "Failed to update PR comment on %s#%s",
            repo,
            pr_number,
        )


def fetch_pr_changed_files(access_token, repo, pr_number):
    """Fetch the set of changed file paths for a PR.

    Paginates through all pages of the GitHub PR files endpoint.
    Returns a set of file path strings, or None on failure.
    """
    if access_token is None:
        return None
    headers = _github_headers(access_token)
    changed_files = set()
    page = 1
    try:
        while True:
            resp = github_session.get(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=10,
            )
            resp.raise_for_status()
            files = resp.json()
            if not files:
                break
            for f in files:
                changed_files.add(f["filename"])
            if len(files) < 100:
                break
            page += 1
    except requests.exceptions.RequestException:
        logger.exception("Failed to fetch changed files for %s#%s", repo, pr_number)
        return None
    return changed_files


def matches_watch_paths(changed_files, watch_patterns):
    """Check if any changed file matches any watch pattern.

    Uses pathspec with gitwildmatch (the same algorithm as .gitignore):
    - ``*`` matches anything except ``/``
    - ``**`` matches any number of directories
    - ``src/*.py`` matches files directly in src/
    - ``src/**`` matches everything under src/
    - ``Dockerfile`` (no slash) matches in any directory

    Returns True if any file matches any pattern, or if watch_patterns
    is empty/None (always match).
    """
    if not watch_patterns:
        return True
    import pathspec

    spec = pathspec.PathSpec.from_lines("gitwildmatch", watch_patterns)
    return any(spec.match_file(f) for f in changed_files)
