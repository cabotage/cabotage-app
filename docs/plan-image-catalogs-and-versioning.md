# Image Catalogs, Extension Tracking, and Controlled Version Rollouts

## Context

Currently, backing services pin a specific container image directly on the CRD (`spec.imageName` for CNPG, image tag for Redis). CNPG 1.29+ supports **ClusterImageCatalogs** — cluster-scoped CRDs that map major versions to specific images.

**Goals:**
1. Use ClusterImageCatalogs as the source of truth for available Postgres versions
2. Support CNPG extensions (PG 18+ only)
3. Control _when_ minor version updates are applied per resource
4. For Redis, track version similarly

---

## Design

### ClusterImageCatalogs as source of truth

Admins create date-suffixed ClusterImageCatalogs in K8s (via terraform or manually):

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: ClusterImageCatalog
metadata:
  name: cabotage-2026-04-12
spec:
  images:
    - major: 18
      image: ghcr.io/cloudnative-pg/postgresql:18.3-...@sha256:...
      extensions:
        - name: pgvector
          image:
            reference: ghcr.io/cloudnative-pg/pgvector:0.8.0-pg18
```

When a new minor version is released, a new catalog is created:
```yaml
# cabotage-2026-05-01
spec:
  images:
    - major: 18
      image: ghcr.io/cloudnative-pg/postgresql:18.4-...@sha256:...
```

Old catalogs are kept (existing clusters still reference them).

### How cabotage uses them

**At resource creation time**: Query K8s for all `ClusterImageCatalog` objects with a cabotage label, find the latest one, use it as the catalog ref for the new cluster. Store the catalog name on the resource.

**Cluster CRD**:
```yaml
spec:
  imageCatalogRef:
    apiGroup: postgresql.cnpg.io
    kind: ClusterImageCatalog
    name: cabotage-2026-04-12
    major: 18
```

**Determining available versions**: List all ClusterImageCatalogs, extract the major versions from their `spec.images[].major` fields. The create form's version selector is populated from this.

**Controlled updates**: The resource detail page shows which catalog a cluster is pinned to. If a newer catalog exists, show "Update available — cabotage-2026-05-01". User clicks update, cabotage patches the cluster's `imageCatalogRef.name` to the new catalog.

### Model changes

**`Resource` base model** — add:
- `image_catalog` (String, nullable) — the ClusterImageCatalog name this resource is pinned to (e.g., "cabotage-2026-04-12"). Null for Redis (no catalog mechanism).

**`PostgresResource`** — add:
- `extensions` (JSONB, nullable) — list of enabled extension names

Remove `POSTGRES_IMAGES` dict from tasks — images come from the catalog.

### Redis versioning

No K8s catalog mechanism for Redis. Keep the `REDIS_IMAGES` dict in code for now. The `service_version` field tracks the major version, the image dict maps to the specific tag.

Future: could create a similar pattern with ConfigMaps or a custom CRD if needed.

### UI changes

**Create form**: Query available ClusterImageCatalogs to populate the Postgres version choices (major versions). For Redis, keep the static list.

**Detail page**: Show the catalog name and the effective image. If a newer catalog exists, show an "Update available" badge with the catalog date.

**Settings page**: Add "Update catalog" action that patches `imageCatalogRef.name`.

### Task changes

**Provision**: 
1. List ClusterImageCatalogs labeled `cabotage.io/image-catalog=true`
2. Sort by name (date suffix), pick latest
3. Set `spec.imageCatalogRef` instead of `spec.imageName`
4. Store catalog name on resource

**Update**:
- If `image_catalog` changed, patch the cluster's `imageCatalogRef.name`

---

## Implementation order

1. Remove `spec.imageName` from CNPG rendering, switch to `imageCatalogRef`
2. Add `image_catalog` field to Resource model + migration
3. Add `extensions` field to PostgresResource + migration
4. Update provision task to query for latest catalog and set ref
5. Update detail page to show catalog info
6. Add "update catalog" action to settings page
7. RBAC: add `clusterimagecatalogs` (cluster-scoped, read-only) to ClusterRole

## RBAC additions

```yaml
- verbs: [get, list]
  apiGroups: [postgresql.cnpg.io]
  resources: [clusters, clusterimagecatalogs]
```

(Existing `clusters` rule already has full CRUD; just add `clusterimagecatalogs` with read-only.)

## Files to modify

| File | Change |
|------|--------|
| `cabotage/server/models/resources.py` | Add `image_catalog`, `extensions` fields. Remove `POSTGRES_IMAGES`. |
| `cabotage/celery/tasks/resources.py` | Switch to `imageCatalogRef`, query for latest catalog |
| `cabotage/server/user/views.py` | Query catalogs for version choices, update catalog action |
| `cabotage/server/user/forms.py` | Dynamic version choices from K8s |
| Detail/settings templates | Show catalog info, update button |
| `cabotage-terraform/.../00-role.yml` | Add `clusterimagecatalogs` read access |
| `cabotage-terraform/` | Add ClusterImageCatalog manifests |
