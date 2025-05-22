import subprocess
import time
import socket
import requests
import re
from datetime import datetime, timezone
from collections import defaultdict
from kubernetes import client, config

# ------------ Helper to parse labels -------------
def parse_labels(label_str):
    labels = {}
    for part in label_str.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            labels[k.strip()] = v.strip('"')
    return labels

# ------------ Find free TCP port on localhost -------------
def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# ------------ Get running cAdvisor pods -------------
def get_running_cadvisor_pods(namespace='kube-system', label_selector='app=cadvisor'):
    config.load_kube_config()
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
    return [pod.metadata.name for pod in pods.items if pod.status.phase == 'Running']

# ------------ Assign ports to pods in a grid -------------
def assign_ports_to_pods(pod_names, columns=5):
    assigned = {}
    row = 0
    col = 0
    for pod in pod_names:
        port = find_free_port()
        assigned[pod] = {'port': port, 'row': row, 'column': col}
        col += 1
        if col >= columns:
            col = 0
            row += 1
    return assigned

# ------------ Start kubectl port-forward for all pods -------------
def start_port_forwards(namespace, assignments):
    procs = []
    for pod, info in assignments.items():
        local_port = info['port']
        cmd = [
            'kubectl', 'port-forward',
            f'pod/{pod}',
            f'{local_port}:8080',  # cAdvisor port
            '-n', namespace
        ]
        print(f"Starting port-forward for pod {pod} on local port {local_port}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append((pod, proc))
    return procs

# ------------ Wait for a port to be open -------------
def wait_for_port(port, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            result = sock.connect_ex(('localhost', port))
            if result == 0:
                return True
        time.sleep(0.5)
    return False

# ------------ Fetch metrics from all port-forwarded pods -------------
def fetch_cadvisor_metrics_multiple(pod_ports):
    usage = {}
    timestamp = time.time()

    for pod, port in pod_ports.items():
        try:
            url = f"http://localhost:{port}/metrics"
            metrics = requests.get(url, timeout=5).text.splitlines()
        except Exception as e:
            print(f"Error fetching metrics from pod {pod} at port {port}: {e}")
            continue

        for line in metrics:
            if line.startswith("container_cpu_usage_seconds_total{"):
                label_part = line.split('{', 1)[1].split('}', 1)[0]
                labels = parse_labels(label_part)
                ns = labels.get("namespace", labels.get("container_label_io_kubernetes_pod_namespace", ""))
                pod_name = labels.get("pod", labels.get("container_label_io_kubernetes_pod_name", ""))
                container = labels.get("container", labels.get("container_label_io_kubernetes_container_name", ""))
                if not container or container in ("POD", ""):
                    continue
                value = float(line.split()[-2]) if len(line.split()) > 2 else float(line.split()[-1])
                key = (ns, pod_name, container)
                usage[key] = {"cpu": value, "timestamp": timestamp}

            elif line.startswith("container_memory_usage_bytes{"):
                label_part = line.split('{', 1)[1].split('}', 1)[0]
                labels = parse_labels(label_part)
                ns = labels.get("namespace", labels.get("container_label_io_kubernetes_pod_namespace", ""))
                pod_name = labels.get("pod", labels.get("container_label_io_kubernetes_pod_name", ""))
                container = labels.get("container", labels.get("container_label_io_kubernetes_container_name", ""))
                if not container or container in ("POD", ""):
                    continue
                value = float(line.split()[-2]) if len(line.split()) > 2 else float(line.split()[-1])
                key = (ns, pod_name, container)
                if key not in usage:
                    usage[key] = {}
                usage[key]["memory"] = value / (1024 ** 2)  # bytes to MiB

    return usage

# ------------ Your existing kube-state-metrics parsing -------------
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

# ------------ Main loop -------------
if __name__ == '__main__':
    namespace = 'kube-system'

    pods = get_running_cadvisor_pods(namespace)
    assignments = assign_ports_to_pods(pods, columns=3)
    procs = start_port_forwards(namespace, assignments)

    pod_ports = {pod: info['port'] for pod, info in assignments.items()}

    # Wait for all port-forwarded ports to be ready before scraping
    for pod, port in pod_ports.items():
        ready = wait_for_port(port, timeout=15)
        if not ready:
            print(f"Warning: Port {port} for pod {pod} did not become ready in time")

    prev_metrics = fetch_cadvisor_metrics_multiple(pod_ports)
    time.sleep(30)

    try:
        while True:
            curr_metrics = fetch_cadvisor_metrics_multiple(pod_ports)
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

    except KeyboardInterrupt:
        print("Stopping port-forwards...")
        for pod, proc in procs:
            proc.terminate()
        print("Stopped all port-forwards.")
