"""Operator-configured Cabotage clusters for the cluster switcher.

Operators who run several Cabotage installs deploy the same list to each of
them as ``CABOTAGE_CLUSTERS``, a JSON array in switcher order::

    [{"name": "psfhosted", "url": "https://cabotage.us-east-2.psfhosted.computer",
      "region": "us-east-2", "color": "teal"},
     {"name": "pypi", "url": "https://cabotage.us-east-2.pypi.io",
      "region": "us-east-2", "color": "amber"}]

``region`` is optional. ``color`` is optional and must be one of ``COLORS``;
omitted colors are assigned by position so every cluster stays distinct.
Each install finds itself in the list by request host. An install served
under a host the list does not name sets ``CABOTAGE_CLUSTER_NAME``.

With no list configured, the switcher still works from clusters a user adds
in their own browser; nothing is rendered until they add one.
"""

import json
import re
from dataclasses import asdict, dataclass
from typing import cast
from urllib.parse import urlsplit

from flask import Flask, current_app, request, url_for

# Identity hues, never status. Rendered by the [data-cluster-color] tokens in main.css.
COLORS = ("teal", "amber", "blue", "rose", "emerald", "sky", "orange", "slate")
# Favicon fills: an SVG data URI can't read CSS custom properties.
_FAVICON_FILL = {
    "teal": "#2bb3a3",
    "amber": "#e0a530",
    "blue": "#4f8fe6",
    "rose": "#e2647a",
    "emerald": "#3fb871",
    "sky": "#45b4e0",
    "orange": "#e8803f",
    "slate": "#8d8fa3",
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")


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
        fill = _FAVICON_FILL[self.color]
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            f"<rect x='1' y='1' width='14' height='14' rx='3.5' fill='{fill}'/></svg>"
        )
        return "data:image/svg+xml," + svg.replace("<", "%3C").replace(
            ">", "%3E"
        ).replace("#", "%23")


def _origin(name: str, url: object) -> str:
    parts = urlsplit(str(url))
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"CABOTAGE_CLUSTERS: {name!r} url {url!r} must be a bare http(s) origin "
            "like https://cabotage.example.com"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}"


def parse_clusters(raw: object) -> tuple[Cluster, ...]:
    """Parse ``CABOTAGE_CLUSTERS``; raise on anything ambiguous."""
    if not raw:
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CABOTAGE_CLUSTERS is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise TypeError("CABOTAGE_CLUSTERS must be a JSON array of clusters")

    clusters: list[Cluster] = []
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
        color = entry.get("color") or COLORS[index % len(COLORS)]
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
    return tuple(clusters)


def validate_cluster_config(app: Flask) -> None:
    """Fail at startup on a cluster list the switcher can't render correctly."""
    clusters = parse_clusters(app.config.get("CLUSTERS"))
    own = cast(str | None, app.config.get("CLUSTER_NAME"))
    if own and own not in {cluster.name for cluster in clusters}:
        raise ValueError(f"CABOTAGE_CLUSTER_NAME={own!r} is not in CABOTAGE_CLUSTERS")


def current_cluster(clusters: tuple[Cluster, ...]) -> Cluster | None:
    own = current_app.config.get("CLUSTER_NAME")
    if own:
        return next((c for c in clusters if c.name == own), None)
    host = request.host.lower()
    return next((c for c in clusters if c.host == host), None)


def cluster_context() -> dict[str, object]:
    """Template context for the switcher: the configured list and where we are."""
    clusters = parse_clusters(current_app.config.get("CLUSTERS"))
    here = current_cluster(clusters)
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
