from __future__ import annotations

from typing import (
    TypedDict,
    Literal,
    ReadOnly,
    NotRequired,
)


class Metadata(TypedDict):
    name: str
    labels: dict[str, str | None]


class Subject(TypedDict):
    organizations: list[str]


class PrivateKey(TypedDict):
    algorithm: str
    size: int


class IssuerRef(TypedDict):
    name: str
    kind: str
    group: str


class RedisCertificateSpec(TypedDict):
    secretName: str
    duration: str
    renewBefore: str
    subject: Subject
    commonName: str
    isCA: bool
    privateKey: PrivateKey
    usages: list[str]
    dnsNames: list[str]
    issuerRef: IssuerRef


class _Common(TypedDict):
    apiVersion: str
    kind: ReadOnly[str]
    metadata: Metadata


class Resource(TypedDict, total=False):
    cpu: str
    memory: str
    storage: str


class Resources(TypedDict, total=False):
    requests: Resource
    limits: Resource


class RedisCertificate(_Common):
    kind: Literal["Certificate"]
    spec: RedisCertificateSpec


class Secret(TypedDict):
    optional: bool
    secretName: str


class TLS(TypedDict):
    secret: Secret


class RedisSecret(TypedDict):
    key: str
    name: str


class KubernetesConfig(TypedDict):
    image: str
    imagePullPolicy: str
    redisSecret: RedisSecret
    resources: Resources


class PodSecurityContext(TypedDict):
    fsGroup: int
    runAsUser: int


class VolumeClaimTemplateSpec(TypedDict):
    accessModes: list[str]
    resources: Resources


class VolumeClaimTemplate(TypedDict):
    spec: VolumeClaimTemplateSpec


class RedisStorage(TypedDict):
    volumeClaimTemplate: VolumeClaimTemplate


NodeSelector = TypedDict("NodeSelector", {"cabotage.dev/node-pool": str})


class Toleration(TypedDict):
    key: Literal["cabotage.dev/node-pool"]
    operator: str
    value: str
    effect: str


class Affinity(TypedDict, total=False):
    nodeSelector: NodeSelector | None
    tolerations: list[Toleration] | None


class RedisSpec(Affinity):
    TLS: TLS
    kubernetesConfig: KubernetesConfig
    podSecurityContext: PodSecurityContext
    storage: RedisStorage


class RedisStandalone(_Common):
    kind: Literal["Redis"]
    spec: RedisSpec


class LabelSelector(TypedDict):
    matchLabels: dict[str, str]


class PodAffinityTerm(TypedDict):
    labelSelector: LabelSelector
    topologyKey: str


class PreferredPodAntiAffinityTerm(TypedDict):
    weight: int
    podAffinityTerm: PodAffinityTerm


class PodAntiAffinity(TypedDict):
    preferredDuringSchedulingIgnoredDuringExecution: list[PreferredPodAntiAffinityTerm]
    requiredDuringSchedulingIgnoredDuringExecution: NotRequired[list[PodAffinityTerm]]


class RedisRoleAffinity(TypedDict):
    podAntiAffinity: PodAntiAffinity


class _LeaderFollower(TypedDict):
    replicas: int
    affinity: NotRequired[RedisRoleAffinity]


class RedisLeader(_LeaderFollower): ...


class RedisFollower(_LeaderFollower): ...


class RedisClusterSpec(RedisSpec):
    clusterSize: int
    clusterVersion: str
    persistenceEnabled: bool
    redisLeader: RedisLeader
    redisFollower: RedisFollower


class RedisCluster(_Common):
    kind: Literal["RedisCluster"]
    spec: RedisClusterSpec


BackingServicePodAnnotations = TypedDict(
    "BackingServicePodAnnotations", {"karpenter.sh/do-not-disrupt": NotRequired[str]}
)


class InheritedMetadata(TypedDict):
    labels: dict[str, str | None]
    annotations: NotRequired[BackingServicePodAnnotations]


class CnpgCertificate(TypedDict):
    serverTLSSecret: str
    serverCASecret: str


class CngStorage(TypedDict):
    size: str


class PostgresqlParam(TypedDict):
    parameters: dict[str, str]


class CnpgAffinity(Affinity, total=False):
    enablePodAntiAffinity: bool
    podAntiAffinityType: str
    topologyKey: Literal["kubernetes.io/hostname"]
    additionalPodAntiAffinity: PodAntiAffinity


class PluginParams(TypedDict):
    barmanObjectName: str
    serverName: str


class Plugin(TypedDict):
    name: str
    parameters: PluginParams
    isWALArchiver: NotRequired[bool]


class CnpgSpec(TypedDict):
    instances: int
    imageName: str
    inheritedMetadata: InheritedMetadata
    certificates: CnpgCertificate
    postgresql: PostgresqlParam
    resources: Resources
    storage: CngStorage
    affinity: NotRequired[CnpgAffinity]
    serviceAccountName: NotRequired[str]
    plugins: NotRequired[list[Plugin]]


class CnpgCluster(_Common):
    kind: Literal["Cluster"]
    spec: CnpgSpec


class BackupSettings(TypedDict):
    provider: str
    bucket: str
    irsa_role_arn: str
    path_prefix: str
    plugin_name: str
    retention_policy: str
    schedule: str
    service_account_name: str
    rustfs_endpoint: str
    rustfs_ca_secret_name: str
    rustfs_secret_name: str
    rustfs_source_secret_name: str
    rustfs_source_secret_namespace: str


type HealthStatus = Literal["provisioning", "ready", "error"]


class PostgresClusterStatus(TypedDict):
    conditions: str
    readyInstances: int | None
    reason: str


class Compression(TypedDict):
    compression: str


class AccessKeyId(TypedDict):
    name: str
    key: Literal["access-key-id"]


class SecretAccessKey(TypedDict):
    name: str
    key: Literal["secret-key"]


class Region(TypedDict):
    name: str
    key: Literal["region"]


class S3Credentials(TypedDict, total=False):
    inheritFromIAMRole: bool
    accessKeyId: AccessKeyId
    secretAccessKey: SecretAccessKey
    region: Region


class EndpointCA(TypedDict):
    name: str
    key: str


class PostgresConfig(TypedDict):
    destinationPath: str
    data: Compression
    wal: Compression
    s3Credentials: NotRequired[S3Credentials]
    endpointURL: NotRequired[str]
    endpointCA: NotRequired[EndpointCA]


class PostgresSpec(TypedDict):
    retentionPolicy: str
    configuration: PostgresConfig


class PostgresObjectStore(_Common):
    kind: Literal["ObjectStore"]
    spec: PostgresSpec


class ScheduledBackupCluster(TypedDict):
    name: str


class PluginConfiguration(TypedDict):
    name: str


class ScheduledBackupSpec(TypedDict):
    schedule: str
    backupOwnerReference: str
    cluster: ScheduledBackupCluster
    method: str
    pluginConfiguration: PluginConfiguration
    target: str
    immediate: NotRequired[bool]


class ScheduledBackup(_Common):
    kind: Literal["ScheduledBackup"]
    spec: ScheduledBackupSpec
