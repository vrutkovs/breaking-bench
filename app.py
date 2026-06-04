"""Breaking Bench — k6 load test controller."""

from __future__ import annotations

import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import jinja2
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

K6_IMAGE = "quay.io/vrutkovs/k6-with-prw-extension:v2.0.0"
K6_INSERT_CONTAINER = "breaking-bench-k6-insert"
K6_SELECT_CONTAINER = "breaking-bench-k6-select"
K6_INSERT_PORT = 6565
K6_SELECT_PORT = 6566
K6_INSERT_API = f"http://localhost:{K6_INSERT_PORT}/v1"
K6_SELECT_API = f"http://localhost:{K6_SELECT_PORT}/v1"
LOG_MAXLEN = 300

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "insert_running": False,
        "select_running": False,
        "insert_logs": deque(maxlen=LOG_MAXLEN),
        "select_logs": deque(maxlen=LOG_MAXLEN),
        "insert_log_queue": queue.Queue(),
        "select_log_queue": queue.Queue(),
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ---------------------------------------------------------------------------
# k6 script builder
# ---------------------------------------------------------------------------


_TEMPLATE_PATH = Path(__file__).parent / "k6_script.js.j2"
_JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_PATH.parent)),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def build_k6_script(
    mode: str,
    write_url: str,
    select_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
    max_vus: int,
) -> str:
    tmpl = _JINJA_ENV.get_template(_TEMPLATE_PATH.name)
    return tmpl.render(
        mode=mode,
        write_url=write_url,
        select_url=select_url,
        metric_name=metric_name,
        num_metrics=num_metrics,
        num_labels=num_labels,
        vus=vus,
        max_vus=max_vus,
    )


# ---------------------------------------------------------------------------
# Podman runner
# ---------------------------------------------------------------------------


def _log_reader(container: str, q: queue.Queue, state_key: str) -> None:
    proc = subprocess.Popen(
        ["podman", "logs", "-f", container],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout
    for line in proc.stdout:
        q.put(line.decode(errors="replace").rstrip())
    proc.wait()
    q.put(None)  # sentinel


def start_k6(mode: str, script: str, write_url: str) -> None:
    container = K6_INSERT_CONTAINER if mode == "insert" else K6_SELECT_CONTAINER
    port = K6_INSERT_PORT if mode == "insert" else K6_SELECT_PORT
    log_queue_key = f"{mode}_log_queue"
    running_key = f"{mode}_running"
    logs_key = f"{mode}_logs"

    tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    tmp.write(script.encode())
    tmp.flush()
    tmp.close()

    subprocess.run(["podman", "rm", "-f", container], capture_output=True)
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{port}:{port}",
            "-e",
            f"K6_PROMETHEUS_RW_SERVER_URL={write_url}",
            "-v",
            f"{tmp.name}:/script.js:ro",
            K6_IMAGE,
            "run",
            f"--address=0.0.0.0:{port}",
            "--out=xk6-prometheus-rw",
            "--tag",
            f"mode={mode}",
            "/script.js",
        ],
        check=True,
    )

    st.session_state[running_key] = True
    st.session_state[logs_key] = deque(maxlen=LOG_MAXLEN)
    q: queue.Queue = st.session_state[log_queue_key]
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(
        target=_log_reader, args=(container, q, running_key), daemon=True
    )
    t.start()


def stop_k6(mode: str) -> None:
    container = K6_INSERT_CONTAINER if mode == "insert" else K6_SELECT_CONTAINER
    subprocess.run(["podman", "rm", "-f", container], capture_output=True)
    st.session_state[f"{mode}_running"] = False


# ---------------------------------------------------------------------------
# k6 REST API helpers
# ---------------------------------------------------------------------------


def k6_get_status(api: str) -> dict | None:
    try:
        r = requests.get(f"{api}/status", timeout=2)
        r.raise_for_status()
        return r.json()["data"]["attributes"]
    except Exception:
        return None


