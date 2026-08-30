# Kubernetes deployment

Applied in filename order:

```bash
kubectl apply -f deploy/k8s/
```

## Before you apply

1. **Create the secret for real.** `01-config.yaml` ships an empty stub so the
   manifests apply cleanly. With no key for the selected `LLM_PROVIDER` the app
   starts in degraded mode and serves baseline keyword scores. Populate it with:

   ```bash
   kubectl create secret generic screener-secrets \
     --namespace resume-screener \
     --from-literal=GEMINI_API_KEY="..." \
     --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

2. **Set the image.** `02-deployment.yaml` points at
   `ghcr.io/OWNER/resume-screener:latest`. CI rewrites this; for a manual deploy,
   substitute your own registry path.

3. **Elasticsearch and Kibana are not included here.** Running a production
   Elasticsearch cluster from hand-written Deployment manifests is a mistake — use
   [ECK](https://www.elastic.co/guide/en/cloud-on-k8s/current/index.html) or a
   managed cluster, and point `06-logstash.yaml` at it. Filebeat and Logstash *are*
   included, because they are genuinely part of this application's deployment.

## What is here

| File | Purpose |
|---|---|
| `00-namespace.yaml` | Namespace |
| `01-config.yaml` | ConfigMap for tunables, Secret stub for credentials |
| `02-deployment.yaml` | App Deployment, Service, PodDisruptionBudget |
| `03-autoscale.yaml` | HorizontalPodAutoscaler |
| `04-ingress.yaml` | TLS ingress with upload-size and timeout tuning |
| `05-filebeat.yaml` | Filebeat DaemonSet, RBAC, node log mounts |
| `06-logstash.yaml` | Logstash Deployment, Service, pipeline ConfigMap |

## Design notes

**Liveness never checks the LLM.** `/healthz` is dependency-free. If liveness
depended on the Gemini or Anthropic API, a provider outage would restart every
pod in a crash loop while the fallback engine was still serving traffic
correctly.

**`/readyz` reports degradation without failing.** It returns `degraded: true`
when the LLM is configured but unreachable. Pulling those pods out of the load
balancer would turn a partial outage into a total one, so readiness stays green
and the signal goes to the dashboard instead.

**No CPU limit.** Screening requests are blocked on network I/O, not compute.
CPU throttling would add latency without protecting anything. Memory *is* capped,
so a leak kills a pod instead of a node.

**90s termination grace plus a preStop sleep.** A screening call can run for a
minute. The pod stops receiving new traffic, then drains in-flight work before
exiting.

**Filebeat filters on the `app: resume-screener` pod label.** Without that
`drop_event`, every system namespace in the cluster floods the index.
