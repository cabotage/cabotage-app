import re


TEMPLATE_PATTERN = re.compile(
    r"\$\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?\.(url|host)\}"
)


class TemplateResolutionError(Exception):
    """Raised when a template variable cannot be resolved."""

    pass


def has_template_variables(value):
    """Return True if the value contains any template variable references."""
    return "${" in value and TEMPLATE_PATTERN.search(value) is not None


def resolve_template_variables(value, application_environment):
    """Replace all ${app_slug.ingress_name.url} references in value
    with the resolved ingress URL from sibling apps in the same
    project + environment.

    Raises TemplateResolutionError if a referenced app or ingress
    does not exist or has no hosts.
    """
    if "${" not in value:
        return value

    siblings = _get_sibling_app_envs(application_environment)

    def _replace(match):
        app_slug = match.group(1)
        ingress_name = match.group(2)
        prop = match.group(3)

        sibling_app_env = siblings.get(app_slug)
        if sibling_app_env is None:
            raise TemplateResolutionError(
                f"Referenced application '{app_slug}' not found in project "
                f"'{application_environment.application.project.slug}' "
                f"environment '{application_environment.environment.slug}'"
            )

        return _resolve_ingress(sibling_app_env, ingress_name, app_slug, prop)

    return TEMPLATE_PATTERN.sub(_replace, value)


def _get_sibling_app_envs(app_env):
    """Return dict of {app_slug: ApplicationEnvironment} for all apps
    in the same project + environment, including the current app."""
    from cabotage.server.models.projects import ApplicationEnvironment

    project_id = app_env.application.project_id
    environment_id = app_env.environment_id

    sibling_app_envs = (
        ApplicationEnvironment.query.join(ApplicationEnvironment.application)
        .filter(
            ApplicationEnvironment.environment_id == environment_id,
            ApplicationEnvironment.application.has(project_id=project_id),
        )
        .all()
    )
    return {ae.application.slug: ae for ae in sibling_app_envs}


def _resolve_ingress(app_env, ingress_name, app_slug, prop="url"):
    """Resolve a single ingress reference to a URL or hostname string."""
    ingresses = [i for i in app_env.ingresses if i.enabled]

    if ingress_name is None:
        if len(ingresses) == 0:
            raise TemplateResolutionError(
                f"Application '{app_slug}' has no enabled ingresses"
            )
        if len(ingresses) > 1:
            raise TemplateResolutionError(
                f"Application '{app_slug}' has multiple ingresses; "
                f"use ${{{app_slug}.<ingress_name>.{prop}}} syntax"
            )
        ingress = ingresses[0]
    else:
        matches = [i for i in ingresses if i.name == ingress_name]
        if not matches:
            raise TemplateResolutionError(
                f"Ingress '{ingress_name}' not found on application '{app_slug}'"
            )
        ingress = matches[0]

    hosts = ingress.hosts
    non_auto = [h for h in hosts if not h.is_auto_generated]
    host = non_auto[0] if non_auto else (hosts[0] if hosts else None)

    if host is None:
        raise TemplateResolutionError(
            f"Ingress '{ingress.name}' on application '{app_slug}' has no hosts"
        )

    if prop == "host":
        return host.hostname

    scheme = "https" if host.tls_enabled else "http"
    return f"{scheme}://{host.hostname}"
