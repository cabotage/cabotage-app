"""Datadog log destination settings shared by deployment and configuration UI."""

from typing import TYPE_CHECKING, Literal, TypeGuard, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cabotage.server.ext.config_writer import ConfigWriter
    from cabotage.server.models.projects import (
        Application,
        ApplicationEnvironment,
        Configuration,
        EnvironmentConfiguration,
    )

type LoggingConfigurations = Mapping[
    str, Configuration | EnvironmentConfiguration | None
]


class LoggingFieldDetails(TypedDict):
    configured: bool
    source: Literal["unset", "application", "shared"]
    source_label: str
    issues: list[str]
    secret: bool
    buildtime: bool


class LoggingDeploymentStatus(TypedDict):
    state: Literal["not_deployed", "pending", "deployed"]
    label: str
    description: str


DATADOG_SITES = (
    "datadoghq.com",
    "datadoghq.eu",
    "us3.datadoghq.com",
    "us5.datadoghq.com",
    "ap1.datadoghq.com",
    "ap2.datadoghq.com",
    "uk1.datadoghq.com",
    "ddog-gov.com",
    "us2.ddog-gov.com",
)

LOGGING_CONFIG_NAMES = {
    "enabled": "DD_LOGS_ENABLED",
    "site": "DD_SITE",
    "api_key": "DD_API_KEY",
}


def valid_api_key(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 2048
        and all(33 <= ord(character) <= 126 for character in value)
        and "*" not in value
        and "${" not in value
        and "{{" not in value
    )


def read_logging_value(
    configuration: Configuration | EnvironmentConfiguration | None,
    reader: ConfigWriter,
) -> object:
    """Read for server-side validation/testing, never for template context.

    Secret-backend values remain untrusted until the caller validates them.
    """
    if configuration is None:
        return None
    if configuration.secret:
        if not configuration.key_slug or ":" not in configuration.key_slug:
            raise ValueError("The saved secret has no readable storage path.")
        payload = cast(
            "Mapping[str, Mapping[str, object]]",
            reader.read(configuration.key_slug.split(":", 1)[1], secret=True),
        )
        return payload["data"][configuration.name]
    return configuration.value


def logging_configuration(
    application: Application, app_env: ApplicationEnvironment
) -> LoggingConfigurations:
    """Select the same effective objects used when taking a release snapshot."""
    objects: dict[str, Configuration | EnvironmentConfiguration] = {
        str(sub.environment_configuration.id): sub.environment_configuration
        for sub in app_env.environment_config_subscriptions
    }
    objects.update({str(config.id): config for config in app_env.configurations})
    resolved = cast(
        "Mapping[str, Mapping[str, object]]",
        application._resolved_configuration(app_env),
    )
    return {
        field: objects[cast("str", resolved[name]["id"])] if name in resolved else None
        for field, name in LOGGING_CONFIG_NAMES.items()
    }


def logging_field_details(
    configurations: LoggingConfigurations,
) -> dict[str, LoggingFieldDetails]:
    fields: dict[str, LoggingFieldDetails] = {}
    for field, config in configurations.items():
        source: Literal["unset", "application", "shared"] = (
            "unset"
            if config is None
            else "application"
            if hasattr(config, "application_id")
            else "shared"
        )
        issues = []
        if config is not None:
            if field == "api_key":
                if not config.secret:
                    issues.append(
                        "This API key is not stored as a secret. "
                        "Replace it here to store it securely."
                    )
                if not config.secret and not valid_api_key(config.value):
                    issues.append(
                        "The saved API key is missing or invalid. "
                        "Replace it before enabling export."
                    )
                if config.secret and not config.key_slug:
                    issues.append(
                        "The saved API key has no secret storage path. "
                        "Replace it before enabling export."
                    )
            elif config.secret:
                issues.append(
                    "This setting is stored as a secret and cannot be displayed. "
                    "Replace it to edit it here."
                )
            elif field == "enabled" and config.value.strip().lower() not in {
                "true",
                "false",
            }:
                issues.append(
                    "The saved value is not true or false; log export is not enabled."
                )
            elif field == "site" and config.value not in DATADOG_SITES:
                issues.append(
                    "The saved site is unsupported. Choose a supported Datadog "
                    "site before enabling export."
                )
            if config.buildtime:
                issues.append(
                    "This variable is exposed during builds. Existing flags are "
                    "preserved unless the API key is replaced."
                )
        fields[field] = {
            "configured": config is not None,
            "source": source,
            "source_label": {
                "unset": "Not configured",
                "application": "Application override",
                "shared": "Shared configuration",
            }[source],
            "issues": issues,
            "secret": bool(config and config.secret),
            "buildtime": bool(config and config.buildtime),
        }
    return fields


def logging_deployment_status(
    app_env: ApplicationEnvironment, configurations: LoggingConfigurations
) -> LoggingDeploymentStatus:
    deployment = app_env.latest_deployment_completed
    if deployment is None:
        return {
            "state": "not_deployed",
            "label": "Not deployed",
            "description": (
                "No completed deployment exists in this environment. Save settings, "
                "then create and deploy a release. Delivery has not been verified."
            ),
        }
    saved = {
        LOGGING_CONFIG_NAMES[field]: config.asdict
        for field, config in configurations.items()
        if config is not None
    }
    deployed_configuration = cast(
        "Mapping[str, object]", deployment.release.get("configuration") or {}
    )
    deployed = {
        name: snapshot
        for name, snapshot in deployed_configuration.items()
        if name in LOGGING_CONFIG_NAMES.values()
    }
    if saved != deployed:
        return {
            "state": "pending",
            "label": "Deployment required",
            "description": (
                "Saved logging settings differ from the last completed deployment. "
                "Create and deploy a new release to apply them. "
                "The previous settings may still be active."
            ),
        }
    return {
        "state": "deployed",
        "label": "Included in last deployment",
        "description": (
            "The last completed deployment contains these configuration versions. "
            "This does not verify collector health or log delivery."
        ),
    }
