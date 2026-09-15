from typing import cast


def assume_not_none[T](val: T | None, /, *, because: str) -> T:
    return cast("T", val)
