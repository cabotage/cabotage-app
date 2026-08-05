from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

    from .config import ConfigDict

    class TypedFlask(Flask):
        # HACK: "fix" app.config.get, runtime beahvior is unchanged
        config: ConfigDict  # type: ignore[bad-override-mutable-attribute]
