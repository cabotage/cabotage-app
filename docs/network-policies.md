# Network Policies

Cabotage can apply Kubernetes NetworkPolicies to tenant namespaces, enforcing
default-deny ingress and restricted egress at the network level.

## Enabling

Set the config/environment variable:

```
NETWORK_POLICIES_ENABLED=true
```

When disabled (the default), namespaces are still labeled with
`resident-namespace.cabotage.io: "true"` but no NetworkPolicy resources are
created. This lets you prepare Vault ingress rules before flipping the switch.

## What gets applied

Four NetworkPolicy resources are created (or updated) in each tenant namespace
on every deploy:

### `default-deny-ingress`

Denies all ingress to pods in the namespace unless explicitly allowed by
another policy.

### `allow-ingress-from-traefik`

Allows ingress from the `traefik` namespace on port 8000 (ghostunnel TLS
proxy port).

### `allow-intra-namespace`

Allows pods within the namespace to communicate with each other freely.

### `restrict-egress`

Default-deny egress with the following exceptions:

| Destination | Namespace | Ports | Reason |
|---|---|---|---|
| CoreDNS | `kube-system` | 53/UDP, 53/TCP | Name resolution |
| Vault | `cabotage` (app=vault) | 443/TCP, 8200/TCP | envconsul reads secrets at runtime |
| Consul | `cabotage` (app=consul) | 8443/TCP | envconsul reads config at runtime |
| ClickHouse | `clickhouse` | 8443/TCP, 9440/TCP | Legacy service provider |
| Redis | `redis` | 6379/TCP | Legacy service provider |
| Elasticsearch | `elasticsearch` | 9200/TCP | Legacy service provider |
| PostgreSQL | `postgres` | 5432/TCP | Legacy service provider |
| Intra-namespace | same namespace | all | Pod-to-pod communication |
| Internet | 0.0.0.0/0 (excluding RFC 1918/CGN) | all TCP; UDP 123, 443, 8443 | Outbound HTTP, NTP, QUIC |

Cluster-internal traffic to any namespace or service not listed above is
blocked.

## Namespace label

All tenant namespaces are labeled with:

```yaml
resident-namespace.cabotage.io: "true"
```

This label is applied unconditionally (regardless of `NETWORK_POLICIES_ENABLED`)
on both new and existing namespaces. It is required by the Vault ingress
NetworkPolicy in the `cabotage` namespace to allow inbound connections from
tenant pods.

## Where policies are applied

- **Standard deploys** — `deploy.py:fetch_namespace()` labels the namespace,
  then `ensure_network_policies()` is called before any other resources are
  created.
- **Branch deploys** — `branch_deploy.py:_precreate_ingresses()` labels the
  namespace and applies policies before creating ingress resources.

Policies are applied idempotently (create-or-patch), so re-deploys are safe.

## Rollout checklist

1. Verify the cluster CNI enforces NetworkPolicies (Calico, Cilium, etc. —
   plain flannel does not).
2. Confirm namespace names match: `traefik`, `kube-system`, `cabotage`,
   `clickhouse`, `redis`, `elasticsearch`, `postgres`.
3. Deploy cabotage-app with `NETWORK_POLICIES_ENABLED` **unset** first to
   roll out namespace labels without policy enforcement.
4. Ensure Vault's ingress policy selecting on
   `resident-namespace.cabotage.io: "true"` is in place.
5. Set `NETWORK_POLICIES_ENABLED=true` and deploy a low-risk tenant first.
6. Watch for connection timeouts — if something breaks:
   ```
   kubectl delete networkpolicy --all -n <namespace>
   ```
