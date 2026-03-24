# Tailscale Ingress

Cabotage can deploy applications with ingresses accessible on an organization's
own Tailscale network (tailnet). Each org authenticates independently via OIDC
workload identity federation — no long-lived secrets.

## Architecture

```
                         ┌──────────────────────────────┐
                         │   Tailscale Control Plane     │
                         │  - validates JWTs via JWKS    │
                         │  - issues access tokens       │
                         └──────┬───────────────────────┘
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
  cabotage-app           Tailscale Operator    operator-manager
  (OIDC issuer)          (Helm, single)        (Kopf, single)
  - mints JWTs           - reconciles          - watches CabotageTailscale
  - serves JWKS            Ingresses,            OperatorConfig CRDs
  - refreshes tokens       ProxyGroups,        - creates Tailnet CRDs
  - manages Secrets        Tailnet CRDs        - creates ProxyGroups
```

**Single operator per cluster.** One Tailscale operator (deployed via Helm)
serves all organizations. Per-org isolation is achieved through:

- **Tailnet CRD** — cluster-scoped resource pointing to a credential Secret.
  The operator uses per-org credentials for API authentication.
- **ProxyGroup** — cluster-scoped resource with `spec.tailnet` referencing the
  org's Tailnet CRD. Proxy pods authenticate to the org's tailnet.
- **OIDC JWTs** — cabotage mints short-lived JWTs (signed by Vault transit,
  ES256) for each org. Refreshed every 15 minutes.

### Forked Operator

The integration requires a forked Tailscale operator with changes from upstream:

1. **Tailnet WIF** — `clientForTailnet()` reads JWT from Secret instead of only
   supporting static OAuth credentials
2. **VIP Service auto-advertisement** — `TS_EXPERIMENTAL_SERVICE_AUTO_ADVERTISEMENT=true`
   on ProxyGroup pods
3. **ProxyGroup CRD** — `spec.tailnet` field (immutable) for multi-tenant support

A postrender script strips the upstream ProxyGroup CRD from Helm chart output so
terraform's version (with `tailnet`) is authoritative.

These changes should eventually land upstream. When they do, switch back to
mainline `tailscale/k8s-operator` and `tailscale/tailscale` images and drop the
postrender workaround.

### Backend Protocol

Cabotage applications use ghostunnel sidecars for internal TLS. Services expose
port 8000 named `https`. The Tailscale operator detects the named port and uses
`https+insecure://` (TLS without CA verification — acceptable since traffic is
cluster-internal).

## Cluster Prerequisites

All cluster prerequisites are managed by the `cabotage` terraform module with
`enable_tailscale = true`.

### Terraform Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `enable_tailscale` | yes | `false` | Enable Tailscale integration |
| `tailscale_operator_oauth_client_id` | yes | — | Platform tailnet OAuth client ID |
| `tailscale_operator_oauth_client_secret` | yes | — | Platform tailnet OAuth client secret (sensitive) |
| `tailscale_tag_prefix` | no | `cabotage` | ACL tag prefix |
| `tailscale_operator_image` | no | `ewdurbin/ts-k8s-operator` | Forked operator image |
| `tailscale_operator_image_tag` | no | `1.97.71-dirty0` | Operator image tag |
| `tailscale_proxy_image` | no | `ewdurbin/ts-tailscale` | Forked proxy image |
| `tailscale_proxy_image_tag` | no | `1.97.71-dirty0` | Proxy image tag |

### Platform Tailnet Setup

The platform tailnet (where the operator itself authenticates) needs:

1. OAuth client with scopes: Devices Core (Write), Auth Keys (Write)
2. ACL tag: `tag:{prefix}-operator`
3. HTTPS certificates enabled (DNS settings)
4. Funnel enabled for `tag:{prefix}-operator` if using the cabotage-app funnel
   ingress

### Cabotage App Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `CABOTAGE_TAILSCALE_OPERATOR_ENABLED` | `False` | Enable Tailscale UI |
| `CABOTAGE_TAILSCALE_TAG_PREFIX` | `cabotage` | Tag prefix for setup instructions and ingress annotations |

Both are propagated by terraform automatically.

## Rollout

### Phase 1: Infrastructure (terraform apply)

A single `terraform apply` deploys everything in order:

1. Fork-specific CRDs (Tailnet, ProxyGroupPolicy) — applied before Helm
2. Helm release — Tailscale operator with forked images, postrender strips
   ProxyGroup CRD
3. ProxyGroup CRD — fork's version applied after Helm (server-side apply,
   force-conflicts)
4. Supplemental RBAC — for fork resources (Tailnets, ProxyGroupPolicies)
5. Operator-manager — Kopf controller with minimal RBAC

```bash
# Verify
kubectl get deploy -n tailscale operator                    # operator running
kubectl get deploy -n cabotage tailscale-operator-manager   # manager running
kubectl get ingressclass tailscale                          # IngressClass exists
kubectl get crd proxygroups.tailscale.com \
  -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties}' \
  | python3 -c "import sys,json; print('tailnet' in json.load(sys.stdin))"  # True
```

### Phase 2: Application Code (deploy cabotage-app)

Deploy the updated cabotage-app (web + worker + beat). This adds:

- OIDC issuer endpoints
- Token exchange validation
- Dual ingress rendering (nginx + tailscale)
- Celery beat: JWT refresh (15 min), state reconciler (30s)
- Organization settings UI with 4-step onboarding

```bash
# Verify OIDC endpoints
curl -s https://{cabotage-url}/.well-known/openid-configuration | python3 -m json.tool
curl -s https://{cabotage-url}/.well-known/jwks.json | python3 -m json.tool
```

### Phase 3: Database Migration

```bash
flask db upgrade
```

Adds `tailscale_integrations` table and Tailscale columns on `ingresses`.
Non-destructive — no existing data is modified.

### Phase 4: Organization Onboarding

Org admins configure via the UI (Organization Settings > Tailscale Integration).
The 4-step setup:

1. **Create ACL Tags** — `tag:{prefix}-operator` and `tag:{prefix}` in tailnet
   policy, operator tag owns service tag
2. **Configure ACL Policies** — auto-approvers for services, funnel attribute
3. **Enable HTTPS Certificates** — in tailnet DNS settings
4. **Create Trust Credential** — OIDC federated identity with cabotage's issuer
   URL and org-specific subject (`org:{k8s_identifier}`)

The only value pasted is the **Client ID** — no secrets.

```bash
# Verify per-org
kubectl get cabotagetailscaleoperatorconfig -A   # CRD created
kubectl get tailnets                              # Tailnet CRD
kubectl get proxygroups                           # ProxyGroup ready
kubectl get pods -n tailscale \
  -l tailscale.com/parent-resource-type=proxygroup  # proxy pods running
```

### Phase 5: Application Ingresses

Create Tailscale ingresses via the application ingress UI:

1. Add new ingress with class `tailscale`
2. Set a hostname
3. Deploy

```bash
# Verify
kubectl get ingress -n {namespace} -o yaml | grep tailscale.com
# Check Tailscale admin > Services — should show hosts
curl -k https://{hostname}.{tailnet}.ts.net
```

## Rollback

### Per-org removal
Delete via UI (Organization Settings > Remove). Tears down CRD, Tailnet,
ProxyGroup, and credential Secret.

### Feature-wide disable
1. `CABOTAGE_TAILSCALE_OPERATOR_ENABLED=False` in cabotage-app
2. `enable_tailscale = false` in terraform, apply
3. All Tailscale resources removed; nginx ingresses unaffected

