"""Strict Mimir (Prometheus-compatible) HTTP client.

``cabotage.server.user.views`` has long-standing lenient wrappers
(``_mimir_connection``, ``_query_mimir_range``, ``_query_mimir_instant``)
used by dozens of observability callers that treat a missing/misbehaving
Mimir as "no data available" and swallow every error. This module is for
callers — namely the cost collector — that need to know definitively
whether a query succeeded, so they raise :class:`MimirError` instead.
"""

from typing import TypedDict

import requests
from flask import current_app


def _raise_for_status(resp: requests.Response, **_) -> None:
    resp.raise_for_status()


mimir_session = requests.Session()
mimir_session.hooks["response"] = [_raise_for_status]


class MimirError(Exception):
    """Raised when a Mimir query cannot be completed."""


class MimirNotConfiguredError(MimirError):
    """Raised when ``MIMIR_URL`` is unset, as opposed to a failed query."""


class MimirSeries(TypedDict, total=False):
    """One series from a Mimir/Prometheus ``query``/``query_range`` result."""

    metric: dict[str, str]
    values: list[list[float | str]]
    value: list[float | str]


class _MimirResultPayload(TypedDict):
    result: list[MimirSeries]


class _MimirResponse(TypedDict, total=False):
    status: str
    data: _MimirResultPayload


def mimir_connection(
    tenant_id: str | None = None,
) -> tuple[str, bool | str, dict[str, str], float]:
    """Return ``(mimir_url, verify, headers, timeout)`` for Mimir requests.

    If *tenant_id* is given it is used verbatim as the ``X-Scope-OrgID``
    header value; otherwise the configured ``MIMIR_TENANT_ID`` is used.
    Raises :class:`MimirNotConfiguredError` if Mimir is not configured.
    """
    mimir_url: str | None = current_app.config.get("MIMIR_URL")
    if not mimir_url:
        raise MimirNotConfiguredError("MIMIR_URL is not configured")

    verify_config: bool | str | None = current_app.config.get("MIMIR_VERIFY")
    verify: bool | str
    if verify_config is not None:
        if isinstance(verify_config, str) and verify_config.lower() == "false":
            verify = False
        else:
            verify = verify_config
    else:
        verify = True

    headers: dict[str, str] = {}
    # MetaFlaskEnv parses all-digit env values to int; requests rejects
    # non-str header values, so coerce.
    effective_tenant = (
        tenant_id
        if tenant_id is not None
        else current_app.config.get("MIMIR_TENANT_ID")
    )
    if effective_tenant:
        headers["X-Scope-OrgID"] = str(effective_tenant)

    timeout: float = current_app.config.get("MIMIR_TIMEOUT", 10)
    return mimir_url, verify, headers, timeout


def _get(
    endpoint: str, params: dict[str, str | int], tenant_id: str | None
) -> list[MimirSeries]:
    mimir_url, verify, headers, timeout = mimir_connection(tenant_id=tenant_id)
    try:
        resp = mimir_session.get(
            f"{mimir_url}/prometheus/api/v1/{endpoint}",
            params=params,
            headers=headers,
            verify=verify,
            timeout=timeout,
        )
    # requests.RequestException subclasses OSError; a bare OSError is raised
    # when MIMIR_VERIFY names a CA bundle path that does not exist.
    except OSError as exc:
        raise MimirError(f"Mimir {endpoint} request failed: {exc}") from exc
    try:
        data: _MimirResponse = resp.json()
    except ValueError as exc:
        raise MimirError(f"Mimir {endpoint} returned invalid JSON: {exc}") from exc

    query = params["query"]
    if not isinstance(data, dict) or data.get("status") != "success":
        status = data.get("status") if isinstance(data, dict) else None
        raise MimirError(
            f"Mimir {endpoint} returned status={status!r} for query={query!r}"
        )
    payload = data.get("data")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        raise MimirError(
            f"Mimir {endpoint} returned malformed payload (missing data.result) "
            f"for query={query!r}"
        )
    return result


def query_range(
    query: str,
    start: int,
    end: int,
    step: int,
    tenant_id: str | None = None,
) -> list[MimirSeries]:
    """Query Mimir's Prometheus-compatible ``query_range`` endpoint.

    Returns the parsed ``data.result`` list. Raises :class:`MimirError` on
    any configuration, network, HTTP, malformed-payload, or
    non-success-status failure.
    """
    params: dict[str, str | int] = {
        "query": query,
        "start": start,
        "end": end,
        "step": step,
    }
    return _get("query_range", params, tenant_id)


def query_instant(
    query: str,
    tenant_id: str | None = None,
    time: int | None = None,
) -> list[MimirSeries]:
    """Query Mimir's Prometheus-compatible instant ``query`` endpoint.

    If *time* is given (a unix timestamp) the query is evaluated at that
    point instead of "now".

    Returns the parsed ``data.result`` list. Raises :class:`MimirError` on
    any configuration, network, HTTP, malformed-payload, or
    non-success-status failure.
    """
    params: dict[str, str | int] = {"query": query}
    if time is not None:
        params["time"] = time
    return _get("query", params, tenant_id)
