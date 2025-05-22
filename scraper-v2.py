import time
import requests
import re
from datetime import datetime, timezone
from collections import defaultdict

# -----------------------------
# Parse label string
# -----------------------------
def parse_labels(label_str):
    labels = {}
    for part in label_str.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            labels[k.strip()] = v.strip('"')
    return labels

# -----------------------------
# Use working CPU/MEM logic from your reference
# -----------------------------
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
            value = float(line.split()[-2]) if len(line.split()) > 2 else float(line.split()[-1])
            key = (ns, pod, container)
            usage[key] = {"cpu": value, "timestamp": timestamp}

        elif line.startswith("container_memory_usage_bytes{"):
            label_part = line.split('{', 1)[1].split('}', 1)[0]
            labels = parse_labels(label_part)
            ns = labels.get("namespace", labels.get("container_label_io_kubernetes_pod_namespace", ""))
            pod = labels.get("pod", labels.get("container_label_io_kubernetes_pod_name", ""))
            container = labels.get("container", labels.get("container_label_io_kubernetes_container_name", ""))
            if not container or container in ("POD", ""):
                continue
            value = float(line.split()[-2]) if len(line.split()) > 2 else float(line.split()[-1])
            key = (ns, pod, container)
            if key not in usage:
                usage[key] = {}
            usage[key]["memory"] = value / (1024 ** 2)  # bytes to MiB

    return usage

# -----------------------------
# Existing kube-state-metrics parsing
# -----------------------------
def fetch_ksm():
    lines = requests.get("http://localhost:8080/metrics").text.splitlines()
    data = defaultdict(dict)

    resource_pattern = re.compile(
        r'kube_pod_container_resource_(requests|limits){.*?namespace="([^"]+)",pod="([^"]+)",.*?container="([^"]+)",.*?node="([^"]+)",.*?resource="(cpu|memory)".*?}\s+([0-9.e+-]+)'
    )
    pod_info_pattern = re.compile(
        r'kube_pod_info{.*?namespace="([^"]+)",pod="([^"]+)",.*?node="([^"]+)".*?}\s+1'
    )
    owner_pattern = re.compile(
        r'kube_pod_owner{[^}]*namespace="([^"]+)",pod="([^"]+)",[^}]*owner_kind="([^"]+)"[^}]*}\s+1'
    )

    for line in lines:
        m = resource_pattern.match(line)
        if m:
            kind, ns, pod, container, node, resource, val = m.groups()
            val = float(val)
            if resource == "memory":
                val /= 1024 ** 2
            key = (ns, pod, container)
            data[key][f"{resource}_{kind}"] = val
            data[key]["node"] = node

    for line in lines:
        m = pod_info_pattern.match(line)
        if m:
            ns, pod, node = m.groups()
            for key in data:
                if key[0] == ns and key[1] == pod and "node" not in data[key]:
                    data[key]["node"] = node

    for line in lines:
        m = owner_pattern.match(line)
        if m:
            ns, pod, owner_kind = m.groups()
            for key in data:
                if key[0] == ns and key[1] == pod:
                    data[key]["owner"] = owner_kind

    return data

# -----------------------------
# Loop with correct CPU/MEM logic
# -----------------------------
prev_metrics = fetch_cadvisor_metrics()
time.sleep(30)

while True:
    curr_metrics = fetch_cadvisor_metrics()
    ksm_metrics = fetch_ksm()
    now = datetime.now(timezone.utc).isoformat()

    print(f"\n📊 [{now}] LIVE RESOURCE USAGE")
    print(f"{'Node':<28} {'Namespace':<12} {'Owner':<12} {'Pod':<30} {'Container':<22} {'CPU (cores/sec)':>17} {'MEM (MiB)':>12} {'CPU Req':>8} {'CPU Lim':>8} {'Mem Req':>9} {'Mem Lim':>9}")
    print("-" * 185)

    for key in sorted(curr_metrics):
        if key not in prev_metrics or "cpu" not in curr_metrics[key] or "cpu" not in prev_metrics[key]:
            continue

        ns, pod, container = key
        delta_cpu = curr_metrics[key]["cpu"] - prev_metrics[key]["cpu"]
        delta_time = curr_metrics[key]["timestamp"] - prev_metrics[key]["timestamp"]
        cpu = delta_cpu / delta_time if delta_time > 0 else 0.0
        mem = curr_metrics[key].get("memory", 0.0)

        ksm = ksm_metrics.get(key, {})
        node = ksm.get("node", "unknown")
        owner = ksm.get("owner", "unknown")
        cpu_req = ksm.get("cpu_requests", 0.0)
        cpu_lim = ksm.get("cpu_limits", 0.0)
        mem_req = ksm.get("memory_requests", 0.0)
        mem_lim = ksm.get("memory_limits", 0.0)

        print(f"{node:<28} {ns:<12} {owner:<12} {pod:<30} {container:<22} {cpu:>17.4f} {mem:>12.2f} {cpu_req:>8.2f} {cpu_lim:>8.2f} {mem_req:>9.2f} {mem_lim:>9.2f}")

    prev_metrics = curr_metrics
    time.sleep(30)
