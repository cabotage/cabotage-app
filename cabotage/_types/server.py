from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict, Literal, Required

    from flask import Flask

    from .config import ConfigDict

    class TypedFlask(Flask):
        # HACK: "fix" app.config.get, runtime beahvior is unchanged
        config: ConfigDict  # type: ignore[bad-override-mutable-attribute]

    class Account(TypedDict):
        id: int
        login: str
        type: str

    class Installation(TypedDict):
        id: int
        account: Account
        repository_selection: Literal["all", "selected"]

    class Repository(TypedDict, total=False):
        id: int
        full_name: str
        private: bool

    class RepositoryOption(TypedDict):
        id: int | None
        name: str | None
        private: bool

    class RepositoryMetadata(TypedDict):
        id: int | None
        full_name: str
        private: bool

    class Payload(TypedDict, total=False):
        organization_id: str
        user_id: str
        installation_id: Required[str]
