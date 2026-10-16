# Tenant Log Export

Tenants can send their application logs to their own Datadog account from the
Logging settings tab. Cabotage only labels pods and renders one Fluent Bit
output per application environment; the platform collects the logs and runs
the exporter. Tenant pods get no sidecar, host mount, or API key.

## Enabling

Set the config/environment variable once the platform pieces below are
installed:

```
LOG_EXPORT_ENABLED=true
```

When disabled (the default), tenants' `DD_LOGS_ENABLED` is ignored: pods are
labeled `log-export.cabotage.io: "false"` and cabotage never reads or writes the
exporter Secret.

`LOG_EXPORT_NAMESPACE` (default `cabotage`) and `LOG_EXPORT_SECRET` (default
`tenant-log-export`) name the Secret cabotage writes outputs into.

## What cabotage does

- Labels every deployment, job, and cronjob pod with
  `log-export.cabotage.io: "true"` or `"false"`, and sets
  `ad.datadoghq.com/logs_exclude` to the same value so a shared Datadog node
  agent does not also collect opted-in logs.
- On each deploy, before release commands run, sets or removes the key
  `<namespace>.<app label>.yaml` in the Secret. The deploy fails if export is
  enabled for the app and the Secret does not exist; cabotage never creates it.
- Removes the key when an application environment is deleted, and every
  `<namespace>.*` key when a branch deploy is torn down.
- Every 10 minutes (`reconcile_log_export`), rebuilds the keys from each
  environment's latest completed release and removes keys for deleted or
  opted-out environments. Keys that do not match `<namespace>.<app>.yaml` are
  left alone.

Each key holds one Fluent Bit YAML output:

```yaml
pipeline:
  outputs:
  - name: datadog
    alias: <namespace>.<app label>
    match: tenant.<namespace>.<app label>
    host: http-intake.logs.<DD_SITE>
    apikey: <tenant DD_API_KEY>
    dd_message_key: _entry
    retry_limit: 5
    storage.total_limit_size: 64M
    # ...tls, compression, service/source/tags
```

## What the platform must provide

1. **The Secret**, created ahead of time with one placeholder key, for example
   `00-placeholder.yaml: ""`. Fluent Bit rejects an include glob that matches
   nothing. Apply it with `stringData` so `kubectl apply` leaves cabotage's keys
   alone.
2. **A Fluent Bit 4.2+ exporter** with no host access that:
   - mounts the Secret (not via `subPath`, which never updates) and includes
     `<mount>/..data/*.yaml`. The kubelet swaps `..data` before it updates the
     per-key links, so globbing `<mount>/*.yaml` misses added keys and fails on
     removed ones;
   - hot reloads (`hot_reload: on`, then `POST /api/v2/reload`) when the
     mount changes, for example with a `configmap-reload` sidecar;
   - tags each record `tenant.<namespace>.<app>` and exposes the original line
     as `_entry`;
   - uses filesystem storage, with `scheduler.cap` kept short, because a reload
     waits for pending chunks.
3. **A collector** that forwards only process-container lines from pods
   labeled `log-export.cabotage.io=true`, along with their `namespace` and
   `app` pod labels.
4. **An ingress restriction** so only the collector can reach the exporter.
   Records name their own tenant, so any client that can reach it could write
   into another tenant's account.

The [cabotage/terraform](https://github.com/cabotage/terraform) module installs
all of this, and sets `LOG_EXPORT_ENABLED`, when `enable_log_export = true`.

It runs `resident-alloy` as the collector. A
`tenant_export` branch drops unlabeled pods and non-process containers, runs
`stage.cri`, and `stage.pack`s the labels around the line. It then sends the
logs over OTLP/TLS to a `tenant-log-export` Fluent Bit StatefulSet. That
StatefulSet JSON-parses the `log` field and runs
`rewrite_tag $app ^(.+)$ tenant.$namespace.$app false`.

## Delivery

Export is best-effort:

- Changes reach the exporter after the kubelet syncs the Secret, which takes
  up to about a minute.
- A reload briefly pauses every tenant's export and can drop a few in-flight
  lines.
- A destination that stays down drops logs after five retries or 64M of
  buffer, without blocking other tenants.
