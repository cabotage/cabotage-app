# Pod sizes

Each process runs in a pod with dedicated CPU and memory. Choose a size based on your workload. The naming convention indicates the optimization: **m1** is balanced, **c1** is CPU-optimized, and **r1** is memory-optimized.

Pod sizes are platform-configured; the table below reflects the sizes currently offered.

| Size | Type | CPU (request) | CPU (limit) | Mem (request) | Mem (limit) |
|---|---|---:|---:|---:|---:|
| `m1.small` | Balanced | 125m | 250m | 256Mi | 384Mi |
| `m1.medium` | Balanced | 250m | 500m | 512Mi | 768Mi |
| `m1.large` | Balanced | 500m | 1000m | 1024Mi | 1536Mi |
| `c1.small` | CPU | 250m | 375m | 256Mi | 384Mi |
| `c1.medium` | CPU | 500m | 750m | 512Mi | 768Mi |
| `c1.large` | CPU | 1000m | 1500m | 1024Mi | 1536Mi |
| `r1.small` | Memory | 125m | 250m | 1024Mi | 1536Mi |
| `r1.medium` | Memory | 250m | 500m | 1536Mi | 2304Mi |
| `r1.large` | Memory | 500m | 1000m | 2048Mi | 3072Mi |
| `r1.xlarge` | Memory | 750m | 1500m | 2048Mi | 3072Mi |
| `r1.2xlarge` | Memory | 1500m | 1500m | 4096Mi | 4096Mi |

**Request** is the guaranteed minimum. **Limit** is the maximum the pod can burst to. CPU is measured in millicores (1000m = 1 core). The default size is `m1.large`.

## Related

- [Deploying your code](deploying.md)
- [Scheduled jobs](jobs.md) — job pods can be resized from the Overview tab
- [Processes & Procfile](processes.md)
