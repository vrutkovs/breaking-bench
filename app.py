"""Breaking Bench — k6 load test controller."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from typing import Any


import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

K6_IMAGE = "quay.io/vrutkovs/k6-with-prw-extension:v2.0.0"
K6_API = "http://localhost:6565/v1"
K6_EXECUTORS = [
    "constant-vus",
    "ramping-vus",
    "constant-arrival-rate",
    "ramping-arrival-rate",
    "per-vu-iterations",
    "shared-iterations",
    "externally-controlled",
]
LOG_MAXLEN = 300

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "process": None,
        "logs": deque(maxlen=LOG_MAXLEN),
        "log_queue": queue.Queue(),
        "reader_thread": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# k6 script builder
# ---------------------------------------------------------------------------


def _stages_to_js(stages: list[dict]) -> str:
    parts = []
    for s in stages:
        parts.append(f'{{ duration: "{s["duration"]}", target: {s["target"]} }}')
    return "[" + ", ".join(parts) + "]"


def build_k6_script(
    executor: str,
    settings: dict,
    metric_name: str,
    num_labels: int,
) -> str:
    # Build executor options block
    opts: dict[str, Any] = {"executor": executor}

    if executor == "constant-vus":
        opts["vus"] = settings["vus"]
        opts["duration"] = settings["duration"]

    elif executor == "ramping-vus":
        opts["stages"] = settings["stages"]
        if settings.get("gracefulRampDown"):
            opts["gracefulRampDown"] = settings["gracefulRampDown"]

    elif executor == "constant-arrival-rate":
        opts["rate"] = settings["rate"]
        opts["timeUnit"] = settings["timeUnit"]
        opts["duration"] = settings["duration"]
        opts["preAllocatedVUs"] = settings["preAllocatedVUs"]
        if settings.get("maxVUs"):
            opts["maxVUs"] = settings["maxVUs"]

    elif executor == "ramping-arrival-rate":
        opts["stages"] = settings["stages"]
        opts["preAllocatedVUs"] = settings["preAllocatedVUs"]
        if settings.get("maxVUs"):
            opts["maxVUs"] = settings["maxVUs"]

    elif executor == "per-vu-iterations":
        opts["vus"] = settings["vus"]
        opts["iterations"] = settings["iterations"]
        if settings.get("maxDuration"):
            opts["maxDuration"] = settings["maxDuration"]

    elif executor == "shared-iterations":
        opts["vus"] = settings["vus"]
        opts["iterations"] = settings["iterations"]
        if settings.get("maxDuration"):
            opts["maxDuration"] = settings["maxDuration"]

    elif executor == "externally-controlled":
        opts["vus"] = settings["vus"]
        if settings.get("maxVUs"):
            opts["maxVUs"] = settings["maxVUs"]
        if settings.get("duration"):
            opts["duration"] = settings["duration"]

    # Serialize stages as JS array literals, everything else as JSON
    opts_lines: list[str] = []
    for k, v in opts.items():
        if k == "stages":
            opts_lines.append(f'          {k}: {_stages_to_js(v)},')
        elif isinstance(v, str):
            opts_lines.append(f'          {k}: "{v}",')
        else:
            opts_lines.append(f"          {k}: {json.dumps(v)},")

    opts_block = "\n".join(opts_lines)

    label_lines = "\n".join(
        [f"    tags['label_{i}'] = faker.person.firstName();" for i in range(num_labels)]
    )

    return f"""import {{ Trend }} from 'k6';
import {{ Faker }} from 'k6/x/faker';

const faker = new Faker(Date.now());
const metric = new Trend('{metric_name}', true);

export const options = {{
  scenarios: {{
    main: {{
{opts_block}
    }},
  }},
}};

