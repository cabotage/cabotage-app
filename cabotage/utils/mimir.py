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


class MimirError(Exception):
    """Raised when a Mimir query cannot be completed."""


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
    Raises :class:`MimirError` if Mimir is not configured.
    """
    mimir_url: str | None = current_app.config.get("MIMIR_URL")
    if not mimir_url:
        raise MimirError("MIMIR_URL is not configured")

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
    effective_tenant: str | None = (
        tenant_id
        if tenant_id is not None
        else current_app.config.get("MIMIR_TENANT_ID")
    )
    if effective_tenant:
        headers["X-Scope-OrgID"] = effective_tenant

    timeout: float = current_app.config.get("MIMIR_TIMEOUT", 10)
    return mimir_url, verify, headers, timeout


def _extract_result(data: _MimirResponse, query: str, label: str) -> list[MimirSeries]:
    if data.get("status") != "success":
        raise MimirError(
            f"Mimir {label} returned status={data.get('status')!r} for query={query!r}"
        )
    payload = data.get("data")
    return payload["result"] if payload is not None else []


def query_range(
    query: str,
    start: int,
    end: int,
    step: int,
    tenant_id: str | None = None,
) -> list[MimirSeries]:
    """Query Mimir's Prometheus-compatible ``query_range`` endpoint.

    Returns the parsed ``data.result`` list. Raises :class:`MimirError` on
    any configuration, network, HTTP, or non-success-status failure.
    """
    mimir_url, verify, headers, timeout = mimir_connection(tenant_id=tenant_id)
    try:
        resp = requests.get(
            f"{mimir_url}/prometheus/api/v1/query_range",
            params={
                "query": query,
                "start": start,
                "end": end,
                "step": step,
            },
            headers=headers,
            verify=verify,
            timeout=timeout,
        )
        resp.raise_for_status()
        data: _MimirResponse = resp.json()
    except requests.RequestException as exc:
        raise MimirError(f"Mimir query_range request failed: {exc}") from exc
    except ValueError as exc:
        raise MimirError(f"Mimir query_range returned invalid JSON: {exc}") from exc
    return _extract_result(data, query, "query_range")


def query_instant(
    query: str,
    tenant_id: str | None = None,
    time: int | None = None,
) -> list[MimirSeries]:
    """Query Mimir's Prometheus-compatible instant ``query`` endpoint.

    If *time* is given (a unix timestamp) the query is evaluated at that
    point instead of "now".

    Returns the parsed ``data.result`` list. Raises :class:`MimirError` on
    any configuration, network, HTTP, or non-success-status failure.
    """
    mimir_url, verify, headers, timeout = mimir_connection(tenant_id=tenant_id)
    params: dict[str, str | int] = {"query": query}
    if time is not None:
        params["time"] = time
    try:
        resp = requests.get(
            f"{mimir_url}/prometheus/api/v1/query",
            params=params,
            headers=headers,
            verify=verify,
            timeout=timeout,
        )
        resp.raise_for_status()
        data: _MimirResponse = resp.json()
    except requests.RequestException as exc:
        raise MimirError(f"Mimir query request failed: {exc}") from exc
    except ValueError as exc:
        raise MimirError(f"Mimir query returned invalid JSON: {exc}") from exc
    return _extract_result(data, query, "query")
