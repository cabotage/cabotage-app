"""Extension module for Grafana integration using `grafana-api-sdk`."""

from flask_login import current_user
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
from grafana_api.user import CurrentUser, User

from cabotage.server.models import (
    Organization as CabotageOrganization,
    User as CabotageUser,
)
from cabotage.server import db
from cabotage.server.models.projects import Project
from cabotage.utils.grafana_auth import generate_grafana_jwt

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("CABOTAGE_GRAFANA_LOG_LEVEL", 10))
logger.addHandler(logging.StreamHandler())

# Note: disable when developing in standalone Docker
ssl_ctx = ssl.create_default_context(
    ssl.Purpose.SERVER_AUTH,
    cafile="/var/run/secrets/cabotage.io/ca.crt"
)
ssl_ctx.verify_mode = ssl.CERT_REQUIRED

grafana = APIModel(
    # TODO: investigate configmap envvar exposure
    host=os.getenv("CABOTAGE_GRAFANA_URL") or "http://grafana:3000",
    username=os.getenv("CABOTAGE_GRAFANA_ORG_ADMIN_USER") or "admin",
    password=os.getenv("CABOTAGE_GRAFANA_ORG_ADMIN_PASS") or "admin",
    # Note: disable when developing in standalone Docker
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
            "jsonData": {"httpHeaderName1": "X-Scope-OrgID", "tlsAuthWithCACert": True},
            "secureJsonData": {
                "httpHeaderValue1": org,
                # Note: disable when developing in standalone Docker
                # "tlsCACert": Path("/var/run/secrets/cabotage.io/ca.crt").read_text()
            },
        }
        return datasource_api.create_datasource(datasource)
    except Exception as exc:
        logger.exception("Error creating Grafana datasource")
        raise exc


def create_grafana_org(org_name: str) -> int:
    """Create a Grafana organization AND it's datasource, given a name."""
    try:
        logger.info(f"Creating Grafana organization: {org_name}")
        org_admin = OrganisationAdmin(grafana)

        # First try to find if org already exists
        try:
            existing_org = org_admin.get_organization_by_name(org_name)
            if existing_org and "id" in existing_org:
                logger.info(f"Found existing Grafana org with ID: {existing_org['id']}")
                return existing_org["id"]
        except Exception:
            logger.info(f"No existing organization found with name: {org_name}")

        # Create a new org since we couldn't find an existing one
        org_id = org_admin.create_organization(org_name)
        logger.info(f"Successfully created new Grafana org with ID: {org_id}")

        try:
            create_datasource(org_name, org_id)
            logger.info(f"Created default datasource for org ID: {org_id}")
        except Exception as ds_exc:
            logger.exception("Failed to create datasource")
            # Don't fail the whole operation if datasource creation fails

        return org_id
    except Exception as exc:
        logger.exception(f"Error creating Grafana organization: {org_name}")
        raise ValueError(f"Failed to create Grafana organization: {str(exc)}")


def create_grafana_team(project: Project, team_name: str, org_id: int) -> int:
    try:
        logger.info(f"Creating Grafana team: {team_name} for org_id: {org_id}")
        team_api = Team(grafana)
        team_email = f"{team_name.lower().replace(' ', '-')}@cabo.local"
        team_obj = TeamObject(name=team_name, email=team_email, org_id=org_id)
        team_id = team_api.add_team(team_obj)
        if not team_id:
            logger.error("Grafana API returned falsy team_id")
            raise ValueError("Invalid team_id returned from Grafana API")
        return team_id
    except Exception as exc:
        logger.exception("Error creating Grafana team")
        raise exc


def create_grafana_app_dashboard(app: str, org: str, project: str) -> None:
    """Create a Grafana dashboard for an app with a pre-defined query with Loki logs."""

    try:
        logger.info(
            f"Creating Grafana dashboard for app: {app} in project: {org}/{project}"
        )
        dashboard_api = Dashboard(grafana)

        dashboard = {
            "title": f"{project}-{app} Logs",
            "tags": [org, project, app, "auto-generated"],
            "panels": [
                {
                    "title": f"{app} Logs",
                    "type": "logs",
                    "datasource": {"type": "loki", "uid": "loki"},
                    "gridPos": {"h": 24, "w": 24, "x": 0, "y": 0},
                    "targets": [
                        {
                            "expr": '{namespace="' + org + '", app="' + app + '"}',
                            "refId": "A",
                        }
                    ],
                    "options": {
                        "showTime": True,
                        "sortOrder": "Descending",
                        "wrapLogMessage": True,
                    },
                }
            ],
            "refresh": "30s",
            "schemaVersion": 36,
            "version": 0,
            "time": {"from": "now-6h", "to": "now"},
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


def assign_user_to_team(user_email: str, team_name: str) -> None: ...


def assign_dashboard_to_team(dashboard_name: str, team_name: str) -> None: ...


def assign_user_to_grafana_org(
    user_email: str, org_id: int, role: str = "Viewer"
) -> None:
    """Assign a user to a Grafana organization with Viewer role."""
    try:
        logger.info(f"Assigning user {user_email} to Grafana org {org_id}")
        org = OrganisationAdmin(grafana)
        user_api = User(grafana)
        try:
            if user := user_api.get_user_by_username_or_email(
                username_or_email=user_email
            ):
                org.add_organization_user(
                    org_id=org_id,
                    login_or_email=user_email,
                    role=role,
                )
                logger.info(f"User {user_email} assigned to Grafana org {org_id}")
            else:
                logger.error(
                    f"User {user_email} not found in Grafana! Creating user..."
                )
                generate_grafana_jwt(current_user)
        except Exception as exc:
            logger.exception(
                f"Error retrieving or creating user {user_email} from Grafana"
            )
    except Exception as exc:
        logger.exception("Error assigning user to Grafana organization")
        raise exc


# def ensure_grafana_user_access(user: User, organization: CabotageOrganization, role: str = "Viewer"):
#     """Ensure user exists in Grafana and has proper org access"""
#     jwt = generate_grafana_jwt(user)
#     try:
#         assign_user_to_grafana_org(user.email, organization.grafana_org_id, role=role)
#     except Exception as e:
#         logger.exception("Failed to assign user to Grafana org")
#         raise
#
# def ensure_grafana_org(organization: CabotageOrganization) -> int:
#     """Ensure Grafana org exists"""
#     if not organization.grafana_org_id:
#         grafana_org_id = create_grafana_org(organization.name)
#         organization.grafana_org_id = grafana_org_id
#         db.session.commit()
#     return organization.grafana_org_id


def setup_grafana_integration(
    organization: CabotageOrganization, user: CabotageUser
) -> None:
    """Helper function to handle Grafana organization integration"""
    try:
        if grafana_org_id := create_grafana_org(organization.name):
            organization.grafana_org_id = grafana_org_id
            db.session.add(organization)
            db.session.flush()

            # Add the user to the Grafana org as an admin
            logger.info(
                f"Adding user {user.email} as admin to Grafana org {grafana_org_id}"
            )
            # admin_jwt = generate_grafana_jwt(user)
            assign_user_to_grafana_org(
                user.email,
                grafana_org_id,
                role="Admin",
                # auth_token=admin_jwt
            )
        else:
            logger.error(
                f"Failed to get Grafana org ID for organization: {organization.name}"
            )
            raise ValueError("Failed to create Grafana organization")
    except Exception as exc:
        logger.exception("Failed to complete Grafana integration")
        raise
