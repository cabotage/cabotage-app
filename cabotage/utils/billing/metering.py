"""Usage metering — bottom-up from service to org.

Hierarchy:
    service (ApplicationEnvironment) -> project -> org

Each level aggregates from below. When real K8s collectors are wired up,
only ``_collect_service_usage`` needs to change — everything above it
stays the same.

For now, service-level data is estimated from pod_class resource requests
× replica counts × billing period hours.  Meters that can't be derived
from the model (egress, build minutes, storage) use deterministic mock
values seeded from the service name so numbers are stable across reloads.
"""

import random
from typing import Any

from cabotage.server.models.auth import Organization
from cabotage.server.models.projects import (
    ApplicationEnvironment,
    DEFAULT_POD_CLASS,
    pod_classes,
)

# Billing period length in hours (approximation for current-period estimates)
PERIOD_HOURS = 720  # ~30 days


def _parse_cpu(cpu_str: str) -> float:
    """Convert K8s CPU string to vCPU count. '250m' -> 0.25, '1' -> 1.0."""
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1]) / 1000
    return float(cpu_str)


def _parse_memory_gi(mem_str: str) -> float:
    """Convert K8s memory string to GB. '512Mi' -> 0.5, '1Gi' -> 1.0."""
    if mem_str.endswith("Gi"):
        return float(mem_str[:-2])
    if mem_str.endswith("Mi"):
        return float(mem_str[:-2]) / 1024
    return 0.0


def collect_service_usage(app_env: ApplicationEnvironment) -> dict[str, float]:
    """Collect usage for a single service (ApplicationEnvironment).

    Derives vCPU and RAM from pod_class × replica count × period hours.
    Other meters (egress, builds, storage) use deterministic mock values
    until real collectors are implemented.

    TODO: Replace mock values with real data from:
      - Egress: K8s network metrics / cloud flow logs
      - Build minutes: Image.build_started_at / build_completed_at deltas
      - Block storage: PVC sizes from K8s API
      - DB storage: pg_database_size queries
      - Postgres/Redis hours: Resource model instance counts
      - Tailscale: node count from Tailscale API
    """
    process_counts = app_env.process_counts or {}
    process_pod_classes = app_env.process_pod_classes or {}

    # --- vCPU and RAM from actual model data ---
    total_vcpu_hours = 0.0
    total_ram_gb_hours = 0.0

    for process_name, replica_count in process_counts.items():
        if not replica_count or replica_count <= 0:
            continue
        pod_class_name = process_pod_classes.get(process_name, DEFAULT_POD_CLASS)
        pod_class = pod_classes.get(pod_class_name, pod_classes[DEFAULT_POD_CLASS])

        cpu_request = _parse_cpu(pod_class["cpu"]["requests"])
        mem_request = _parse_memory_gi(pod_class["memory"]["requests"])

        total_vcpu_hours += cpu_request * replica_count * PERIOD_HOURS
        total_ram_gb_hours += mem_request * replica_count * PERIOD_HOURS

    # --- Mock data for meters we can't derive yet ---
    # Seed from service identity for deterministic values
    app = app_env.application
    project = app.project
    seed_str = f"{project.organization.slug}:{project.slug}:{app.slug}"
    rng = random.Random(hash(seed_str))

    # Scale mock values by the service's compute footprint
    compute_weight = max(total_vcpu_hours / PERIOD_HOURS, 0.1)

    usage: dict[str, float] = {
        "vcpu_hours": round(total_vcpu_hours, 2),
        "ram_gb_hours": round(total_ram_gb_hours, 2),
        "egress_gb": round(rng.uniform(1, 15) * compute_weight, 2),
        "build_minutes": round(rng.uniform(20, 120) * compute_weight, 1),
        "block_storage_gb": round(rng.uniform(0, 8) * compute_weight, 2),
        "object_storage_gb": 0,
        "db_storage_gb": 0,
        "postgres_gb_hours": round(rng.uniform(0, 30) * compute_weight, 2),
        "redis_gb_hours": 0,
        "tailscale_nodes": 0,
    }

    return usage


# ---------------------------------------------------------------------------
# Aggregation: project and org levels
# ---------------------------------------------------------------------------

def _sum_usage(items: list[dict[str, float]]) -> dict[str, float]:
    """Sum usage dicts, combining all meter keys."""
    totals: dict[str, float] = {}
    for item in items:
        for key, value in item.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    return {k: round(v, 2) for k, v in totals.items()}


def collect_project_usage(project) -> dict[str, Any]:
    """Aggregate usage across all services in a project."""
    services = []
    for app in project.project_applications:
        if getattr(app, "deleted_at", None) is not None:
            continue
        for app_env in app.application_environments:
            usage = collect_service_usage(app_env)
            services.append({
                "service": app.name,
                "environment": app_env.environment.name if app_env.environment else "default",
                "usage": usage,
            })

    return {
        "project": project.name,
        "services": services,
        "totals": _sum_usage([s["usage"] for s in services]),
    }


def collect_environment_usage(environment) -> dict[str, Any]:
    """Aggregate usage across all services in a single environment."""
    services = []
    for app_env in environment.active_application_environments:
        app = app_env.application
        if getattr(app, "deleted_at", None) is not None:
            continue
        usage = collect_service_usage(app_env)
        services.append({
            "service": app.name,
            "usage": usage,
        })

    return {
        "environment": environment.name,
        "services": services,
        "totals": _sum_usage([s["usage"] for s in services]),
    }


def collect_org_usage(org: Organization) -> dict[str, Any]:
    """Aggregate usage across all projects in an org.

    Returns structure:
    {
        "projects": [
            {
                "project": "My API",
                "services": [{"service": "Web", "environment": "Production", "usage": {...}}, ...],
                "totals": {...},
            },
            ...
        ],
        "totals": {...},  # org-wide totals
    }
    """
    projects = []
    for project in org.projects:
        if getattr(project, "deleted_at", None) is not None:
            continue
        project_data = collect_project_usage(project)
        if project_data["services"]:
            projects.append(project_data)

    return {
        "projects": projects,
        "totals": _sum_usage([p["totals"] for p in projects]),
    }


def get_service_usage_list(org: Organization) -> list[dict]:
    """Flat list of per-service usage with cost, for the billing UI.

    Each entry: {service, project, environment, vcpu_hours, ..., cost}
    """
    from cabotage.utils.billing._products import METERS

    org_data = collect_org_usage(org)
    result = []

    for project_data in org_data["projects"]:
        for svc in project_data["services"]:
            entry = {
                "service": svc["service"],
                "project": project_data["project"],
                "environment": svc["environment"],
            }
            cost = 0.0
            for meter_key, value in svc["usage"].items():
                meter = METERS.get(meter_key)
                if meter and value > 0:
                    rate = float(meter.unit_amount_decimal) / 100
                    cost += value * rate
                entry[meter_key] = value
            entry["cost"] = round(cost, 2)
            result.append(entry)

    # Sort by cost descending
    result.sort(key=lambda x: x["cost"], reverse=True)
    return result
