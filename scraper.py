import time
import re
import requests
import json
from datetime import datetime, timezone
from collections import defaultdict
from kubernetes import client, config

# --- Helpers ---
def parse_labels(label_str):
    labels = {}
    for part in label_str.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            labels[k.strip()] = v.strip('"')
    return labels

def fetch_cadvisor_metrics():
    metrics = requests.get("http://localhost:8081/metrics").text.splitlines()
    usage = {}
    timestamp = time.time()

    for line in metrics:
        if line.startswith("container_cpu_usage_seconds_total{"):
            label_part = line.split('{', 1)[1].split('}', 1)[0]
            labels = parse_labels(label_part)
            ns = labels.get("namespace", labels.get("container_label_io_kubernetes_pod_namespace", ""))
            pod = labels.get("pod", labels.get("container_label_io_kubernetes_pod_name", ""))
            container = labels.get("container", labels.get("container_label_io_kubernetes_container_name", ""))
            if not container or container in ("POD", ""):
                continue
            value = float(line.split()[-1])
            usage[(ns, pod, container)] = {"cpu": value, "timestamp": timestamp}

        elif line.startswith("container_memory_usage_bytes{"):
            label_part = line.split('{', 1)[1].split('}', 1)[0]
            labels = parse_labels(label_part)
            ns = labels.get("namespace", labels.get("container_label_io_kubernetes_pod_namespace", ""))
            pod = labels.get("pod", labels.get("container_label_io_kubernetes_pod_name", ""))
            container = labels.get("container", labels.get("container_label_io_kubernetes_container_name", ""))
            if not container or container in ("POD", ""):
                continue
            value = float(line.split()[-1])
            key = (ns, pod, container)
            if key not in usage:
                usage[key] = {}
            usage[key]["memory"] = value / (1024 ** 2)  # MiB

    return usage

def fetch_ksm_metrics():
    return requests.get("http://localhost:8080/metrics").text.splitlines()

def parse_ksm_metrics(ksm_lines):
    resource_data = defaultdict(lambda: defaultdict(dict))
    node_totals = defaultdict(lambda: {'memory_request': 0, 'memory_limit': 0, 'cpu_request': 0, 'cpu_limit': 0})

    patterns = {
        'memory_request': re.compile(r'kube_pod_container_resource_requests{[^}]*namespace="([^"]+)",[^}]*pod="([^"]+)",[^}]*node="([^"]+)",[^}]*resource="memory"[^}]*}\s+([0-9.e+-]+)'),
        'memory_limit': re.compile(r'kube_pod_container_resource_limits{[^}]*namespace="([^"]+)",[^}]*pod="([^"]+)",[^}]*node="([^"]+)",[^}]*resource="memory"[^}]*}\s+([0-9.e+-]+)'),
        'cpu_request': re.compile(r'kube_pod_container_resource_requests{[^}]*namespace="([^"]+)",[^}]*pod="([^"]+)",[^}]*node="([^"]+)",[^}]*resource="cpu"[^}]*}\s+([0-9.e+-]+)'),
        'cpu_limit': re.compile(r'kube_pod_container_resource_limits{[^}]*namespace="([^"]+)",[^}]*pod="([^"]+)",[^}]*node="([^"]+)",[^}]*resource="cpu"[^}]*}\s+([0-9.e+-]+)')
    }

    for line in ksm_lines:
        for key, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                ns, pod, node, val = match.groups()
                val = float(val)
                if 'memory' in key:
                    val /= 1024 ** 2
                resource_data[ns][pod][key] = val
                node_totals[node][key] += val

    return resource_data, node_totals

# --- Main Loop ---
if __name__ == "__main__":
    config.load_kube_config()
    v1 = client.CoreV1Api()

    owner_info = {}
    container_info = defaultdict(list)
    node_map = {}

    for pod in v1.list_pod_for_all_namespaces().items:
        ns, name = pod.metadata.namespace, pod.metadata.name
        owner = pod.metadata.owner_references
        kind = owner[0].kind if owner else "None"
        owner_info[(ns, name)] = kind
        node_map[(ns, name)] = pod.spec.node_name
        for c in pod.spec.containers:
            container_info[(ns, name)].append(c.name)

    prev_metrics = fetch_cadvisor_metrics()
    time.sleep(60)

    while True:
        now = datetime.now(timezone.utc).isoformat()
        curr_metrics = fetch_cadvisor_metrics()
        ksm_metrics = fetch_ksm_metrics()
        resource_data, node_totals = parse_ksm_metrics(ksm_metrics)

        payload = {
            "timestamp": now,
            "containers": [],
            "nodes": []
        }

        for key in curr_metrics:
            if key in prev_metrics and "cpu" in curr_metrics[key] and "cpu" in prev_metrics[key]:
                ns, pod, container = key
                delta_cpu = curr_metrics[key]["cpu"] - prev_metrics[key]["cpu"]
                delta_time = curr_metrics[key]["timestamp"] - prev_metrics[key]["timestamp"]
                cpu_rate = delta_cpu / delta_time if delta_time > 0 else 0.0
                mem = curr_metrics[key].get("memory", 0.0)

                kind = owner_info.get((ns, pod), "Unknown")
                node = node_map.get((ns, pod), "unknown")
                metrics = resource_data.get(ns, {}).get(pod, {})

                payload["containers"].append({
                    "namespace": ns,
                    "pod": pod,
                    "node": node,
                    "container": container,
                    "kind": kind,
                    "cpu_usage": cpu_rate,
                    "memory_usage": mem,
                    "cpu_request": metrics.get('cpu_request', 0.0),
                    "cpu_limit": metrics.get('cpu_limit', 0.0),
                    "memory_request": metrics.get('memory_request', 0.0),
                    "memory_limit": metrics.get('memory_limit', 0.0)
                })

        for node, metrics in node_totals.items():
            payload["nodes"].append({
                "node": node,
                "cpu_request": metrics.get("cpu_request", 0.0),
                "cpu_limit": metrics.get("cpu_limit", 0.0),
                "memory_request": metrics.get("memory_request", 0.0),
                "memory_limit": metrics.get("memory_limit", 0.0)
            })

        try:
            res = requests.post("http://<YOUR-APP-HOST>:<PORT>/ingest", json=payload)
            print(f"[{now}] Sent {len(payload['containers'])} containers, {len(payload['nodes'])} nodes. Status: {res.status_code}")
        except Exception as e:
            print(f"❌ Failed to send metrics: {e}")

        prev_metrics = curr_metrics
        time.sleep(60)
