import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def _build_cabotage_url(path):
    server = current_app.config.get("EXT_SERVER_NAME")
    if not server:
        return None
    scheme = current_app.config.get("EXT_PREFERRED_URL_SCHEME", "https")
    return f"{scheme}://{server}{path}"


def github_deployment_url(github_deployment_id, application):
    return _build_cabotage_url(
        f"/projects/{application.project.organization.slug}/{application.project.slug}"
        f"/applications/{application.slug}"
        f"/github-deployments/{github_deployment_id}"
    )


def post_deployment_status_update(
    access_token, status_url, state, description, log_url=None, environment_url=None
):
    if access_token is None:
        return
    try:
        payload = {"state": state, "description": description}
        if log_url is not None:
            payload["log_url"] = log_url
        if environment_url is not None:
            payload["environment_url"] = environment_url
        requests.post(
            status_url,
            headers={
                "Accept": "application/vnd.github.flash-preview+json",
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
