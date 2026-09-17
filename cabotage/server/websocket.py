from typing import Concatenate, TYPE_CHECKING
from functools import wraps
from werkzeug.exceptions import HTTPException

if TYPE_CHECKING:
    from collections.abc import Callable

    from simple_websocket import Server

    type WebSocketView[**P] = Callable[Concatenate[Server, P], None]


def close_on_abort[**P](route: WebSocketView[P]) -> WebSocketView[P]:
    @wraps(route)
    def inner(ws: Server, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            route(ws, *args, **kwargs)
        except HTTPException as e:
            # abort raises subclasses of `HTTPException`
            ws.close(message=e.description)

    return inner
