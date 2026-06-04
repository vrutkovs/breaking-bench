# Breaking Bench

Streamlit controller for running k6 load tests against VictoriaMetrics.

![Breaking Bench running insert and select workloads](docs/screenshot.svg)

App starts two Podman containers:

- `breaking-bench-k6-insert` writes generated time series through Prometheus remote write.
- `breaking-bench-k6-select` runs query range requests against vmselect.

Both containers use `docker.io/grafana/k6:1.7.1` and k6 automatic extension resolution for `k6/x/remotewrite` and `k6/x/faker`.

## Requirements

- Python 3.11+
- uv
- Podman
- Network access to VictoriaMetrics `vminsert` and `vmselect`

## Run

```bash
uv run streamlit run app.py
```

Open Streamlit URL shown by command output.

## Configuration

Sidebar fields:

- `Write URL`: VictoriaMetrics Prometheus remote write endpoint, for example `/insert/0/prometheus/api/v1/write`.
- `Select URL`: VictoriaMetrics query range endpoint, for example `/select/0/prometheus/api/v1/query_range`.
- `Metric name prefix`: prefix for generated metrics. Metric names become `<prefix>_<index>`.
- `Metric variants`: number of metric names to generate.
- `Extra labels`: number of additional labels named `label_0`, `label_1`, etc.
- `Insert VUs`: insert workload VUs.
- `Select VUs`: select workload VUs.

Changing metric prefix, variants, or labels while a scenario is running regenerates k6 script and restarts affected container. Changing VUs is applied through k6 REST API without restart.

## Controls

- `Start insert`: starts remote-write workload on port `6565`.
- `Start select`: starts query workload on port `6566`.
- `Stop`: removes corresponding Podman container.
- `Pause` / `Resume`: controls running k6 process through REST API.

Container names are reused with Podman `--replace`.

## Metrics

Insert workload writes custom metrics with labels:

- `__name__`: generated metric name, such as `test__0`
- `first_name`
- `last_name`
- optional `label_N` labels

k6 also writes built-in metrics such as:

- `k6_iterations_total`
- `k6_vus`
- `k6_vus_max`
- `k6_http_reqs_total`
- `k6_http_req_duration_*`

Example vmselect query:

```bash
curl 'http://vmselect.example/select/0/prometheus/api/v1/query?query=k6_iterations_total'
```

## Development

Type check and syntax check:

```bash
uv run mypy app.py
uv run python -m py_compile app.py
```
