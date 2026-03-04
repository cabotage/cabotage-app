import logging

import requests

logger = logging.getLogger(__name__)

COMMENT_MARKER = "<!-- cabotage-branch-deploy -->"

_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
}


def _github_headers(access_token):
    return {**_GITHUB_HEADERS, "Authorization": f"token {access_token}"}


def post_deployment_status_update(access_token, status_url, state, description):
    if access_token is None:
        return
    try:
        requests.post(
            status_url,
            headers={
                "Accept": "application/vnd.github.ant-man-preview+json",
                "Authorization": f"token {access_token}",
                "Content-Type": "application/json",
            },
            json={"state": state, "description": description},
            timeout=10,
        )
    except requests.exceptions.RequestException:
        logger.exception(
            "Failed to post deployment status update to %s (state=%s)",
            status_url,
            state,
        )


def find_or_create_pr_comment(access_token, repo, pr_number, body):
    """Find an existing bot comment by marker and update it, or create a new one."""
    if access_token is None:
        return
    headers = _github_headers(access_token)
    body_with_marker = f"{COMMENT_MARKER}\n{body}"

    try:
        page = 1
        while True:
            resp = requests.get(
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
                    requests.patch(
                        f"https://api.github.com/repos/{repo}/issues/comments/{comment['id']}",
                        headers=headers,
                        json={"body": body_with_marker},
                        timeout=10,
                    )
                    return
            page += 1

        requests.post(
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
