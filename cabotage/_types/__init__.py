from typing import cast, Final


def assume_not_none[T](val: T | None, /, *, because: str) -> T:
    return cast("T", val)


K8S_OBJECT_HAS_STATUS: Final = "Kubernetes objects have status"
K8S_POD_HAS_START_TIME: Final = "A running pod has a start time"
