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
    return response.json().get("access_token")

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

def assign_ports_to_pods(pod_names, columns=3):
    assigned = {}
    row, col = 0, 0
    for pod in pod_names:
        port = find_free_port()
        assigned[pod] = {'port': port, 'row': row, 'column': col}
        col += 1
        if col >= columns:
            col, row = 0, row + 1
    return assigned

def start_port_forwards(namespace, assignments):
    procs = []
    for pod, info in assignments.items():
        cmd = ['kubectl', 'port-forward', f'pod/{pod}', f"{info['port']}:8080", '-n', namespace]
        procs.append((pod, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)))
    return procs

def wait_for_port(port, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) == 0:
                return True
        time.sleep(0.5)
    return False

def fetch_metrics(url):
    return requests.get(url).text.splitlines()

# ---------- Main Execution ----------
if __name__ == '__main__':
    token = get_jwt_token()
    namespace = 'kube-system'
    pods = get_running_cadvisor_pods(namespace)
    assignments = assign_ports_to_pods(pods)
    procs = start_port_forwards(namespace, assignments)
    pod_ports = {pod: info['port'] for pod, info in assignments.items()}
    for pod, port in pod_ports.items():
        wait_for_port(port)

    prev_metrics = fetch_metrics(f"http://localhost:{list(pod_ports.values())[0]}/metrics")
    time.sleep(30)
    curr_metrics = fetch_metrics(f"http://localhost:{list(pod_ports.values())[0]}/metrics")
    ksm_metrics = fetch_metrics("http://localhost:8080/metrics")

    # Collect Kubernetes info
    v1 = client.CoreV1Api()
    owner_info, container_info, node_map = {}, defaultdict(list), {}
    for pod in v1.list_pod_for_all_namespaces().items:
        ns, name, node = pod.metadata.namespace, pod.metadata.name, pod.spec.node_name
        owner = pod.metadata.owner_references
        kind = owner[0].kind if owner else "Unknown"
        owner_info[(ns, name)] = kind
        node_map[(ns, name)] = node
        for c in pod.spec.containers:
            container_info[(ns, name)].append(c.name)

    # Parse KSM data
    resource_data, container_usage = defaultdict(lambda: defaultdict(dict)), defaultdict(dict)
    patterns = {
        'memory_request': re.compile(r'kube_pod_container_resource_requests{.*namespace="([^"]+)",.*pod="([^"]+)",.*resource="memory".*}\s+([0-9.e+-]+)'),
        'memory_limit': re.compile(r'kube_pod_container_resource_limits{.*namespace="([^"]+)",.*pod="([^"]+)",.*resource="memory".*}\s+([0-9.e+-]+)'),
        'cpu_request': re.compile(r'kube_pod_container_resource_requests{.*namespace="([^"]+)",.*pod="([^"]+)",.*resource="cpu".*}\s+([0-9.e+-]+)'),
        'cpu_limit': re.compile(r'kube_pod_container_resource_limits{.*namespace="([^"]+)",.*pod="([^"]+)",.*resource="cpu".*}\s+([0-9.e+-]+)')
    }
    for line in ksm_metrics:
        for key, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                ns, pod, val = match.groups()
                resource_data[ns][pod][key] = float(val)

    cadvisor_cpu = re.compile(r'container_cpu_usage_seconds_total{.*namespace="([^"]+)",.*pod="([^"]+)",.*container="([^"]+)".*}\s+([0-9.e+-]+)')
    cadvisor_mem = re.compile(r'container_memory_usage_bytes{.*namespace="([^"]+)",.*pod="([^"]+)",.*container="([^"]+)".*}\s+([0-9.e+-]+)')
    for line in curr_metrics:
        if (match := cadvisor_cpu.search(line)):
            ns, pod, container, val = match.groups()
            container_usage[(ns, pod, container)]["cpu"] = float(val)
        if (match := cadvisor_mem.search(line)):
            ns, pod, container, val = match.groups()
            container_usage[(ns, pod, container)]["memory"] = float(val) / (1024 ** 2)

    # Prepare rows
    rows = []
    for (ns, pod), metrics in resource_data.items():
        containers = container_info.get((ns, pod), ["-"])
        for container in containers:
            usage = container_usage.get((ns, pod, container), {})
            rows.append({
                "ts": time.time(),
                "cluster": "my-cluster",
                "node": node_map.get((ns, pod), "unknown"),
                "namespace": ns,
                "pod": pod,
                "container": container,
                "kind": owner_info.get((ns, pod), "unknown"),
                "cpu_request": metrics.get("cpu_request", 0.0),
                "cpu_limit": metrics.get("cpu_limit", 0.0),
                "mem_request": metrics.get("memory_request", 0.0),
                "mem_limit": metrics.get("memory_limit", 0.0),
                "cpu_usage_sec": usage.get("cpu", 0.0),
                "mem_usage_b": int(usage.get("memory", 0.0) * 1024 * 1024)
            })

    # Send to ingest
    if rows:
        response = requests.post(INGEST_URL, json={"records": rows}, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        print(f"✅ Data sent: {response.json() if response.ok else response.text}")
    else:
        print("No data to send.")

    # Cleanup
    for _, proc in procs:
        proc.terminate()
