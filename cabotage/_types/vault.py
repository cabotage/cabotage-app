from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal, TypedDict

    type HashAlgorithm = Literal["sha2-224", "sha2-256", "sha2-384", "sha2-512"]
    type MarshalAlgorithm = Literal["asn1", "jws"]

    class TransitKeyVersion(TypedDict):
        creation_time: str
        name: str
        public_key: str

    class TransitKeyData(TypedDict):
        keys: dict[str, TransitKeyVersion]
        latest_version: int

    class TransitSignData(TypedDict):
        key_version: int
        signature: str

    class VaultResponse[T](TypedDict):
        request_id: str
        lease_id: str
        renewable: bool
        lease_duration: int
        data: T
        warnings: list[str] | None
        mount_type: Literal["kv", "transit"]

    type VaultSecretResponse = VaultResponse[dict[str, str]]
    type VaultTransitKeyResponse = VaultResponse[TransitKeyData]
    type VaultTransitSignResponse = VaultResponse[TransitSignData]
