import re

TEMPLATE_PATTERN = re.compile(
    r"\$\{([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_-]+))?\.(url|host|svc|hostname|port)\}"
)

SHARED_TEMPLATE_PATTERN = re.compile(r"\$\{shared\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TemplateResolutionError(Exception):
    """Raised when a template variable cannot be resolved."""

    pass


def has_template_variables(value):
    """Return True if the value contains any template variable references."""
    if "${" not in value:
        return False
    return (
        TEMPLATE_PATTERN.search(value) is not None
        or SHARED_TEMPLATE_PATTERN.search(value) is not None
    )


def resolve_template_variables(value, application_environment, reader=None):
    """Replace all template variable references in value.

    Supported forms:
      ${app_slug.url}                  - ingress URL (single ingress)
      ${app_slug.host}                 - ingress hostname (single ingress)
      ${app_slug.ingress_name.url}     - ingress URL (named)
      ${app_slug.ingress_name.host}    - ingress hostname (named)
      ${app_slug.process_name.hostname} - k8s service FQDN for a tcp* process
      ${app_slug.process_name.svc}     - k8s service FQDN:port for a tcp* process
      ${app_slug.process_name.port}    - service port for a tcp* process
      ${shared.VAR_NAME}               - value of a shared environment variable

    Raises TemplateResolutionError if a referenced app, ingress,
    process, or shared variable does not exist.
    """
    if "${" not in value:
        return value

    # Resolve shared references first to avoid collisions with service
    # patterns (e.g. ${shared.host} would otherwise match TEMPLATE_PATTERN)
    if SHARED_TEMPLATE_PATTERN.search(value):
        value = _resolve_shared_references(value, application_environment, reader)

    if TEMPLATE_PATTERN.search(value):
        siblings = _get_sibling_app_envs(application_environment)

        def _replace(match):
            app_slug = match.group(1)
            name = match.group(2)
            prop = match.group(3)

            sibling_app_env = siblings.get(app_slug)
            if sibling_app_env is None:
                raise TemplateResolutionError(
                    f"Referenced application '{app_slug}' not found in project "
                    f"'{application_environment.application.project.slug}' "
                    f"environment '{application_environment.environment.slug}'"
                )

            if prop in ("svc", "hostname", "port"):
                return _resolve_tcp_service(sibling_app_env, name, app_slug, prop)
            return _resolve_ingress(sibling_app_env, name, app_slug, prop)

        value = TEMPLATE_PATTERN.sub(_replace, value)

    return value


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


def _resolve_tcp_service(app_env, process_name, app_slug, prop="svc"):
    """Resolve a TCP service reference to its cluster-internal address.

    Returns:
      hostname - k8s service FQDN
      svc      - FQDN:port
      port     - port number
    """
    from cabotage.server.models.utils import safe_k8s_name

    if process_name is None:
        raise TemplateResolutionError(
            f"TCP service reference for '{app_slug}' requires a process name; "
            f"use ${{{app_slug}.<process_name>.{prop}}} syntax"
        )

    if prop == "port":
        return "8000"

    # Build the k8s service FQDN:
    #   {resource_prefix}-{process_name}.{namespace}.svc.cluster.local
    app = app_env.application
    project = app.project
    org = project.organization

    resource_prefix = safe_k8s_name(project.k8s_identifier, app.k8s_identifier)
    service_name = f"{resource_prefix}-{process_name}"

    if app_env.k8s_identifier is not None:
        namespace = safe_k8s_name(
            org.k8s_identifier, app_env.environment.k8s_identifier
        )
    else:
        namespace = org.k8s_identifier

    fqdn = f"{service_name}.{namespace}.svc.cluster.local"

    if prop == "hostname":
        return fqdn

    return f"{fqdn}:8000"


def resolve_shared_secret_refs(value, application_environment):
    """Extract ${shared.VAR} references that point to secret env configs.

    Returns a list of (alias_name, EnvironmentConfiguration) tuples for
    each secret shared ref found.  Only whole-value references are supported
    (the entire config value must be exactly ``${shared.NAME}``).

    Non-secret shared refs are handled by normal template resolution and
    are NOT returned here.
    """
    from cabotage.server.models.projects import EnvironmentConfiguration

    match = SHARED_TEMPLATE_PATTERN.fullmatch(value.strip())
    if match is None:
        return []

    var_name = match.group(1)
    ec = EnvironmentConfiguration.query.filter_by(
        project_id=application_environment.application.project_id,
        environment_id=application_environment.environment_id,
        name=var_name,
        deleted=False,
    ).first()

    if ec is None or not ec.secret:
        return []

    return [(var_name, ec)]


def _resolve_shared_references(value, application_environment, reader=None):
    """Replace ${shared.VAR_NAME} references with the value from
    environment-level configurations.

    For secret shared vars, a reader is required to fetch the value from
    vault. This is used during image builds (buildargs). At runtime,
    secret shared refs are handled via envconsul key format directives
    instead of inline resolution.
    """
    from cabotage.server.models.projects import EnvironmentConfiguration

    env_configs = {
        ec.name: ec
        for ec in EnvironmentConfiguration.query.filter_by(
            project_id=application_environment.application.project_id,
            environment_id=application_environment.environment_id,
            deleted=False,
        ).all()
    }

    def _replace(match):
        var_name = match.group(1)
        ec = env_configs.get(var_name)
        if ec is None:
            raise TemplateResolutionError(
                f"Shared environment variable '{var_name}' not found in "
                f"environment '{application_environment.environment.slug}'"
            )
        if ec.secret:
            if reader is not None:
                if not ec.buildtime:
                    raise TemplateResolutionError(
                        f"Shared secret '{var_name}' is not marked as "
                        f"build-time. Enable 'Expose during Build' on the "
                        f"shared variable to reference it in a build-time "
                        f"config."
                    )
                if not ec.build_key_slug:
                    raise TemplateResolutionError(
                        f"Shared secret '{var_name}' has no build-time vault path."
                    )
                payload = reader.read(
                    ec.build_key_slug.split(":", 1)[1],
                    build=True,
                    secret=True,
                )
                return payload["data"][ec.name]
            raise TemplateResolutionError(
                f"Shared secret '{var_name}' cannot be interpolated into a "
                f"larger string. Use it as the entire value "
                f"(${{shared.{var_name}}}) to rename it via envconsul, or "
                f"subscribe to it directly."
            )
        return ec.value

    return SHARED_TEMPLATE_PATTERN.sub(_replace, value)
