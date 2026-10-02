from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict, Protocol
    from uuid import UUID

    from sqlalchemy.orm import Mapped

    from cabotage.server import Model
    from cabotage.server.models.projects import Configuration, IngressHost, IngressPath

    class ValueChange[T](TypedDict, total=False):
        old_value: T
        new_value: T

    class ProcessScaleChanges(TypedDict, total=False):
        process_count: ValueChange[int]
        pod_class: ValueChange[str]

    type ScaleChanges = dict[str, ProcessScaleChanges]

    class Diff(TypedDict):
        field: str
        old: str | None
        new: str | None

    class EntryLike(Protocol):
        id: int
        object_id: UUID | None
        object_tx_id: int | None

    class Versioned:
        transaction_id: Mapped[int]
        end_transaction_id: Mapped[int | None]
        operation_type: Mapped[int]

    class ConfigurationVersion(Configuration, Versioned): ...

    class IngressHostVersion(IngressHost, Versioned): ...

    class IngressPathVersion(IngressPath, Versioned): ...

    class GenericModelVersion(Model, Versioned):
        id: Mapped[UUID]

    type VersionKey = tuple[UUID | None, int | None]

    type VersionIndex[T: Versioned] = dict[VersionKey, T]
