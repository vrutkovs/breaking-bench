"""Breaking Bench — k6 load test controller."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
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
K6_INSERT_POD = "breaking-bench-k6-insert"
K6_SELECT_POD = "breaking-bench-k6-select"
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
INSERT_VUS_SLIDER_MAX = 500
SELECT_VUS_SLIDER_MAX = 10
RUNTIME_PODMAN = "Podman"
RUNTIME_K8S = "Kubernetes pod"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime",
        choices=("k8s", "podman"),
        default="k8s",
        help="k6 runner backend",
    )
    parser.add_argument(
        "--k8s-namespace",
        default="default",
        help="Kubernetes namespace for k6 pods",
    )
    return parser.parse_args()


ARGS = _parse_args()
RUNTIME = RUNTIME_K8S if ARGS.runtime == "k8s" else RUNTIME_PODMAN
K8S_NAMESPACE = ARGS.k8s_namespace

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "insert_running": False,
        "select_running": False,
        "insert_runtime": RUNTIME,
        "select_runtime": RUNTIME,
        "insert_namespace": K8S_NAMESPACE,
        "select_namespace": K8S_NAMESPACE,
        "insert_recreate_logs": [],
        "select_recreate_logs": [],
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
_POD_TEMPLATE_PATH = Path(__file__).parent / "k6_pod.yaml.j2"
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
        maxVUs=INSERT_VUS_SLIDER_MAX if mode == "insert" else SELECT_VUS_SLIDER_MAX,
    )


def build_k6_pod_manifest(
    mode: str,
    name: str,
    write_url: str,
    select_url: str,
) -> str:
    tmpl = _JINJA_ENV.get_template(_POD_TEMPLATE_PATH.name)
    return tmpl.render(
        mode=mode,
        name=name,
        image=K6_IMAGE,
        write_url=write_url,
        select_url=select_url,
    )


def _script_config(
    metric_name: str, num_metrics: int, num_labels: int
) -> tuple[str, int, int]:
    return metric_name, num_metrics, num_labels


def _log_recreate(
    action: str,
    runtime: str,
    mode: str,
    write_url: str,
    select_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
) -> None:
    entry: dict[str, Any] = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "runtime": runtime,
        "mode": mode,
        "namespace": namespace if runtime == RUNTIME_K8S else "",
        "write_url": write_url,
        "select_url": select_url,
        "metric_name": metric_name,
        "metric_variants": num_metrics,
        "extra_labels": num_labels,
        "vus": vus,
    }
    key = f"{mode}_recreate_logs"
    logs = list(st.session_state.get(key, []))
    logs.append(entry)
    st.session_state[key] = logs[-5:]
    print(f"breaking-bench recreate params: {entry}", flush=True)


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
            "-e",
            "K6_PROMETHEUS_RW_TREND_STATS=p(99),p(95),avg,sum",
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


def _k8s_name(mode: str) -> str:
    return K6_INSERT_POD if mode == "insert" else K6_SELECT_POD


def _kubectl_namespace_args(namespace: str) -> list[str]:
    return ["-n", namespace] if namespace else []


def _kubectl_delete(namespace: str, name: str) -> None:
    ns_args = _kubectl_namespace_args(namespace)
    subprocess.run(
        ["kubectl", *ns_args, "delete", "pod", name, "--ignore-not-found"],
        capture_output=True,
    )
    subprocess.run(
        ["kubectl", *ns_args, "delete", "configmap", name, "--ignore-not-found"],
        capture_output=True,
    )


def _apply_script_configmap(namespace: str, name: str, script: str) -> None:
    ns_args = _kubectl_namespace_args(namespace)
    tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    try:
        tmp.write(script.encode())
        tmp.flush()
        tmp.close()
        configmap = subprocess.run(
            [
                "kubectl",
                *ns_args,
                "create",
                "configmap",
                name,
                f"--from-file=script.js={tmp.name}",
                "--dry-run=client",
                "-o",
                "yaml",
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["kubectl", *ns_args, "apply", "-f", "-"],
            input=configmap.stdout,
            check=True,
        )
    finally:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)


def start_k6_pod(
    mode: str,
    script: str,
    write_url: str,
    select_url: str,
    namespace: str,
) -> None:
    name = _k8s_name(mode)
    ns_args = _kubectl_namespace_args(namespace)

    _kubectl_delete(namespace, name)
    _apply_script_configmap(namespace, name, script)
    manifest = build_k6_pod_manifest(mode, name, write_url, select_url)
    subprocess.run(
        ["kubectl", *ns_args, "apply", "-f", "-"],
        input=manifest.encode(),
        check=True,
    )
    st.session_state[f"{mode}_running"] = True
    st.session_state[f"{mode}_runtime"] = RUNTIME_K8S
    st.session_state[f"{mode}_namespace"] = namespace


def stop_k6_pod(mode: str, namespace: str) -> None:
    _kubectl_delete(namespace, _k8s_name(mode))
    st.session_state[f"{mode}_running"] = False
    st.session_state[f"{mode}_script_config"] = None


def get_k6_pod_phase(mode: str, namespace: str) -> str | None:
    ns_args = _kubectl_namespace_args(namespace)
    proc = subprocess.run(
        [
            "kubectl",
            *ns_args,
            "get",
            "pod",
            _k8s_name(mode),
            "-o",
            "jsonpath={.status.phase}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def start_k6_workload(
    runtime: str,
    mode: str,
    script: str,
    write_url: str,
    select_url: str,
    namespace: str,
) -> None:
    if runtime == RUNTIME_K8S:
        start_k6_pod(mode, script, write_url, select_url, namespace)
    else:
        start_k6(mode, script, write_url, select_url)
    st.session_state[f"{mode}_runtime"] = runtime
    st.session_state[f"{mode}_namespace"] = namespace


def restart_k6(
    runtime: str,
    mode: str,
    write_url: str,
    select_url: str,
    namespace: str,
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
    _log_recreate(
        "recreate",
        runtime,
        mode,
        write_url,
        select_url,
        namespace,
        metric_name,
        num_metrics,
        num_labels,
        vus,
    )
    start_k6_workload(runtime, mode, script, write_url, select_url, namespace)
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


def stop_k6_workload(runtime: str, mode: str, namespace: str) -> None:
    if runtime == RUNTIME_K8S:
        stop_k6_pod(mode, namespace)
    else:
        stop_k6(mode)


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
    runtime: str,
    mode: str,
    api: str,
    write_url: str,
    select_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    vus: int,
) -> None:
    running: bool = st.session_state.get(f"{mode}_running", False)
    active_runtime: str = st.session_state.get(f"{mode}_runtime", runtime)
    active_namespace: str = st.session_state.get(f"{mode}_namespace", namespace)
    script_config = _script_config(metric_name, num_metrics, num_labels)

    if not running:
        active_runtime = runtime
        active_namespace = namespace
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
            _log_recreate(
                "start",
                runtime,
                mode,
                write_url,
                select_url,
                namespace,
                metric_name,
                num_metrics,
                num_labels,
                vus,
            )
            start_k6_workload(runtime, mode, script, write_url, select_url, namespace)
            st.session_state[f"{mode}_script_config"] = script_config
            st.rerun()
    else:
        if st.session_state.get(f"{mode}_script_config") != script_config:
            restart_k6(
                active_runtime,
                mode,
                write_url,
                select_url,
                active_namespace,
                metric_name,
                num_metrics,
                num_labels,
                vus,
            )
            st.info(f"{mode} restarted with updated metric configuration")
            st.rerun()

        st.success(f"{mode} running via {active_runtime}")
        if active_runtime != runtime:
            st.info(f"Stop current {active_runtime} workload before switching runner")
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button(
                f"Stop {mode}",
                type="secondary",
                use_container_width=True,
                key=f"stop_{mode}",
            ):
                stop_k6_workload(active_runtime, mode, active_namespace)
                st.rerun()

        if active_runtime == RUNTIME_K8S:
            phase = get_k6_pod_phase(mode, active_namespace)
            st.caption(f"Pod phase: {phase or 'not found'}")
        else:
            status = k6_get_status(api)
            if status:
                paused = status.get("paused", False)
                with bcol2:
                    label = "Resume" if paused else "Pause"
                    if st.button(label, key=f"{mode}_pause", use_container_width=True):
                        k6_patch_status(api, {"paused": not paused})
                        st.rerun()

        recreate_logs = st.session_state.get(f"{mode}_recreate_logs", [])
        if recreate_logs:
            with st.expander("Last job parameters"):
                st.json(recreate_logs[-1])

    if running and active_runtime == RUNTIME_PODMAN and vus:
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
        st.caption(f"Runner: {RUNTIME}")
        if RUNTIME == RUNTIME_K8S:
            st.caption(f"Kubernetes namespace: {K8S_NAMESPACE}")
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
            INSERT_VUS_SLIDER_MAX,
            INSERT_VUS_DEFAULT,
            key="insert_vus",
        )

        st.divider()
        st.header("Select")
        select_vus = st.slider(
            "Select VUs",
            1,
            SELECT_VUS_SLIDER_MAX,
            SELECT_VUS_DEFAULT,
            key="select_vus",
        )

    col_insert, col_select = st.columns(2, gap="large")

    with col_insert:
        st.subheader("Insert")
        _scenario_panel(
            RUNTIME,
            "insert",
            K6_INSERT_API,
            write_url,
            select_url,
            K8S_NAMESPACE,
            metric_name,
            num_metrics,
            num_labels,
            insert_vus,
        )

    with col_select:
        st.subheader("Select")
        _scenario_panel(
            RUNTIME,
            "select",
            K6_SELECT_API,
            write_url,
            select_url,
            K8S_NAMESPACE,
            metric_name,
            num_metrics,
            num_labels,
            select_vus,
        )

    if st.session_state.get("insert_running") or st.session_state.get("select_running"):
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
