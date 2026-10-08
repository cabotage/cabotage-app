from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import TypedDict

    class ConsulEntry(TypedDict):
        Key: str
        Value: bytes | None
        Flags: int
        LockIndex: int
        CreateIndex: int
        ModifyIndex: int

    type ConsulResponse = tuple[str, ConsulEntry | None]
