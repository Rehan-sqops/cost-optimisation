import os
import subprocess
import sys
import time
import socket
import requests
import re
from datetime import datetime, timezone
from collections import defaultdict
from kubernetes import client, config

# ---------- Setup Cronjob (Optional) ----------
def setup_cron():
    cron_entry = f"* * * * * /usr/bin/python3 {os.path.abspath(__file__)} >> ~/cadvisor_cron.log 2>&1"
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    current_cron = result.stdout if result.returncode == 0 else ''

    if cron_entry in current_cron:
        print("✅ Cronjob already exists.")
        return

    print("➕ Adding cronjob...")
    updated_cron = current_cron.strip() + '\n' + cron_entry + '\n'
    subprocess.run(['crontab', '-'], input=updated_cron, text=True)
    print("✅ Cronjob added successfully.")

if '--install-cron' in sys.argv:
    setup_cron()
    sys.exit(0)

# ---------- Dynamic JWT Token ----------
USERNAME = os.getenv("SCRAPER_USER", "user")
PASSWORD = os.getenv("SCRAPER_PASS", "pass")
AUTH_URL = os.getenv("AUTH_URL", "http://localhost:8082/auth")
INGEST_URL = os.getenv("INGEST_URL", "http://localhost:8082/ingest")


def get_jwt_token():
    auth_payload = {"username": USERNAME, "password": PASSWORD}
    headers = {"Content-Type": "application/json"}
    response = requests.post(AUTH_URL, json=auth_payload, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Auth failed: {response.text}")
    token = response.json().get("access_token")
    return token

# ---------- Helpers ----------
def parse_labels(label_str):
    labels = {}
    for part in label_str.split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            labels[k.strip()] = v.strip('"')
    return labels

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def get_running_cadvisor_pods(namespace='kube-system', label_selector='app=cadvisor'):
    config.load_kube_config()
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
    return [pod.metadata.name for pod in pods.items if pod.status.phase == 'Running']

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

def start_port_forwards(namespace, assignments):
    procs = []
    for pod, info in assignments.items():
        local_port = info['port']
        cmd = [
            'kubectl', 'port-forward',
            f'pod/{pod}',
            f'{local_port}:8080',
            '-n', namespace
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append((pod, proc))
    return procs

def wait_for_port(port, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            result = sock.connect_ex(('localhost', port))
            if result == 0:
                return True
        time.sleep(0.5)
    return False

def fetch_cadvisor_metrics_multiple(pod_ports):
    usage = {}
    timestamp = time.time()
    for pod, port in pod_ports.items():
        try:
            url = f"http://localhost:{port}/metrics"
            metrics = requests.get(url, timeout=5).text.splitlines()
        except Exception:
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
                usage[key]["memory"] = value / (1024 ** 2)
    return usage

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

def send_to_ingest(token, rows):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"records": rows}
    response = requests.post(INGEST_URL, json=payload, headers=headers)
    if response.status_code != 200:
        print(f"Error ingesting data: {response.text}")
    else:
        print(f"✅ Data sent successfully: {response.json()}")

# ---------- Main Execution ----------
if __name__ == '__main__':
    token = get_jwt_token()
    namespace = 'kube-system'
    pods = get_running_cadvisor_pods(namespace)
    assignments = assign_ports_to_pods(pods, columns=3)
    procs = start_port_forwards(namespace, assignments)
    pod_ports = {pod: info['port'] for pod, info in assignments.items()}

    for pod, port in pod_ports.items():
        wait_for_port(port, timeout=15)

    prev_metrics = fetch_cadvisor_metrics_multiple(pod_ports)
    time.sleep(30)

    try:
        curr_metrics = fetch_cadvisor_metrics_multiple(pod_ports)
        ksm_metrics = fetch_ksm()
        rows = []
        now = datetime.now(timezone.utc).isoformat()

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

            rows.append({
                "ts": time.time(),
                "cluster": "my-cluster",
                "node": node,
                "namespace": ns,
                "pod": pod,
                "container": container,
                "cpu_usage_sec": cpu,
                "mem_usage_b": int(mem * 1024 * 1024)  # Convert MiB to bytes
            })

        send_to_ingest(token, rows)

    finally:
        for _, proc in procs:
            proc.terminate()
