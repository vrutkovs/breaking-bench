"""Breaking Bench — k6 load test controller."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import jinja2
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

K6_IMAGE = "docker.io/grafana/k6:1.7.1"
K6_INSERT_POD = "breaking-bench-k6-insert"
K6_SELECT_POD = "breaking-bench-k6-select"
K6_OPERATOR_BUNDLE = (
    "https://github.com/grafana/k6-operator/releases/latest/download/bundle.yaml"
)

WRITE_URL_DEFAULT = (
    "http://vminsert.192.168.1.254.nip.io/insert/0/prometheus/api/v1/write"
)
SELECT_URL_DEFAULT = (
    "http://vmselect.192.168.1.254.nip.io/select/0/prometheus/api/v1/query_range"
)
METRICS_URL_DEFAULT = (
    "http://vminsert.192.168.1.254.nip.io/insert/0/prometheus/api/v1/write"
)
INSERT_RPS_DEFAULT = 1
INSERT_TIMEOUT_DEFAULT = "30s"
INSERT_MAX_VUS_DEFAULT = 50
INSERT_MAX_VUS_SLIDER_MAX = 2000
SELECT_TIMEOUT_DEFAULT = "30s"
SELECT_MAX_VUS_DEFAULT = 50
SELECT_MAX_VUS_SLIDER_MAX = 500
SELECT_FAST_RPS_DEFAULT = 1
SELECT_SLOW_RPS_DEFAULT = 1
INSERT_RPS_SLIDER_MAX = 3000
SELECT_FAST_RPS_SLIDER_MAX = 50
SELECT_SLOW_RPS_SLIDER_MAX = 5
CARDINALITY_DEFAULT = 1
CARDINALITY_SLIDER_MAX = 100
INSERT_REPLICAS_DEFAULT = 1
SELECT_REPLICAS_DEFAULT = 1
REPLICAS_SLIDER_MAX = 20
RUNTIME_K8S = "Kubernetes pod"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--k8s-namespace",
        default="default",
        help="Kubernetes namespace for k6 pods",
    )
    return parser.parse_args()


ARGS = _parse_args()
RUNTIME = RUNTIME_K8S
K8S_NAMESPACE = ARGS.k8s_namespace

# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _init_state() -> None:
    defaults: dict[str, Any] = {
        "insert_running": False,
        "select_running": False,
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
_TESTRUN_TEMPLATE_PATH = Path(__file__).parent / "k6_testrun.yaml.j2"
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
    metrics_url: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    cardinality: int,
    insert_timeout: str,
    select_timeout: str,
    insert_max_vus: int,
    select_max_vus: int,
    fast_rps: int,
    slow_rps: int | None = None,
) -> str:
    fast_rps = fast_rps if fast_rps else 1
    slow_rps = (slow_rps if slow_rps is not None else fast_rps) or 1
    tmpl = _JINJA_ENV.get_template(_TEMPLATE_PATH.name)
    return tmpl.render(
        mode=mode,
        write_url=write_url,
        select_url=select_url,
        metrics_url=metrics_url,
        metric_name=metric_name,
        num_metrics=num_metrics,
        num_labels=num_labels,
        cardinality=cardinality,
        insert_timeout=insert_timeout,
        select_timeout=select_timeout,
        fast_rps=fast_rps,
        slow_rps=slow_rps,
        maxVUs=max(1, insert_max_vus),
        fastMaxVUs=max(1, select_max_vus),
        slowMaxVUs=max(1, select_max_vus),
    )


def build_k6_testrun_manifest(
    mode: str,
    name: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    parallelism: int,
) -> str:
    tmpl = _JINJA_ENV.get_template(_TESTRUN_TEMPLATE_PATH.name)
    return tmpl.render(
        mode=mode,
        name=name,
        image=K6_IMAGE,
        write_url=write_url,
        select_url=select_url,
        metrics_url=metrics_url,
        parallelism=parallelism,
    )


def _script_config(
    metric_name: str, num_metrics: int, num_labels: int
) -> tuple[str, int, int]:
    return metric_name, num_metrics, num_labels


def _workload_config(
    runtime: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    cardinality: int,
    insert_timeout: str,
    select_timeout: str,
    insert_max_vus: int,
    select_max_vus: int,
    replicas: int,
    fast_rps: int,
    slow_rps: int | None = None,
) -> tuple[Any, ...]:
    return (
        runtime,
        namespace,
        write_url,
        select_url,
        metrics_url,
        metric_name,
        num_metrics,
        num_labels,
        cardinality,
        insert_timeout,
        select_timeout,
        insert_max_vus,
        select_max_vus,
        replicas,
        fast_rps,
        slow_rps,
    )


def _log_recreate(
    action: str,
    runtime: str,
    mode: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    cardinality: int,
    insert_timeout: str,
    select_timeout: str,
    insert_max_vus: int,
    select_max_vus: int,
    replicas: int,
    fast_rps: int,
    slow_rps: int | None = None,
) -> None:
    entry: dict[str, Any] = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "runtime": runtime,
        "mode": mode,
        "namespace": namespace,
        "write_url": write_url,
        "select_url": select_url,
        "metrics_url": metrics_url,
        "metric_name": metric_name,
        "metric_variants": num_metrics,
        "extra_labels": num_labels,
        "cardinality": cardinality,
        "insert_timeout": insert_timeout,
        "select_timeout": select_timeout,
        "insert_max_vus": insert_max_vus,
        "select_max_vus": select_max_vus,
        "replicas": replicas,
        "fast_rps": fast_rps,
    }
    if mode == "select":
        entry["slow_rps"] = slow_rps
    key = f"{mode}_recreate_logs"
    logs = list(st.session_state.get(key, []))
    logs.append(entry)
    st.session_state[key] = logs[-5:]
    print(f"breaking-bench recreate params: {entry}", flush=True)


@st.cache_resource
def _ensure_k6_operator() -> str | None:
    """Install k6 operator via bundle.yaml if TestRun CRD is absent. Runs once per process.
    Returns an error message string on failure, None on success."""
    try:
        proc = subprocess.run(
            ["kubectl", "get", "crd", "testruns.k6.io"],
            capture_output=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return None
        _kubectl_run(["kubectl", "apply", "-f", K6_OPERATOR_BUNDLE])
        return None
    except Exception as exc:
        return str(exc)


def _k8s_name(mode: str) -> str:
    return K6_INSERT_POD if mode == "insert" else K6_SELECT_POD


def _kubectl_namespace_args(namespace: str) -> list[str]:
    return ["-n", namespace] if namespace else []


def _kubectl_run(args: list[str], input_data: bytes | None = None) -> bytes:
    proc = subprocess.run(args, input=input_data, capture_output=True)
    if proc.returncode == 0:
        return proc.stdout
    detail = (proc.stderr or proc.stdout).decode(errors="replace").strip()
    raise RuntimeError(f"kubectl failed: {' '.join(args)}\n{detail}")


def _kubectl_delete(namespace: str, name: str, include_configmap: bool = True) -> None:
    ns_args = _kubectl_namespace_args(namespace)
    subprocess.run(
        ["kubectl", *ns_args, "delete", "testrun", name, "--ignore-not-found"],
        capture_output=True,
    )
    if not include_configmap:
        return
    subprocess.run(
        ["kubectl", *ns_args, "delete", "configmap", name, "--ignore-not-found"],
        capture_output=True,
    )


def _kubectl_wait_deleted(
    namespace: str, kind: str, name: str, timeout_s: float = 30
) -> None:
    ns_args = _kubectl_namespace_args(namespace)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["kubectl", *ns_args, "get", kind, name],
            capture_output=True,
        )
        if proc.returncode != 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {kind}/{name} deletion")


def _apply_script_configmap(namespace: str, name: str, script: str) -> None:
    ns_args = _kubectl_namespace_args(namespace)
    tmp = tempfile.NamedTemporaryFile(suffix=".js", delete=False)
    try:
        tmp.write(script.encode())
        tmp.flush()
        tmp.close()
        configmap = _kubectl_run(
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
            ]
        )
        _kubectl_run(
            ["kubectl", *ns_args, "apply", "-f", "-"],
            input_data=configmap,
        )
    finally:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)


def start_k6_testrun(
    mode: str,
    script: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    replicas: int,
) -> None:
    name = _k8s_name(mode)
    ns_args = _kubectl_namespace_args(namespace)

    _kubectl_delete(namespace, name, include_configmap=False)
    _kubectl_wait_deleted(namespace, "testrun", name, timeout_s=60)
    _apply_script_configmap(namespace, name, script)
    manifest = build_k6_testrun_manifest(
        mode, name, write_url, select_url, metrics_url, replicas
    )
    _kubectl_run(
        ["kubectl", *ns_args, "apply", "-f", "-"],
        input_data=manifest.encode(),
    )
    st.session_state[f"{mode}_running"] = True
    st.session_state[f"{mode}_runtime"] = RUNTIME_K8S
    st.session_state[f"{mode}_namespace"] = namespace


def stop_k6_testrun(mode: str, namespace: str) -> None:
    _kubectl_delete(namespace, _k8s_name(mode))
    st.session_state[f"{mode}_running"] = False
    st.session_state[f"{mode}_script_config"] = None


def get_k6_testrun_stage(mode: str, namespace: str) -> str | None:
    ns_args = _kubectl_namespace_args(namespace)
    proc = subprocess.run(
        [
            "kubectl",
            *ns_args,
            "get",
            "testrun",
            _k8s_name(mode),
            "-o",
            "jsonpath={.status.stage}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout or None


def start_k6_workload(
    mode: str,
    script: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    replicas: int = 1,
) -> None:
    start_k6_testrun(
        mode, script, write_url, select_url, metrics_url, namespace, replicas
    )
    st.session_state[f"{mode}_runtime"] = RUNTIME_K8S
    st.session_state[f"{mode}_namespace"] = namespace


def restart_k6(
    mode: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    cardinality: int,
    insert_timeout: str,
    select_timeout: str,
    insert_max_vus: int,
    select_max_vus: int,
    replicas: int,
    fast_rps: int,
    slow_rps: int | None = None,
) -> None:
    script = build_k6_script(
        mode,
        write_url,
        select_url,
        metrics_url,
        metric_name,
        num_metrics,
        num_labels,
        cardinality,
        insert_timeout,
        select_timeout,
        insert_max_vus,
        select_max_vus,
        fast_rps,
        slow_rps,
    )
    _log_recreate(
        "recreate",
        RUNTIME_K8S,
        mode,
        write_url,
        select_url,
        metrics_url,
        namespace,
        metric_name,
        num_metrics,
        num_labels,
        cardinality,
        insert_timeout,
        select_timeout,
        insert_max_vus,
        select_max_vus,
        replicas,
        fast_rps,
        slow_rps,
    )
    start_k6_workload(
        mode, script, write_url, select_url, metrics_url, namespace, replicas
    )
    st.session_state[f"{mode}_script_config"] = _workload_config(
        RUNTIME_K8S,
        write_url,
        select_url,
        metrics_url,
        namespace,
        metric_name,
        num_metrics,
        num_labels,
        cardinality,
        insert_timeout,
        select_timeout,
        insert_max_vus,
        select_max_vus,
        replicas,
        fast_rps,
        slow_rps,
    )


def stop_k6_workload(mode: str, namespace: str) -> None:
    stop_k6_testrun(mode, namespace)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _scenario_panel(
    mode: str,
    write_url: str,
    select_url: str,
    metrics_url: str,
    namespace: str,
    metric_name: str,
    num_metrics: int,
    num_labels: int,
    cardinality: int,
    insert_timeout: str,
    select_timeout: str,
    insert_max_vus: int,
    select_max_vus: int,
    replicas: int,
    fast_rps: int,
    slow_rps: int | None = None,
) -> None:
    running: bool = st.session_state.get(f"{mode}_running", False)

    if not running:
        stage = get_k6_testrun_stage(mode, namespace)
        if stage and stage not in ("finished", "error"):
            running = True
            st.session_state[f"{mode}_running"] = True
            st.session_state[f"{mode}_namespace"] = namespace

    active_namespace: str = st.session_state.get(f"{mode}_namespace", namespace)
    script_config = _workload_config(
        RUNTIME_K8S,
        write_url,
        select_url,
        metrics_url,
        active_namespace,
        metric_name,
        num_metrics,
        num_labels,
        cardinality,
        insert_timeout,
        select_timeout,
        insert_max_vus,
        select_max_vus,
        replicas,
        fast_rps,
        slow_rps,
    )
    if running and st.session_state.get(f"{mode}_script_config") is None:
        st.session_state[f"{mode}_script_config"] = script_config

    if not running:
        active_namespace = namespace
        script_config = _workload_config(
            RUNTIME_K8S,
            write_url,
            select_url,
            metrics_url,
            namespace,
            metric_name,
            num_metrics,
            num_labels,
            cardinality,
            insert_timeout,
            select_timeout,
            insert_max_vus,
            select_max_vus,
            replicas,
            fast_rps,
            slow_rps,
        )
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
                metrics_url,
                metric_name,
                num_metrics,
                num_labels,
                cardinality,
                insert_timeout,
                select_timeout,
                insert_max_vus,
                select_max_vus,
                fast_rps,
                slow_rps,
            )
            _log_recreate(
                "start",
                RUNTIME_K8S,
                mode,
                write_url,
                select_url,
                metrics_url,
                namespace,
                metric_name,
                num_metrics,
                num_labels,
                cardinality,
                insert_timeout,
                select_timeout,
                insert_max_vus,
                select_max_vus,
                replicas,
                fast_rps,
                slow_rps,
            )
            start_k6_workload(
                mode, script, write_url, select_url, metrics_url, namespace, replicas
            )
            st.session_state[f"{mode}_script_config"] = script_config
            st.rerun()
    else:
        if st.session_state.get(f"{mode}_script_config") != script_config:
            restart_k6(
                mode,
                write_url,
                select_url,
                metrics_url,
                active_namespace,
                metric_name,
                num_metrics,
                num_labels,
                cardinality,
                insert_timeout,
                select_timeout,
                insert_max_vus,
                select_max_vus,
                replicas,
                fast_rps,
                slow_rps,
            )
            st.info(f"{mode} restarted with updated parameters")
            st.rerun()

        st.success(f"{mode} running")
        if st.button(
            f"Stop {mode}",
            type="secondary",
            use_container_width=True,
            key=f"stop_{mode}",
        ):
            stop_k6_workload(mode, active_namespace)
            st.rerun()

        stage = get_k6_testrun_stage(mode, active_namespace)
        st.caption(f"TestRun stage: {stage or 'not found'}")

        recreate_logs = st.session_state.get(f"{mode}_recreate_logs", [])
        if recreate_logs:
            with st.expander("Last job parameters"):
                st.json(recreate_logs[-1])


def _scenario_settings(mode: str) -> tuple[int, str, int, int, int | None]:
    """Render mode settings; return (fast_rps, timeout, max_vus, replicas, slow_rps)."""
    if mode == "insert":
        rps = st.slider(
            "Insert RPS",
            1,
            INSERT_RPS_SLIDER_MAX,
            INSERT_RPS_DEFAULT,
            key="insert_rps",
        )
        timeout = st.text_input(
            "Insert timeout (e.g. 30s, 1m)",
            INSERT_TIMEOUT_DEFAULT,
            key="insert_timeout",
        )
        max_vus = st.slider(
            "Insert max VUs",
            1,
            INSERT_MAX_VUS_SLIDER_MAX,
            INSERT_MAX_VUS_DEFAULT,
            key="insert_max_vus",
        )
        replicas = st.slider(
            "Insert replicas (k8s parallelism)",
            1,
            REPLICAS_SLIDER_MAX,
            INSERT_REPLICAS_DEFAULT,
            key="insert_replicas",
        )
        return rps, timeout, max_vus, replicas, None
    else:
        fast_rps = st.slider(
            "Fast queries RPS",
            0,
            SELECT_FAST_RPS_SLIDER_MAX,
            SELECT_FAST_RPS_DEFAULT,
            key="select_fast_rps",
        )
        slow_rps = st.slider(
            "Slow queries RPS",
            0,
            SELECT_SLOW_RPS_SLIDER_MAX,
            SELECT_SLOW_RPS_DEFAULT,
            key="select_slow_rps",
        )
        timeout = st.text_input(
            "Select timeout (e.g. 30s, 1m)",
            SELECT_TIMEOUT_DEFAULT,
            key="select_timeout",
        )
        max_vus = st.slider(
            "Select max VUs",
            1,
            SELECT_MAX_VUS_SLIDER_MAX,
            SELECT_MAX_VUS_DEFAULT,
            key="select_max_vus",
        )
        replicas = st.slider(
            "Select replicas (k8s parallelism)",
            1,
            REPLICAS_SLIDER_MAX,
            SELECT_REPLICAS_DEFAULT,
            key="select_replicas",
        )
        return fast_rps, timeout, max_vus, replicas, slow_rps


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
    if err := _ensure_k6_operator():
        st.warning(f"k6 operator check failed: {err}")

    # --- Main area: settings ---
    st.subheader("Shared settings")
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
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
        metrics_url = st.text_input(
            "Metrics URL (k6 metrics PRW endpoint)",
            METRICS_URL_DEFAULT,
            key="metrics_url",
        )
    with sc2:
        metric_name = st.text_input("Metric name prefix", "test", key="metric_name")
        num_metrics = st.slider("Metric variants", 1, 20, 1, key="num_metrics")
        num_labels = st.slider("Extra labels", 0, 10, 0, key="num_labels")
    with sc3:
        cardinality = st.slider(
            "Cardinality (distinct label values)",
            1,
            CARDINALITY_SLIDER_MAX,
            CARDINALITY_DEFAULT,
            key="cardinality",
        )

    st.divider()
    col_insert, col_select = st.columns(2, gap="large")

    with col_insert:
        st.subheader("Insert settings")
        insert_rps, insert_timeout, insert_max_vus, insert_replicas, _ = (
            _scenario_settings("insert")
        )

    with col_select:
        st.subheader("Select settings")
        (
            select_fast_rps,
            select_timeout,
            select_max_vus,
            select_replicas,
            select_slow_rps,
        ) = _scenario_settings("select")

    # --- Sidebar: buttons and status ---
    with st.sidebar:
        st.caption(f"Runner: {RUNTIME}")
        st.caption(f"Namespace: {K8S_NAMESPACE}")
        st.divider()
        st.subheader("Insert")
        _scenario_panel(
            "insert",
            write_url,
            select_url,
            metrics_url,
            K8S_NAMESPACE,
            metric_name,
            num_metrics,
            num_labels,
            cardinality,
            insert_timeout,
            select_timeout,
            insert_max_vus,
            select_max_vus,
            insert_replicas,
            insert_rps,
        )
        st.divider()
        st.subheader("Select")
        _scenario_panel(
            "select",
            write_url,
            select_url,
            metrics_url,
            K8S_NAMESPACE,
            metric_name,
            num_metrics,
            num_labels,
            cardinality,
            insert_timeout,
            select_timeout,
            insert_max_vus,
            select_max_vus,
            select_replicas,
            select_fast_rps,
            select_slow_rps,
        )

    if st.session_state.get("insert_running") or st.session_state.get("select_running"):
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
