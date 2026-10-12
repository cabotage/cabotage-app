from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict

    # Generic recursive JSON value, used for the dashboard payload (and its
    # body envelope) whose shape is Grafana's own dashboard JSON schema and
    # too heterogeneous to model as a fixed TypedDict.
    JSONValue = (
        str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
    )
    JSONObject = dict[str, "JSONValue"]

    class GrafanaDatasourceJsonData(TypedDict, total=False):
        httpHeaderName1: str
        tlsAuthWithCACert: bool

    class GrafanaDatasourceSecureJsonData(TypedDict, total=False):
        httpHeaderValue1: str
        tlsCACert: str

    class GrafanaDatasourcePayload(TypedDict):
        uid: str
        name: str
        type: str
        url: str
        access: str
        isDefault: bool
        readOnly: bool
        editable: bool
        jsonData: GrafanaDatasourceJsonData
        secureJsonData: GrafanaDatasourceSecureJsonData

    class GrafanaOrgLookupResponse(TypedDict):
        id: int
        name: str

    class GrafanaOrgCreateResponse(TypedDict):
        orgId: int

    class GrafanaUserLookupResponse(TypedDict):
        id: int

    class GrafanaOrgMembership(TypedDict):
        userId: int
        login: str
        role: str
