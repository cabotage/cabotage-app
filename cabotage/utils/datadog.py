"""Datadog log destination settings shared by deployment and configuration UI."""

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


def valid_api_key(value):
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 2048
        and all(33 <= ord(character) <= 126 for character in value)
        and "*" not in value
        and "${" not in value
        and "{{" not in value
    )


def read_logging_value(configuration, reader):
    """Read only for server-side validation/testing, never for template context."""
    if configuration is None:
        return None
    if configuration.secret:
        if not configuration.key_slug or ":" not in configuration.key_slug:
            raise ValueError("The saved secret has no readable storage path.")
        payload = reader.read(configuration.key_slug.split(":", 1)[1], secret=True)
        return payload["data"][configuration.name]
    return configuration.value


def logging_configuration(application, app_env):
    """Select the same effective objects used when taking a release snapshot."""
    objects = {
        str(sub.environment_configuration.id): sub.environment_configuration
        for sub in app_env.environment_config_subscriptions
    }
    objects.update({str(config.id): config for config in app_env.configurations})
    resolved = application._resolved_configuration(app_env)
    return {
        field: objects[resolved[name]["id"]] if name in resolved else None
        for field, name in LOGGING_CONFIG_NAMES.items()
    }


def logging_field_details(configurations):
    fields = {}
    for field, config in configurations.items():
        source = (
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


def logging_deployment_status(app_env, configurations):
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
    deployed = {
        name: snapshot
        for name, snapshot in (deployment.release.get("configuration") or {}).items()
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
