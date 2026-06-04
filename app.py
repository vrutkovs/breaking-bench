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
K6_API = "http://localhost:6565/v1"
K6_CONTAINER = "breaking-bench-k6"
LOG_MAXLEN = 300

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "running": False,
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


_TEMPLATE_PATH = Path(__file__).parent / "k6_script.js.j2"
_JINJA_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_PATH.parent)),
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def build_k6_script(
    write_url: str,
    select_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    insert_rate: int,
    select_rate: int,
    insert_vus: int,
    select_vus: int,
) -> str:
    tmpl = _JINJA_ENV.get_template(_TEMPLATE_PATH.name)
    return tmpl.render(
        write_url=write_url,
        select_url=select_url,
        metric_name=metric_name,
        num_metrics=num_metrics,
        num_labels=num_labels,
        insert_rate=insert_rate,
        select_rate=select_rate,
        insert_vus=insert_vus,
        select_vus=select_vus,
    )


# ---------------------------------------------------------------------------
# Podman runner
# ---------------------------------------------------------------------------


def _log_reader(q: queue.Queue) -> None:
    """Stream logs from detached container into queue until container exits."""
    proc = subprocess.Popen(
        ["podman", "logs", "-f", K6_CONTAINER],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout
    for line in proc.stdout:
        q.put(line.decode(errors="replace").rstrip())
    proc.wait()
    q.put(None)  # sentinel


def start_k6(script: str, write_url: str) -> None:
    # Write script to a temp file mounted into the container
    tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    tmp.write(script.encode())
    tmp.flush()
    tmp.close()

    subprocess.run(["podman", "rm", "-f", K6_CONTAINER], capture_output=True)

    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--name",
            K6_CONTAINER,
            "--platform",
            "linux/amd64",
            "-p",
            "6565:6565",
            "-e", f"K6_PROMETHEUS_RW_SERVER_URL={write_url}",
            "-v",
            f"{tmp.name}:/script.js:ro",
            K6_IMAGE,
            "run",
            "--address=0.0.0.0:6565",
            "--out=xk6-prometheus-rw",
            "/script.js",
        ],
        check=True,
    )

    st.session_state["running"] = True
    st.session_state["logs"] = deque(maxlen=LOG_MAXLEN)
    q: queue.Queue = st.session_state["log_queue"]
    while not q.empty():
        try:
            q.get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(target=_log_reader, args=(q,), daemon=True)
    t.start()
    st.session_state["reader_thread"] = t


def stop_k6() -> None:
    subprocess.run(["podman", "rm", "-f", K6_CONTAINER], capture_output=True)
    st.session_state["running"] = False


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
        select_url = st.text_input(
            "Select URL (query_range endpoint)",
            "http://localhost:8428/api/v1/query_range",
            key="select_url",
        )
        metric_name = st.text_input("Metric name", "k6_metric", key="metric_name")
        num_metrics = st.slider("Number of metric variants", 1, 20, 10, key="num_metrics")
        num_labels = st.slider("Extra labels per series", 0, 10, 0, key="num_labels")

        st.divider()
        st.header("Insert load")
        insert_rate = st.slider("Insert rate (req/s)", 1, 10000, 1000, key="insert_rate")
        insert_vus = st.slider("Insert VUs", 1, 500, 50, key="insert_vus")

        st.divider()
        st.header("Select load")
        select_rate = st.slider("Select rate (req/s)", 1, 5000, 200, key="select_rate")
        select_vus = st.slider("Select VUs", 1, 500, 50, key="select_vus")

    # ------------------------------------------------------------------
    # Left column: run control
    # Right column: status + logs
    # ------------------------------------------------------------------
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        running: bool = st.session_state.get("running", False)

        if not running:
            if st.button("Start k6", type="primary", use_container_width=True):
                script = build_k6_script(
                    write_url, select_url, metric_name, num_metrics,
                    num_labels, insert_rate, select_rate, insert_vus, select_vus,
                )
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
                        "VUs",
                        min_value=0,
                        max_value=1000,
                        value=current_vus,
                        key="live_vus",
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
                    # container exited
                    st.session_state["running"] = False
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
        # Logs
        # ------------------------------------------------------------------
        st.subheader("k6 Logs")
        log_text = "\n".join(st.session_state["logs"]) or "(no output yet)"
        st.code(log_text, language="text")

    # ------------------------------------------------------------------
    # Auto-refresh while k6 is running
    # ------------------------------------------------------------------
    if st.session_state.get("running"):
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