def k6_patch_status(api: str, attrs: dict) -> None:
    try:
        requests.patch(
            f"{api}/status",
            json={"data": {"type": "status", "id": "default", "attributes": attrs}},
            timeout=2,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _drain_logs(mode: str) -> None:
    q: queue.Queue = st.session_state[f"{mode}_log_queue"]
    while True:
        try:
            line = q.get_nowait()
            if line is None:
                st.session_state[f"{mode}_running"] = False
            else:
                st.session_state[f"{mode}_logs"].append(line)
        except queue.Empty:
            break


def _live_controls(mode: str, api: str) -> None:
    status = k6_get_status(api)
    if not status:
        st.info("k6 API not reachable")
        return
    paused = status.get("paused", False)
    current_vus = status.get("vus", 1)
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        label = "Resume" if paused else "Pause"
        if st.button(label, key=f"{mode}_pause", use_container_width=True):
            k6_patch_status(api, {"paused": not paused})
            st.rerun()
    with bcol2:
        new_vus = st.number_input(
            "VUs",
            min_value=0,
            max_value=10000,
            value=current_vus,
            key=f"{mode}_live_vus",
        )
        if new_vus != current_vus:
            if st.button("Apply VUs", key=f"{mode}_apply_vus"):
                k6_patch_status(api, {"vus": new_vus})
                st.rerun()


def _scenario_panel(
    mode: str,
    api: str,
    write_url: str,
    select_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
    max_vus: int,
) -> None:
    _drain_logs(mode)
    running: bool = st.session_state.get(f"{mode}_running", False)

    if not running:
        if st.button(
            f"Start {mode}",
            type="primary",
            use_container_width=True,
            key=f"start_{mode}",
        ):
            script = build_k6_script(
                mode,
                write_url,
                select_url,
                metric_name,
                num_metrics,
                num_labels,
                vus,
                max_vus,
            )
            with st.expander("Generated k6 script"):
                st.code(script, language="javascript")
            start_k6(mode, script, write_url)
            st.rerun()
    else:
        st.success(f"{mode} running")
        if st.button(
            f"Stop {mode}",
            type="secondary",
            use_container_width=True,
            key=f"stop_{mode}",
        ):
            stop_k6(mode)
            st.rerun()
        _live_controls(mode, api)

    st.subheader("Logs")
    log_text = "\n".join(st.session_state[f"{mode}_logs"]) or "(no output yet)"
    st.code(log_text, language="text")


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

    with st.sidebar:
        st.header("Configuration")
        write_url = st.text_input(
            "Write URL (PRW endpoint)",
            "http://localhost:8428/api/v1/write",
            key="write_url",
        )
        select_url = st.text_input(
            "Select URL (query_range endpoint)",
            "http://localhost:8428/api/v1/query_range",
            key="select_url",
        )
        metric_name = st.text_input("Metric name", "k6_metric", key="metric_name")
        num_metrics = st.slider("Metric variants", 1, 20, 10, key="num_metrics")
        num_labels = st.slider("Extra labels", 0, 10, 0, key="num_labels")

        st.divider()
        st.header("Insert")
        insert_vus = st.slider("Insert VUs", 0, 500, 10, key="insert_vus")
        insert_max_vus = st.slider("Insert max VUs", 1, 500, 100, key="insert_max_vus")

        st.divider()
        st.header("Select")
        select_vus = st.slider("Select VUs", 0, 500, 5, key="select_vus")
        select_max_vus = st.slider("Select max VUs", 1, 500, 50, key="select_max_vus")

    col_insert, col_select = st.columns(2, gap="large")

    with col_insert:
        st.subheader("Insert")
        _scenario_panel(
            "insert",
            K6_INSERT_API,
            write_url,
            select_url,
            metric_name,
            num_metrics,
            num_labels,
            insert_vus,
            insert_max_vus,
        )

    with col_select:
        st.subheader("Select")
        _scenario_panel(
            "select",
            K6_SELECT_API,
            write_url,
            select_url,
            metric_name,
            num_metrics,
            num_labels,
            select_vus,
            select_max_vus,
        )

    if st.session_state.get("insert_running") or st.session_state.get("select_running"):
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
