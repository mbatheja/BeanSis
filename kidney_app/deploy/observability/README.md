# Bean Sis observability

The single-VM setup uses Prometheus for 15-second metric collection and Grafana for dashboards. Prometheus retains 15 days of metrics. Grafana and Prometheus data survive container recreation in named Docker volumes.

## Start

Set a long random `GRAFANA_ADMIN_PASSWORD` in the VM's untracked `.env` file. Then run:

```sh
docker compose up -d --build api prometheus grafana
```

Compose binds Grafana to `127.0.0.1:3000`; it is not exposed on the VM's public interfaces. From your computer, open an SSH tunnel:

```sh
ssh -L 3000:127.0.0.1:3000 <vm-user>@<vm-host>
```

Then open `http://localhost:3000`, sign in as `admin` with the password from `.env`, and choose **Bean Sis Overview**.

## Collected metrics

- HTTP request counts, status codes, and latency by method and route template.
- Nebius Token Factory request counts, latency, reported token totals, and success/error outcome by configured model.
- Last successful and latest-run status for the scheduled clinical-trials, PubMed, and provider-directory refreshes.

Metric labels do not contain request bodies, query strings, user IDs, ZIP codes, or patient data. Caddy returns 404 for `/metrics`; Prometheus scrapes it over the private Compose network. Ingestion refresh status is written atomically to `data/ingestion_status.json` and read by the API metrics endpoint.

## Logs and limits

Use `docker compose logs -f api research-agent` for container logs. Do not log prompts or answers because they can contain health information. This first pass does not centralize logs or send alerts; add Loki or an external managed service only if searching logs or notifications becomes necessary.