export default function () {{
  const tags = {{}};
{label_lines}
  metric.add(Math.random() * 100, tags);
}}
"""


# ---------------------------------------------------------------------------
# Podman runner
# ---------------------------------------------------------------------------


def _log_reader(proc: subprocess.Popen, q: queue.Queue) -> None:
    """Read stdout+stderr from process into queue until process ends."""
    import selectors

    sel = selectors.DefaultSelector()
    if proc.stdout:
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    if proc.stderr:
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")

    while True:
        events = sel.select(timeout=0.2)
        for key, _ in events:
            line = key.fileobj.readline()  # type: ignore[union-attr]
            if line:
                q.put(line.decode(errors="replace").rstrip())
        if proc.poll() is not None:
            # Drain remaining output
            for key in sel.get_map().values():
                for line in key.fileobj:  # type: ignore[union-attr]
                    q.put(line.decode(errors="replace").rstrip())
            break
    sel.close()
    q.put(None)  # sentinel


def start_k6(script: str, write_url: str) -> None:
    cmd = [
        "podman", "run", "--rm", "-i",
        "--platform", "linux/amd64",
        "-p", "6565:6565",
        "-e", f"K6_PROMETHEUS_RW_SERVER_URL={write_url}",
        K6_IMAGE,
        "run", "--address=0.0.0.0:6565", "--out=xk6-prometheus-rw", "-",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Write script to stdin then close
    assert proc.stdin
    proc.stdin.write(script.encode())
    proc.stdin.close()

    st.session_state["process"] = proc
    st.session_state["logs"] = deque(maxlen=LOG_MAXLEN)
    q: queue.Queue = st.session_state["log_queue"]
    # Drain stale items
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(target=_log_reader, args=(proc, q), daemon=True)
    t.start()
    st.session_state["reader_thread"] = t


def stop_k6() -> None:
    proc: subprocess.Popen | None = st.session_state.get("process")
    if proc and proc.poll() is None:
        try:
            requests.patch(
                f"{K6_API}/status",
                json={"data": {"type": "status", "id": "default", "attributes": {"stopped": True}}},
                timeout=2,
            )
            time.sleep(1)
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
    st.session_state["process"] = None


# ---------------------------------------------------------------------------
# k6 REST API helpers
# ---------------------------------------------------------------------------


def k6_get_status() -> dict | None:
    try:
        r = requests.get(f"{K6_API}/status", timeout=2)
        r.raise_for_status()
        return r.json()["data"]["attributes"]
    except Exception:
        return None


def k6_patch_status(attrs: dict) -> None:
    try:
        requests.patch(
            f"{K6_API}/status",
            json={"data": {"type": "status", "id": "default", "attributes": attrs}},
            timeout=2,
        )
    except Exception:
        pass


def k6_get_metrics() -> list[dict] | None:
    try:
        r = requests.get(f"{K6_API}/metrics", timeout=2)
        r.raise_for_status()
        return r.json()["data"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Prometheus read helpers
# ---------------------------------------------------------------------------


def prom_query_range(base_url: str, metric_name: str) -> list[dict] | None:
    end = int(time.time())
    start = end - 300  # last 5 min
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/api/v1/query_range",
            params={
                "query": f'{{__name__=~"k6_{metric_name}.*"}}',
                "start": start,
                "end": end,
                "step": "15",
            },
            timeout=5,
        )
        r.raise_for_status()
        results = r.json()["data"]["result"]
        if not results:
            return []
        rows = []
        for series in results:
            labels = series["metric"]
            values = series["values"]
            if values:
                ts, val = values[-1]
                row = {**labels, "timestamp": ts, "value": float(val)}
                rows.append(row)
        return rows
    except Exception as e:
        st.warning(f"Prometheus query failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Executor settings UI
# ---------------------------------------------------------------------------


def _stages_editor(key: str, default: list[dict]) -> list[dict]:
    edited = st.data_editor(
        default,
        num_rows="dynamic",
        key=key,
        column_config={
            "duration": st.column_config.TextColumn("Duration", help='e.g. "30s"'),
            "target": st.column_config.NumberColumn("Target VUs", min_value=0, step=1),
        },
        use_container_width=True,
    )
    return edited


def executor_settings_ui(executor: str) -> dict:
    settings: dict[str, Any] = {}

    if executor == "constant-vus":
        settings["vus"] = st.slider("VUs", 1, 500, 10, key="cvus_vus")
        settings["duration"] = st.text_input("Duration", "30s", key="cvus_dur")

    elif executor == "ramping-vus":
        st.caption("Stages (duration + target VUs)")
        default_stages = [
            {"duration": "10s", "target": 10},
            {"duration": "30s", "target": 50},
            {"duration": "10s", "target": 0},
        ]
        settings["stages"] = _stages_editor("rvus_stages", default_stages)
        settings["gracefulRampDown"] = st.text_input("Graceful ramp-down", "30s", key="rvus_grd")

    elif executor == "constant-arrival-rate":
        settings["rate"] = st.slider("Rate (iterations/timeUnit)", 1, 1000, 10, key="car_rate")
        settings["timeUnit"] = st.text_input("Time unit", "1s", key="car_tu")
        settings["duration"] = st.text_input("Duration", "30s", key="car_dur")
        settings["preAllocatedVUs"] = st.slider("Pre-allocated VUs", 1, 200, 10, key="car_pavus")
        settings["maxVUs"] = st.slider("Max VUs", 1, 500, 50, key="car_maxvus")

    elif executor == "ramping-arrival-rate":
        st.caption("Stages (duration + target rate)")
        default_stages = [
            {"duration": "10s", "target": 10},
            {"duration": "30s", "target": 100},
            {"duration": "10s", "target": 0},
        ]
        settings["stages"] = _stages_editor("rar_stages", default_stages)
        settings["preAllocatedVUs"] = st.slider("Pre-allocated VUs", 1, 200, 10, key="rar_pavus")
        settings["maxVUs"] = st.slider("Max VUs", 1, 500, 50, key="rar_maxvus")

    elif executor == "per-vu-iterations":
        settings["vus"] = st.slider("VUs", 1, 500, 10, key="pvi_vus")
        settings["iterations"] = st.number_input("Iterations per VU", 1, 100000, 100, key="pvi_iters")
        settings["maxDuration"] = st.text_input("Max duration", "10m", key="pvi_maxdur")

    elif executor == "shared-iterations":
        settings["vus"] = st.slider("VUs", 1, 500, 10, key="si_vus")
        settings["iterations"] = st.number_input("Total iterations", 1, 1000000, 1000, key="si_iters")
        settings["maxDuration"] = st.text_input("Max duration", "10m", key="si_maxdur")

    elif executor == "externally-controlled":
        settings["vus"] = st.slider("Initial VUs", 0, 500, 1, key="ec_vus")
        settings["maxVUs"] = st.slider("Max VUs", 1, 500, 100, key="ec_maxvus")
        settings["duration"] = st.text_input("Duration (blank = indefinite)", "10m", key="ec_dur")

    return settings


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Breaking Bench",
        page_icon=":bar_chart:",
        layout="wide",
    )
    st.title("Breaking Bench")

    _init_state()

    # ------------------------------------------------------------------
    # Sidebar — global config
    # ------------------------------------------------------------------
    with st.sidebar:
        st.header("Configuration")
        write_url = st.text_input(
            "Write URL (PRW endpoint)",
            "http://localhost:8428/api/v1/write",
            key="write_url",
        )
        read_url = st.text_input(
            "Read URL (Prometheus base)",
            "http://localhost:8428",
            key="read_url",
        )
        metric_name = st.text_input("Metric name", "generated_metric", key="metric_name")
        num_labels = st.slider("Number of labels", 1, 20, 3, key="num_labels")

        st.divider()
        st.header("Executor")
        executor = st.selectbox("Executor type", K6_EXECUTORS, key="executor")

    # ------------------------------------------------------------------
    # Left column: executor settings + run control
    # Right column: status + metrics + logs
    # ------------------------------------------------------------------
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("Executor settings")
        settings = executor_settings_ui(executor)  # type: ignore[arg-type]

        st.divider()

        proc: subprocess.Popen | None = st.session_state.get("process")
        running = proc is not None and proc.poll() is None

        if not running:
            if st.button("Start k6", type="primary", use_container_width=True):
                script = build_k6_script(executor, settings, metric_name, num_labels)  # type: ignore[arg-type]
                with st.expander("Generated k6 script"):
                    st.code(script, language="javascript")
                start_k6(script, write_url)
                st.rerun()
        else:
            st.success("k6 running")
            if st.button("Stop k6", type="secondary", use_container_width=True):
                stop_k6()
                st.rerun()

            # Live VU control (externally-controlled or any running test)
            st.subheader("Live controls")
            status = k6_get_status()
            if status:
                paused = status.get("paused", False)
                current_vus = status.get("vus", 1)

                bcol1, bcol2 = st.columns(2)
                with bcol1:
                    if paused:
                        if st.button("Resume", use_container_width=True):
                            k6_patch_status({"paused": False})
                            st.rerun()
                    else:
                        if st.button("Pause", use_container_width=True):
                            k6_patch_status({"paused": True})
                            st.rerun()
                with bcol2:
                    new_vus = st.number_input(
                        "VUs", min_value=0, max_value=1000, value=current_vus, key="live_vus"
                    )
                    if new_vus != current_vus:
                        if st.button("Apply VUs"):
                            k6_patch_status({"vus": new_vus})
                            st.rerun()

    with col_right:
        # Drain log queue into session state log buffer
        q: queue.Queue = st.session_state["log_queue"]
        while True:
            try:
                line = q.get_nowait()
                if line is None:
                    # process finished
                    if st.session_state.get("process"):
                        st.session_state["process"] = None
                else:
                    st.session_state["logs"].append(line)
            except queue.Empty:
                break

        # ------------------------------------------------------------------
        # k6 status panel
        # ------------------------------------------------------------------
        st.subheader("k6 Status")
        status = k6_get_status()
        if status:
            sc1, sc2, sc3, sc4 = st.columns(4)
            sc1.metric("Running", "Yes" if status.get("running") else "No")
            sc2.metric("Paused", "Yes" if status.get("paused") else "No")
            sc3.metric("VUs", status.get("vus", "—"))
            sc4.metric("Tainted", "Yes" if status.get("tainted") else "No")
        else:
            st.info("k6 API not reachable (is k6 running?)")

        # ------------------------------------------------------------------
        # k6 Metrics
        # ------------------------------------------------------------------
        st.subheader("k6 Metrics")
        metrics = k6_get_metrics()
        if metrics:
            rows = []
            for m in metrics:
                attrs = m.get("attributes", {})
                sample = attrs.get("sample", {})
                row = {
                    "metric": m["id"],
                    "type": attrs.get("type"),
                    "contains": attrs.get("contains"),
                }
                row.update({k: round(v, 3) if isinstance(v, float) else v for k, v in sample.items()})
                rows.append(row)
            st.dataframe(rows, use_container_width=True)
        else:
            st.info("No metrics yet.")

        # ------------------------------------------------------------------
        # Prometheus read
        # ------------------------------------------------------------------
        st.subheader("Prometheus — written metrics (last 5 min)")
        df_prom = prom_query_range(read_url, metric_name)
        if df_prom is not None:
            if not df_prom:
                st.info("No series found yet.")
            else:
                st.dataframe(df_prom, use_container_width=True)

        # ------------------------------------------------------------------
        # Logs
        # ------------------------------------------------------------------
        st.subheader("k6 Logs")
        log_text = "\n".join(st.session_state["logs"]) or "(no output yet)"
        st.code(log_text, language="text")

    # ------------------------------------------------------------------
    # Auto-refresh while k6 is running
    # ------------------------------------------------------------------
    proc = st.session_state.get("process")
    if proc is not None and proc.poll() is None:
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
