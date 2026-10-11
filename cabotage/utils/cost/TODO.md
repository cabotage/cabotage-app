- Replace mock meter values in `metering.collect_service_usage` with real telemetry:
  - Egress: K8s network metrics / cloud flow logs
  - Build minutes: `Image.build_started_at` / `build_completed_at` deltas
  - Block storage: PVC sizes from K8s API
  - DB storage: `pg_database_size` queries
  - Postgres / Redis hours: Resource model instance counts
  - Tailscale: node count from Tailscale API
- Higher-fidelity AWS rates: ingest a Cost and Usage Report (CUR) with Split
  Cost Allocation Data for EKS so per-pod cost comes from AWS instead of being
  derived from `pod_class × replicas`. Replace the static `RATES` table with the
  CUR-derived numbers.
- Per-pod-class rates: today `aws_costs.RATES` is a single blended rate for
  vCPU/RAM. If we want a c-class workload to reflect cost more than an r-class one,
  attach `cost_per_vcpu_hr` / `cost_per_gb_hr` to entries in `pod_classes`.
