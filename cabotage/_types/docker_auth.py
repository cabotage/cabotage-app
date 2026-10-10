from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal, TypedDict

    class JWK(TypedDict):
        kty: Literal["EC"]
        crv: Literal["P-256"]
        kid: str
        alg: Literal["ES256"]
        use: Literal["sig"]
        x: str
        y: str

    class Access(TypedDict):
        type: str
        name: str
        actions: list[str]
