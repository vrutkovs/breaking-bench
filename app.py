"""Breaking Bench — k6 load test controller."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import jinja2
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# K6_IMAGE = "quay.io/vrutkovs/k6-with-prw-extension:v2.0.0"
K6_IMAGE = "docker.io/grafana/k6:1.7.1"
K6_INSERT_CONTAINER = "breaking-bench-k6-insert"
K6_SELECT_CONTAINER = "breaking-bench-k6-select"
K6_INSERT_PORT = 6565
K6_SELECT_PORT = 6566

WRITE_URL_DEFAULT = (
    "http://vminsert.192.168.1.254.nip.io/insert/0/prometheus/api/v1/write"
)
SELECT_URL_DEFAULT = (
    "http://vmselect.192.168.1.254.nip.io/select/0/prometheus/api/v1/query_range"
)
K6_INSERT_API = f"http://localhost:{K6_INSERT_PORT}/v1"
K6_SELECT_API = f"http://localhost:{K6_SELECT_PORT}/v1"

INSERT_VUS_DEFAULT = 1
SELECT_VUS_DEFAULT = 1
VUS_SLIDER_MAX = 100

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "insert_running": False,
        "select_running": False,
        "insert_script_config": None,
        "select_script_config": None,
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
        maxVUs=VUS_SLIDER_MAX,
    )


def _script_config(metric_name: str, num_metrics: int, num_labels: int) -> tuple[str, int, int]:
    return metric_name, num_metrics, num_labels


# ---------------------------------------------------------------------------
# Podman runner
# ---------------------------------------------------------------------------


def start_k6(mode: str, script: str, write_url: str, select_url: str) -> None:
    container = K6_INSERT_CONTAINER if mode == "insert" else K6_SELECT_CONTAINER
    port = K6_INSERT_PORT if mode == "insert" else K6_SELECT_PORT
    running_key = f"{mode}_running"

    tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    tmp.write(script.encode())
    tmp.flush()
    tmp.close()
    os.chmod(tmp.name, 0o644)

    subprocess.run(["podman", "rm", "-f", container], capture_output=True)
    subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--replace",
            "--name",
            container,
            "--network",
            "host",
            "-e",
            f"WRITE_URL={write_url}",
            "-e",
            f"SELECT_URL={select_url}",
            "-e",
            f"K6_PROMETHEUS_RW_SERVER_URL={write_url}",
            "-v",
            f"{tmp.name}:/script.js:ro",
            K6_IMAGE,
            "run",
            f"--address=0.0.0.0:{port}",
            "--out=experimental-prometheus-rw",
            "--tag",
            f"testid={mode}",
            "/script.js",
        ],
        check=True,
    )

    st.session_state[running_key] = True


def restart_k6(
    mode: str,
    write_url: str,
    select_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
) -> None:
    script = build_k6_script(
        mode,
        write_url,
        select_url,
        metric_name,
        num_metrics,
        num_labels,
        vus,
    )
    start_k6(mode, script, write_url, select_url)
    st.session_state[f"{mode}_script_config"] = _script_config(
        metric_name,
        num_metrics,
        num_labels,
    )


def stop_k6(mode: str) -> None:
    container = K6_INSERT_CONTAINER if mode == "insert" else K6_SELECT_CONTAINER
    subprocess.run(["podman", "rm", "-f", container], capture_output=True)
    st.session_state[f"{mode}_running"] = False
    st.session_state[f"{mode}_script_config"] = None


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
def _scenario_panel(
    mode: str,
    api: str,
    write_url: str,
    select_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
) -> None:
    running: bool = st.session_state.get(f"{mode}_running", False)
    script_config = _script_config(metric_name, num_metrics, num_labels)

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
            )
            with st.expander("Generated k6 script"):
                st.code(script, language="javascript")
            start_k6(mode, script, write_url, select_url)
            st.session_state[f"{mode}_script_config"] = script_config
            st.rerun()
    else:
        if st.session_state.get(f"{mode}_script_config") != script_config:
            restart_k6(
                mode,
                write_url,
                select_url,
                metric_name,
                num_metrics,
                num_labels,
                vus,
            )
            st.info(f"{mode} restarted with updated metric configuration")
            st.rerun()

        st.success(f"{mode} running")
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button(
                f"Stop {mode}",
                type="secondary",
                use_container_width=True,
                key=f"stop_{mode}",
            ):
                stop_k6(mode)
                st.rerun()

        status = k6_get_status(api)
        if not status:
            st.info("k6 API not reachable")
        else:
            paused = status.get("paused", False)
            with bcol2:
                label = "Resume" if paused else "Pause"
                if st.button(label, key=f"{mode}_pause", use_container_width=True):
                    k6_patch_status(api, {"paused": not paused})
                    st.rerun()

    if running and vus:
        k6_patch_status(api, {"vus": vus})


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

    st.html("""
    <style>
    .stChatMessage {
        padding-top: 0;
        padding-bottom: 0;
    }
    </style>
    """)

    with st.sidebar:
        st.header("Configuration")
        write_url = st.text_input(
            "Write URL (PRW endpoint)",
            WRITE_URL_DEFAULT,
            key="write_url",
        )
        select_url = st.text_input(
            "Select URL (query_range endpoint)",
            SELECT_URL_DEFAULT,
            key="select_url",
        )
        metric_name = st.text_input("Metric name prefix", "test_", key="metric_name")
        num_metrics = st.slider("Metric variants", 1, 20, 1, key="num_metrics")
        num_labels = st.slider("Extra labels", 0, 10, 0, key="num_labels")

        st.divider()
        st.header("Insert")
        insert_vus = st.slider(
            "Insert VUs",
            1,
            VUS_SLIDER_MAX,
            INSERT_VUS_DEFAULT,
            key="insert_vus",
        )

        st.divider()
        st.header("Select")
        select_vus = st.slider(
            "Select VUs",
            1,
            VUS_SLIDER_MAX,
            SELECT_VUS_DEFAULT,
            key="select_vus",
        )

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
        )


if __name__ == "__main__":
    main()
