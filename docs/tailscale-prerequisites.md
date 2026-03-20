# Tailscale Ingress: Cluster Prerequisites

Cabotage can deploy applications with ingresses accessible on an organization's
own Tailscale network (tailnet). This requires some one-time cluster-level setup
before organizations can configure their Tailscale integrations in the UI.

Cabotage manages the per-organization, per-namespace operator instances
automatically — including the operator Deployment, OAuth Secret,
ServiceAccounts (operator + proxies), namespace-scoped RBAC, and
ClusterRoleBinding subjects. The steps below cover what a **cluster
administrator** must provision beforehand.

## 1. Install the Tailscale CRDs

The Tailscale Kubernetes operator defines several Custom Resource Definitions.
Install them from the official Helm chart:

```bash
helm repo add tailscale https://pkgs.tailscale.com/helmcharts
helm repo update

# Pull the chart and extract CRDs
helm template tailscale-operator tailscale/tailscale-operator \
  --set oauth.clientId="placeholder" \
  --set oauth.clientSecret="placeholder" \
  | awk 'BEGIN{RS="---\n";ORS=""} /kind: CustomResourceDefinition/{print "---\n"$0}' \
  | kubectl apply -f -
```

Verify all 5 CRDs are installed:

```bash
kubectl get crds | grep tailscale
# connectors.tailscale.com
# dnsconfigs.tailscale.com
# proxyclasses.tailscale.com
# proxygroups.tailscale.com
# recorders.tailscale.com
```

## 2. Create the Tailscale IngressClass

```yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: tailscale
spec:
  controller: tailscale.com/ts-ingress
```

```bash
kubectl apply -f tailscale-ingressclass.yaml
```

## 3. Create the ClusterRole

The Tailscale operator needs cluster-wide permissions for IngressClasses,
Nodes, Services, Ingress status, Tailscale CRDs, and namespace-scoped RBAC
management (to create Roles for proxy pods). Extract this from the Helm chart
or create it manually:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: tailscale-operator
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["events", "services", "services/status"]
    verbs: ["create", "delete", "deletecollection", "get", "list", "patch", "update", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses", "ingresses/status"]
    verbs: ["create", "delete", "deletecollection", "get", "list", "patch", "update", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingressclasses"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["tailscale.com"]
    resources: ["connectors", "connectors/status", "proxyclasses", "proxyclasses/status", "proxygroups", "proxygroups/status", "dnsconfigs", "dnsconfigs/status", "recorders", "recorders/status"]
    verbs: ["get", "list", "watch", "update"]
  # The operator manages its own proxy RBAC in each namespace
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

```bash
kubectl apply -f tailscale-clusterrole.yaml
```

## 4. Create the ClusterRoleBinding

Cabotage automatically adds operator ServiceAccount subjects to this binding
as organizations are onboarded. Create it with an empty subjects list:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: tailscale-operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: tailscale-operator
subjects: []
```

```bash
kubectl apply -f tailscale-clusterrolebinding.yaml
```

## 5. Grant cabotage-app additional RBAC permissions

Cabotage's service account needs permissions to manage the Tailscale operator
resources in org namespaces. Add these to the cabotage-app ClusterRole:

```yaml
# Namespace-scoped RBAC management (for operator + proxies Roles/RoleBindings)
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "list", "create", "update", "patch", "delete"]

# ClusterRoleBinding management (to add operator SA subjects)
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["clusterrolebindings"]
  verbs: ["get", "list", "update", "patch"]

# The operator Role delegates these permissions, so cabotage must hold them
# (K8s RBAC escalation prevention — you cannot create a Role granting
# permissions you don't already have)
- apiGroups: [""]
  resources: ["configmaps", "events", "services/status"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["apps"]
  resources: ["statefulsets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["get", "list", "watch", "create", "update", "patch"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses/status"]
  verbs: ["get", "list", "watch", "update", "patch"]
- apiGroups: ["tailscale.com"]
  resources: ["*"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

## 6. Add Vault policy

Cabotage stores Tailscale OAuth credentials in Vault. Add to the cabotage app's
Vault policy:

```hcl
path "cabotage-secrets/integrations/*" {
  capabilities = ["create", "read", "update", "delete"]
}
```

## 7. Enable the feature in cabotage

Set the following environment variables for the cabotage application and worker:

| Variable | Default | Description |
|----------|---------|-------------|
| `CABOTAGE_TAILSCALE_OPERATOR_ENABLED` | `False` | Set to `True` to enable Tailscale operator management |
| `CABOTAGE_TAILSCALE_OPERATOR_IMAGE` | `ghcr.io/tailscale/k8s-operator` | Operator container image |
| `CABOTAGE_TAILSCALE_OPERATOR_VERSION` | `v1.80.3` | Operator image tag |

## What cabotage manages automatically

Once the prerequisites above are in place, cabotage handles per-namespace:

- **Secret** — OAuth credentials for the Tailscale operator
- **ServiceAccount** — `tailscale-operator-{org}` for the operator pod
- **ServiceAccount** — `proxies` for the ingress proxy pods
- **Role + RoleBinding** — namespace-scoped operator permissions
- **Role + RoleBinding** — `proxies` role for proxy pod secret access
- **ClusterRoleBinding subject** — adds operator SA to the pre-provisioned binding
- **Deployment** — the Tailscale operator pod itself
- **Cleanup** — tears down all resources when integration is removed

## What organization admins need to do

Organization admins configure their Tailscale integration in
**Organization Settings > Tailscale Integration**. Before doing so:

### Tailnet ACL policy

Add these tag owners to the tailnet policy file
(https://login.tailscale.com/admin/acls):

```jsonc
{
  "tagOwners": {
    "tag:k8s-operator": [],
    "tag:k8s":          ["tag:k8s-operator"]
  },
  // Required: auto-approve services advertised by ProxyGroup proxies
  "autoApprovers": {
    "services": {
      "tag:k8s": ["tag:k8s"]
    }
  },
  // If using Funnel, also add:
  "nodeAttrs": [
    {
      "target": ["tag:k8s"],
      "attr":   ["funnel"]
    }
  ]
}
```

### OAuth client

1. Go to https://login.tailscale.com/admin/settings/trust-credentials
2. Click **Credential** > **OAuth**
3. Enable scopes: **Devices Core** (Write), **Auth Keys** (Write), **Services** (Write)
4. Assign tag: `tag:k8s-operator`
5. Click **Generate credential**
6. Paste the client ID and secret into the cabotage UI

### MagicDNS and HTTPS

Tailscale ingresses require both MagicDNS and HTTPS to be enabled on the
tailnet. These are enabled by default on new tailnets. Verify at
https://login.tailscale.com/admin/dns.

## Architecture notes

### Backend protocol

Cabotage applications use ghostunnel sidecars for internal TLS, so backends
speak HTTPS on port 8000. Services expose both port 8000 (for nginx ingress)
and port 443 (for Tailscale HTTPS backend detection). The Tailscale operator
automatically uses `https+insecure://` when the backend port is 443, which
correctly handles the internal self-signed certificates.

### One operator per namespace

The Tailscale operator's `OPERATOR_NAMESPACE` env var scopes it to a single
namespace. Since organizations can have multiple namespaces (one per
environment), cabotage deploys one operator instance per namespace. Each
operator authenticates independently to the same tailnet using the org's OAuth
credentials.
