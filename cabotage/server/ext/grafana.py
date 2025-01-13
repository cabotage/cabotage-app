"""Extension module for Grafana integration using `grafana-api-sdk`."""
from pathlib import Path

import os
import ssl
import json

import logging

from grafana_api.dashboard import Dashboard
from grafana_api.model import APIModel, TeamObject
from grafana_api.organisation import OrganisationAdmin
from grafana_api.datasource import Datasource
from grafana_api.team import Team
from grafana_api.user import CurrentUser

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("CABOTAGE_GRAFANA_LOG_LEVEL", 10))
logger.addHandler(logging.StreamHandler())

ssl_ctx = ssl.create_default_context(
    ssl.Purpose.SERVER_AUTH,
    cafile="/var/run/secrets/cabotage.io/ca.crt"
)
ssl_ctx.verify_mode = ssl.CERT_REQUIRED

grafana = APIModel(
    # TODO: investigate configmap envvar exposure
    host=os.getenv("CABOTAGE_GRAFANA_URL"), #"http://grafana:3000",
    # token=os.getenv("CABOTAGE_GRAFANA_API_KEY"),
    # TODO: Figure out how we can store user/pass since grafana open source only allows
    #       to do us to CRUD resources with the org admin user via basic auth (no api access) :(
    username=os.getenv("CABOTAGE_GRAFANA_ORG_ADMIN_USER"),
    password=os.getenv("CABOTAGE_GRAFANA_ORG_ADMIN_PASS"),
    ssl_context=ssl_ctx
)


"""
Infra
"""

def create_datasource(org: str, org_id: int) -> None:
    try:
        logger.info(f"Creating Grafana datasource for the {org} organization.")
        datasource_api = Datasource(grafana)
        # Optionally instead of the below we could go a bit longer route
        # and create a new org svc account with the basic auth admin
        # then use that svc account to create that orgs api key
        # then create the datasource
        # then delete the svc account
        user_api = CurrentUser(grafana)
        user_api.switch_current_user_context(org_id)

        datasource = {
            "name": f"{org} Logs",
            "type": "loki",
            "url": "https://loki-read:3100/",
            "access": "proxy",
            "basicAuth": False,
            "jsonData": {
                "httpHeaderName1": "X-Scope-OrgID",
                "tlsAuthWithCACert": True
            },
            "secureJsonData": {
                "httpHeaderValue1": org,
                "tlsCACert": Path("/var/run/secrets/cabotage.io/ca.crt").read_text()
                }
            }
        return datasource_api.create_datasource(datasource)
    except Exception as exc:
        logger.exception("Error creating Grafana datasource")
        raise exc

def create_org(org_name: str) -> int:
    """Create a Grafana organization given a name.

    Returns:
        The created organization's ID.
    """
    try:
        logger.info(f"Creating Grafana organization: {org_name}")
        org = OrganisationAdmin(grafana)
        org_id = org.create_organization(org_name)
        create_datasource(org_name, org_id)
        return org_id
    except Exception as exc:
        logger.exception("Error creating Grafana organization")
        raise exc


def create_team(team_name: str, org_id: int) -> int:
    try:
        logger.info(f"Creating Grafana team: {team_name}")
        team_api = Team(grafana)
        team_email = f"{team_name.lower().replace(' ', '-')}@cabo.local"
        team_obj = TeamObject(name=team_name, email=team_email, org_id=org_id)
        return team_api.add_team(team_obj)
    except Exception as exc:
        logger.exception("Error creating Grafana team")
        raise exc

def create_app_dashboard(app: str, org: str, project: str) -> None:
    """Create a Grafana dashboard for an app with a pre-defined query with Loki logs."""

    try:
        logger.info(f"Creating Grafana dashboard for app: {app} in project: {org}/{project}")
        dashboard_api = Dashboard(grafana)

        dashboard = {
            "title": f"{project}-{app} Logs",
            "tags": [org, project, app, "auto-generated"],
            "panels": [{
                "title": f"{app} Logs",
                "type": "logs",
                "datasource": {
                    "type": "loki",
                    "uid": "loki"
                },
                "gridPos": {
                    "h": 24,
                    "w": 24,
                    "x": 0,
                    "y": 0
                },
                "targets": [{
                    "expr": '{namespace="' + org + '", app="' + app + '"}',
                    "refId": "A"
                }],
                "options": {
                    "showTime": True,
                    "sortOrder": "Descending",
                    "wrapLogMessage": True
                }
            }],
            "refresh": "30s",
            "schemaVersion": 36,
            "version": 0,
            "time": {
                "from": "now-6h",
                "to": "now"
            }
        }

        dashboard_api.create_or_update_dashboard(
            dashboard_path="General",
            dashboard_json=dashboard,
            message=f"Automated dashboard creation via Cabotage app event in the {org}/{project} project.",
        )
    except Exception as exc:
        logger.exception("Error creating Grafana dashboard")
        raise exc


"""
RBACish
"""

def assign_user_to_team(user_email: str, team_name: str) -> None:
    ...

def assign_dashboard_to_team(dashboard_name: str, team_name: str) -> None:
    ...
