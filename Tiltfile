# -*- mode: Python -*-
# Cabotage Local Development with Tilt
#
# Usage:
#   tilt up
#
# This Tiltfile sets up the complete Cabotage development environment on OrbStack K8s.
# Hot reload is handled by:
#   - hostPath volume mounts (source code mounted directly into pods)
#   - Hupper (Python process watcher, auto-reloads on file changes)

# Configuration
k8s_context('orbstack')

# Dynamic paths (avoids hardcoding user-specific paths)
HOME = str(local('echo $HOME', quiet=True)).strip()
load('ext://namespace', 'namespace_create')

# Create namespace
namespace_create('cabotage-dev')

# =============================================================================
# Docker Image Build
# =============================================================================

# Build the development image with DEVEL=yes for dev dependencies
docker_build(
    'cabotage-app:dev',
    '.',
    dockerfile='Dockerfile',
    build_args={'DEVEL': 'yes'},
    # Since we use hostPath mounts, we don't need live_update for file sync
    # Hupper handles process reload automatically
)

# =============================================================================
# Infrastructure Resources
# =============================================================================

# Deploy infrastructure in dependency order
# (namespace already created by namespace_create above)

# =============================================================================
# OrbStack DNS - No Port Conflicts!
# =============================================================================
# All services are accessible via OrbStack's automatic DNS:
#   - http://cabotage-app.cabotage-dev.orb.local (web app)
#   - db.cabotage-dev.orb.local:5432 (postgres)
#   - redis.cabotage-dev.orb.local:6379 (redis)
#   - consul.cabotage-dev.orb.local:8500 (consul)
#   - vault.cabotage-dev.orb.local:8200 (vault)
#   - minio.cabotage-dev.orb.local:9000 (minio)
#   - registry.cabotage-dev.orb.local:5001 (registry)
#
# Uncomment port_forwards below if you need localhost access for specific tools.
# =============================================================================

# Postgres - must come first (Vault needs it for DB creds)
k8s_yaml('k8s/dev/infra/postgres.yaml')
k8s_resource(
    'postgres',
    objects=['postgres-initdb:configmap'],
    # port_forwards=['5432:5432'],
    labels=['infra'],
)

# Redis
k8s_yaml('k8s/dev/infra/redis.yaml')
k8s_resource(
    'redis',
    # port_forwards=['6379:6379'],
    labels=['infra'],
)

# Consul
k8s_yaml('k8s/dev/infra/consul.yaml')
k8s_resource(
    'consul',
    # port_forwards=['8500:8500'],
    labels=['infra'],
)

# Vault - depends on postgres
k8s_yaml('k8s/dev/infra/vault.yaml')
k8s_resource(
    'vault',
    objects=['vault-config:configmap'],
    # port_forwards=['8200:8200'],
    resource_deps=['postgres'],
    labels=['infra'],
)

# MinIO (S3-compatible storage for registry)
k8s_yaml('k8s/dev/infra/minio.yaml')
k8s_resource(
    'minio',
    # port_forwards=['9000:9000', '9001:9001'],
    labels=['infra'],
)

# Docker Registry - depends on minio
k8s_yaml('k8s/dev/infra/registry.yaml')
k8s_resource(
    'registry',
    objects=['registry-config:configmap'],
    # port_forwards=['5001:5001'],
    resource_deps=['minio'],
    labels=['infra'],
)

# BuildKit
k8s_yaml('k8s/dev/infra/buildkit.yaml')
k8s_resource(
    'buildkit',
    objects=['buildkit-config:configmap'],
    # port_forwards=['1234:1234'],
    resource_deps=['registry'],
    labels=['infra'],
)

# =============================================================================
# Application Resources
# =============================================================================

# Template app.yaml with dynamic paths
app_yaml = str(read_file('k8s/dev/app.yaml'))
app_yaml = app_yaml.replace('__HOME__', HOME)
k8s_yaml(blob(app_yaml))

k8s_yaml('k8s/dev/ingress.yaml')

# Cabotage Web App
# Access via: http://cabotage.192-168-139-2.nip.io (through nginx ingress)
k8s_resource(
    'cabotage-app',
    objects=['cabotage-app:ingress'],  # Include the ingress with this resource
    resource_deps=['postgres', 'redis', 'vault', 'consul'],
    labels=['app'],
    trigger_mode=TRIGGER_MODE_AUTO,
)

# Cabotage Celery Worker
k8s_resource(
    'cabotage-app-worker',
    resource_deps=['postgres', 'redis', 'vault', 'consul'],
    labels=['app'],
    trigger_mode=TRIGGER_MODE_AUTO,
)

# =============================================================================
# Local Commands
# =============================================================================

# Working directory for kubectl exec commands (must cd first, then use python -m)
EXEC_PREFIX = 'kubectl exec -n cabotage-dev deploy/cabotage-app -- sh -c "cd /opt/cabotage-app/src && '

# Run database migrations (auto-runs on startup, safe to run multiple times)
local_resource(
    'db-migrate',
    cmd=EXEC_PREFIX + 'python3 -m flask db upgrade"',
    resource_deps=['cabotage-app'],
    labels=['setup'],
    auto_init=True,
)

# Create admin user (auto-runs on startup, script should handle existing user)
local_resource(
    'create-admin',
    cmd=EXEC_PREFIX + 'python3 -m cabotage.scripts.create_admin"',
    resource_deps=['db-migrate'],
    labels=['setup'],
    auto_init=True,
)

# View Flask routes
local_resource(
    'routes',
    cmd=EXEC_PREFIX + 'python3 -m flask routes"',
    resource_deps=['cabotage-app'],
    labels=['commands'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

# =============================================================================
# Development Workflow
# =============================================================================

# Watch Python files for changes (informational - Hupper handles actual reload)
watch_file('cabotage/')
watch_file('migrations/')

# Watch templates for changes
watch_file('cabotage/client/templates/')
watch_file('cabotage/client/static/')
