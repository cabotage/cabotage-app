"""Operator-configured legal policies.

Each Cabotage deployment is run by a different operator, so the policies it
links to are configured per deployment, in one of two ways:

- ``CABOTAGE_LEGAL_PROFILE`` serves a bundled set of pages under ``/legal/``.
  A profile is a directory of templates under ``main/legal/<profile>/``.
- ``CABOTAGE_LEGAL_TERMS_URL``, ``CABOTAGE_LEGAL_PRIVACY_URL``,
  ``CABOTAGE_LEGAL_ACCEPTABLE_USE_URL`` (and optionally ``CABOTAGE_LEGAL_URL``
  for an index page) link to policies the operator hosts elsewhere.

With neither configured, ``/legal/`` is not served and no legal links render.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from flask import Flask, abort, current_app, render_template, url_for
from jinja2 import TemplateNotFound

LEGAL_PAGES = ("_nav", "index", "terms", "privacy", "acceptable_use")
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class LegalLink:
    label: str
    href: str
    icon: str
    summary: str
    endpoint: str | None = None

    @property
    def external(self) -> bool:
        return self.endpoint is None


# (config key, label, icon, summary) for externally hosted policies, in reading order.
_EXTERNAL_POLICIES = (
    ("LEGAL_TERMS_URL", "Terms of Use", "scroll-text", "Rules for using this instance"),
    (
        "LEGAL_PRIVACY_URL",
        "Privacy",
        "shield-check",
        "How this instance handles your data",
    ),
    ("LEGAL_ACCEPTABLE_USE_URL", "Acceptable Use", "scale", "What you can run here"),
)
_URL_KEYS = ("LEGAL_URL", *(key for key, *_ in _EXTERNAL_POLICIES))


def _template(profile: str, page: str) -> str:
    return f"main/legal/{profile}/{page}.html"


def validate_legal_config(app: Flask) -> None:
    """Fail at startup on legal configuration that can't render correctly."""
    profile = app.config.get("LEGAL_PROFILE")
    urls = {key: app.config.get(key) for key in _URL_KEYS if app.config.get(key)}

    if profile and urls:
        raise ValueError(
            "Set either CABOTAGE_LEGAL_PROFILE or the CABOTAGE_LEGAL_*_URL "
            "settings, not both"
        )

    for key, url in urls.items():
        parts = urlsplit(str(url))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"CABOTAGE_{key}={url!r} must be an absolute http(s) URL")

    if not profile:
        return
    if not isinstance(profile, str) or not _PROFILE_NAME.fullmatch(profile):
        raise ValueError(
            f"CABOTAGE_LEGAL_PROFILE={profile!r} is not a valid profile name "
            "(lowercase letters, digits, '-' and '_')"
        )
    missing = []
    for page in LEGAL_PAGES:
        try:
            _ = app.jinja_env.get_template(_template(profile, page))
        except TemplateNotFound:
            missing.append(_template(profile, page))
    if missing:
        raise ValueError(
            f"CABOTAGE_LEGAL_PROFILE={profile!r} is missing templates: "
            + ", ".join(missing)
        )


def _active_profile() -> str | None:
    profile: str | None = current_app.config.get("LEGAL_PROFILE")
    return profile or None


def _config_url(key: str) -> str | None:
    url: str | None = current_app.config.get(key)
    return url or None


def legal_nav() -> list[LegalLink]:
    """The configured policies in reading order; empty when none are configured."""
    profile = _active_profile()
    if profile is None:
        return [
            LegalLink(label=label, href=url, icon=icon, summary=summary)
            for key, label, icon, summary in _EXTERNAL_POLICIES
            if (url := _config_url(key))
        ]
    nav = current_app.jinja_env.get_template(_template(profile, "_nav"))
    pages: list[dict[str, str]] = vars(nav.module)["legal_nav"]
    return [
        LegalLink(
            label=page["label"],
            href=url_for(page["endpoint"]),
            icon=page["icon"],
            summary=page["summary"],
            endpoint=page["endpoint"],
        )
        for page in pages
    ]


def legal_home() -> LegalLink | None:
    """Where the footer's "Legal" link points, if anywhere."""
    if _active_profile() is not None:
        return LegalLink("Legal", url_for("main.legal"), "scale", "", "main.legal")
    home = _config_url("LEGAL_URL")
    return LegalLink("Legal", home, "scale", "") if home else None


def render_legal_page(page: str) -> str:
    profile = _active_profile()
    if profile is None:
        abort(404)
    return render_template(_template(profile, page))
