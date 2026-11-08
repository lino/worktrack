# Metrics Host, Health Checks & Docker

This guide explains **why** you might want to enable Worktrack's built-in metrics and health-check server, **when** you can ignore it completely, and **how** to configure it if you self-host a shared Team Hub or run Worktrack in Docker.

______________________________________________________________________

## Why Would You Want a Metrics Host?

If you only use `wt` on your own laptop to track time and sync with Jira, **you do not need to run or configure a metrics host at all**.

You only want the metrics and health server in two situations:

1. **You self-host a shared Team Hub or OAuth Broker** (for example, in Docker or on an internal server) so your team can sign in to Jira without sharing OAuth `client_secret` values and sync shared project catalogs (`WORKTRACK_MODE=hub`). Operations tools like Docker, Kubernetes, and Prometheus need a `/healthz` endpoint to check if the server is alive and a `/metrics` endpoint to alert you if OAuth token exchanges or database queries fail.
1. **You want to monitor or benchmark local performance**: If you run a local Prometheus/Grafana stack on your workstation or want to inspect why a shell prompt or Outbox sync took longer than expected, Worktrack records lightweight, privacy-safe counters and latency histograms (never containing ticket keys, emails, or file paths).

______________________________________________________________________

## How to Inspect or Serve Metrics

### 1. Print current metrics in your terminal

You can view your local counters and latency histograms at any time without starting a server:

```bash
mise run metrics
# or directly:
uv run python -m worktrack.telemetry
```

### 2. Run the standalone `/metrics` and `/healthz` HTTP server

To expose `/healthz` and `/metrics` for Prometheus or container health checks:

```bash
uv run python -m worktrack.telemetry --serve --host 127.0.0.1 --port 9112
```

When you run the full web server (`wt serve`), `/healthz` and `/metrics` are also served automatically on the web server port.

### 3. Run with Docker Compose

Worktrack includes a ready-to-use `Dockerfile` and `compose.yaml` with built-in health checks:

```bash
docker compose up --build -d

# Check container readiness and Prometheus metrics
curl -fsS http://127.0.0.1:9112/healthz
curl -fsS http://127.0.0.1:9112/metrics
```

______________________________________________________________________

## Metrics, Logging & Topology Configuration

You can configure these settings via environment variables or under the `[worktrack]` section of `$XDG_CONFIG_HOME/worktrack/config.toml`:

| Why You Would Change It                                             | Environment Variable                 | Config Key (`[worktrack]`) | Default                               |
| ------------------------------------------------------------------- | ------------------------------------ | -------------------------- | ------------------------------------- |
| Run as a shared Team Hub (`hub`) instead of a personal laptop       | `WORKTRACK_MODE`                     | `mode`                     | `node` (`node` or `hub`)              |
| Bind the metrics server to a container network interface            | `WORKTRACK_METRICS_HOST`             | `metrics_host`             | `127.0.0.1`                           |
| Change the TCP port if `9112` is already in use on your machine     | `WORKTRACK_METRICS_PORT`             | `metrics_port`             | `9112`                                |
| Adjust how long the metrics server waits during graceful shutdown   | `WORKTRACK_METRICS_SHUTDOWN_TIMEOUT` | `metrics_shutdown_timeout` | `5.0` (seconds)                       |
| Store local metric counters and event logs in a custom directory    | `WORKTRACK_TELEMETRY_DIR`            | `telemetry_dir`            | `$XDG_STATE_HOME/worktrack/telemetry` |
| Show more detailed diagnostic logs (`DEBUG`) or quieter (`WARNING`) | `WORKTRACK_LOG_LEVEL`                | `log_level`                | `INFO`                                |
| Cap distinct label values per metric to protect memory usage        | `WORKTRACK_MAX_LABEL_VALUES`         | `max_label_values`         | `20`                                  |
| Cap total time series per metric                                    | `WORKTRACK_MAX_SERIES_PER_METRIC`    | `max_series_per_metric`    | `500`                                 |

Example `config.toml` for a self-hosted monitoring setup:

```toml
[worktrack]
mode = "node"
metrics_host = "127.0.0.1"
metrics_port = 9112
log_level = "INFO"
```
