import requests


def post_deployment_status_update(access_token, status_url, state, description):
    requests.post(
        status_url,
        headers={
            "Accept": "application/vnd.github.ant-man-preview+json",
            "Authorization": f"token {access_token}",
            "Content-Type": "application/json",
        },
        json={"state": state, "description": description},
        timeout=10
    )
