"""Operator-configured Cabotage clusters for the cluster switcher.

Operators who run several Cabotage installs deploy the same list to each of
them as ``CABOTAGE_CLUSTERS``, a JSON array in switcher order::

    [{"name": "psfhosted", "url": "https://cabotage.us-east-2.psfhosted.computer",
      "region": "us-east-2", "color": "teal"},
     {"name": "pypi", "url": "https://cabotage.us-east-2.pypi.io",
      "region": "us-east-2", "color": "amber"}]

``region`` is optional. ``color`` is optional and must be one of ``COLORS``,
the theme accent presets; it becomes that install's default accent, so the
whole UI takes the cluster's color unless a user picks another accent.
Omitted colors take the first preset no other cluster uses, so clusters stay
distinct until all eight are taken.
Each install finds itself in the list by request host. An install served
under a host the list does not name sets ``CABOTAGE_CLUSTER_NAME``.

Everyone sees which cluster they are on. The list of other clusters and the
jump palette go only to ``CABOTAGE_CLUSTER_SWITCHER_ACCESS``: ``admins``
(the default) or every signed-in user (``users``). Those users can also add
clusters of their own, kept in their browser.
"""

import json
import re
from dataclasses import asdict, dataclass
from typing import cast
from urllib.parse import urlsplit

from flask import Flask, current_app, request, url_for
from flask_login import current_user

# The theme accent presets in _base.html, in the order of the accent picker.
COLORS = ("purple", "blue", "teal", "rose", "amber", "emerald", "white", "dark")
# Favicon fills: each preset's dark-theme accent text color (ACCENT_PRESETS in
# _base.html), since an SVG data URI can't read CSS custom properties.
FAVICON_FILL = {
    "purple": "#a78bfa",
    "blue": "#60a5fa",
    "teal": "#2dd4bf",
    "rose": "#fb7185",
    "amber": "#fbbf24",
    "emerald": "#34d399",
    "white": "#e8e8e8",
    "dark": "#888888",
}
ACCESS_LEVELS = ("admins", "users")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class Cluster:
    name: str
    url: str
    region: str | None
    color: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.lower()

    @property
    def favicon(self) -> str:
        fill = FAVICON_FILL[self.color]
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            f"<rect x='1' y='1' width='14' height='14' rx='3.5' fill='{fill}'/></svg>"
        )
        return "data:image/svg+xml," + svg.replace("<", "%3C").replace(
            ">", "%3E"
        ).replace("#", "%23")


def _origin(name: str, url: object) -> str:
    """Canonical origin, spelled the way browsers and ``request.host`` spell it."""
    parts = urlsplit(str(url))
    try:
        port = parts.port
    except ValueError:  # not a number, or out of range
        port = 0
    hostname = parts.hostname or ""
    if (
        port == 0
        or parts.scheme not in ("http", "https")
        or not _HOSTNAME.fullmatch(hostname)
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"CABOTAGE_CLUSTERS: {name!r} url {url!r} must be a bare http(s) origin "
            "like https://cabotage.example.com"
        )
    if port is None or port == _DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{hostname}"
    return f"{parts.scheme}://{hostname}:{port}"


def parse_clusters(raw: object) -> list[Cluster]:
    """Parse ``CABOTAGE_CLUSTERS``; raise on anything ambiguous."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CABOTAGE_CLUSTERS is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise TypeError("CABOTAGE_CLUSTERS must be a JSON array of clusters")

    clusters: list[Cluster] = []
    # Explicit colors first, so a default never repeats one an operator chose.
    taken = {
        item.get("color")
        for item in cast(list[object], raw)
        if isinstance(item, dict) and isinstance(item.get("color"), str)
    }
    spare = [color for color in COLORS if color not in taken]
    for index, item in enumerate(cast(list[object], raw)):
        if not isinstance(item, dict):
            raise TypeError(f"CABOTAGE_CLUSTERS[{index}] must be an object")
        entry = cast(dict[str, object], item)
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(
                f"CABOTAGE_CLUSTERS[{index}] name {name!r} must be 1-40 letters, "
                "digits, '.', '_' or '-'"
            )
        if "url" not in entry:
            raise ValueError(f"CABOTAGE_CLUSTERS: {name!r} is missing url")
        color = entry.get("color")
        if not color:
            color = spare.pop(0) if spare else COLORS[index % len(COLORS)]
        if not isinstance(color, str) or color not in COLORS:
            raise ValueError(
                f"CABOTAGE_CLUSTERS: {name!r} color {color!r} must be one of "
                + ", ".join(COLORS)
            )
        region = entry.get("region")
        if region is not None and not isinstance(region, str):
            raise ValueError(f"CABOTAGE_CLUSTERS: {name!r} region must be a string")
        clusters.append(
            Cluster(name, _origin(name, entry["url"]), region or None, color)
        )

    names: set[str] = set()
    hosts: set[str] = set()
    for cluster in clusters:
        if cluster.name.lower() in names:
            raise ValueError(f"CABOTAGE_CLUSTERS lists name {cluster.name!r} twice")
        if cluster.host in hosts:
            raise ValueError(f"CABOTAGE_CLUSTERS lists host {cluster.host!r} twice")
        names.add(cluster.name.lower())
        hosts.add(cluster.host)
    return clusters


def validate_cluster_config(app: Flask) -> None:
    """Fail at startup on a cluster list the switcher can't render correctly."""
    clusters = parse_clusters(app.config.get("CLUSTERS"))
    own = cast(str | None, app.config.get("CLUSTER_NAME"))
    if own and own not in {cluster.name for cluster in clusters}:
        raise ValueError(f"CABOTAGE_CLUSTER_NAME={own!r} is not in CABOTAGE_CLUSTERS")
    access = cast(str | None, app.config.get("CLUSTER_SWITCHER_ACCESS"))
    if access not in ACCESS_LEVELS:
        raise ValueError(
            f"CABOTAGE_CLUSTER_SWITCHER_ACCESS={access!r} must be one of "
            + ", ".join(ACCESS_LEVELS)
        )


def current_cluster(clusters: list[Cluster]) -> Cluster | None:
    own = current_app.config.get("CLUSTER_NAME")
    if own:
        return next((c for c in clusters if c.name == own), None)
    host = request.host.lower()
    return next((c for c in clusters if c.host == host), None)


def can_switch() -> bool:
    """Whether the current user may see other clusters and open the palette."""
    if not current_user.is_authenticated:
        return False
    if current_app.config.get("CLUSTER_SWITCHER_ACCESS") == "users":
        return True
    return bool(getattr(current_user, "admin", False))


def cluster_context() -> dict[str, object]:
    """Template context: where we are for everyone, the switcher for the allowed."""
    clusters = parse_clusters(current_app.config.get("CLUSTERS"))
    here = current_cluster(clusters)
    if not can_switch():
        return {"current_cluster": here, "cluster_switcher": None}
    return {
        "current_cluster": here,
        "cluster_switcher": {
            "current": here.name if here else None,
            "clusters": [asdict(cluster) for cluster in clusters],
            "colors": COLORS,
            # Pages that exist on every cluster; a jump keeps the longest one
            # the current path starts with.
            "shared": [
                url_for("user.projects"),
                url_for("user.organizations"),
                url_for("user.guide"),
            ],
        },
    }
