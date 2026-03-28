import streamlit as st
import paramiko
import pandas as pd
import time
import socket

# ---------- CONFIG ----------
st.set_page_config(page_title="VM Dashboard", layout="wide")

# ---------- SSH ----------
@st.cache_resource
def connect_ssh(host, username, password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=username, password=password)
    return ssh

def run_command(ssh, command):
    stdin, stdout, stderr = ssh.exec_command(command)
    return stdout.read().decode()

# ---------- HOST METRICS ----------
def get_host_metrics(ssh):
    cpu_out = run_command(ssh, "top -bn1 | grep 'Cpu(s)'")
    cpu_idle = float(cpu_out.split(",")[3].split()[0])
    cpu = round(100 - cpu_idle, 2)

    mem_out = run_command(ssh, "free -m")
    mem_line = mem_out.split("\n")[1].split()
    mem = round((int(mem_line[2]) / int(mem_line[1])) * 100, 2)

    disk_out = run_command(ssh, "df -h /")
    disk = int(disk_out.split("\n")[1].split()[4].replace("%", ""))

    proc = run_command(ssh, "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 6")

    return cpu, mem, disk, proc

# ---------- VPC CPU TRACK ----------
if "vpc_prev" not in st.session_state:
    st.session_state.vpc_prev = {}

def calculate_cpu_percent(name, current_time, interval, vcpu_count):
    prev = st.session_state.vpc_prev.get(name, current_time)
    delta = current_time - prev
    st.session_state.vpc_prev[name] = current_time

    if delta <= 0:
        return 0

    cpu_seconds = delta / 1e9
    cpu_percent = (cpu_seconds / (interval * vcpu_count)) * 100

    return round(cpu_percent, 2)

# ---------- VPC METRICS ----------
def get_vpc_stats(ssh, interval):
    output = run_command(ssh, "virsh domstats --vcpu")
    blocks = output.strip().split("\n\n")

    vpc_data = []

    for block in blocks:
        lines = block.split("\n")
        name = ""
        cpu_time = 0
        vcpu_count = 1

        for line in lines:
            if "Domain:" in line:
                name = line.split(":")[1].strip().replace("'", "")

            if "vcpu.time" in line:
                cpu_time += int(line.split("=")[1])

            if "vcpu.current" in line:
                vcpu_count = int(line.split("=")[1])

        if name:
            # Memory
            mem_out = run_command(ssh, f"virsh dommemstat {name} | grep rss")
            mem_kb = int(mem_out.split()[-1]) if mem_out else 0
            mem_mb = round(mem_kb / 1024, 2)

            cpu_percent = calculate_cpu_percent(name, cpu_time, interval, vcpu_count)

            vpc_data.append({
                "name": name,
                "cpu_%": cpu_percent,
                "memory_MB": mem_mb
            })

    return vpc_data

# ---------- VPC / VM LISTING & STATS (READ-ONLY) ----------
def list_vpcs(ssh):
    """Return a list of all VPCs (domains) with basic state from `virsh list --all`."""
    out = run_command(ssh, "virsh list --all")
    lines = out.strip().split('\n')
    vpcs = []
    # Find start of table (skip header lines)
    for line in lines:
        if not line.strip():
            continue
        # Skip header line if present
        if line.lower().startswith('id') or '----' in line:
            continue
        parts = line.split()
        # Expect: Id Name State...  (Id can be '-' for shut off)
        if len(parts) >= 2:
            vid = parts[0]
            name = parts[1]
            state = ' '.join(parts[2:]) if len(parts) > 2 else ''
            vpcs.append({
                'id': vid,
                'name': name,
                'state': state
            })
    return vpcs


def get_vpc_cpu_map(ssh, interval):
    """Return a mapping name -> cpu_percent using `virsh domstats --vcpu`.
    Keeps the same calculate_cpu_percent logic used elsewhere."""
    out = run_command(ssh, "virsh domstats --vcpu")
    blocks = out.strip().split('\n\n')
    cpu_map = {}
    for block in blocks:
        lines = block.split('\n')
        name = ''
        cpu_time = 0
        vcpu_count = 1
        for line in lines:
            if 'Domain:' in line:
                name = line.split(':', 1)[1].strip().replace("'", '')
            if 'vcpu.time' in line:
                try:
                    cpu_time += int(line.split('=')[1])
                except:
                    pass
            if 'vcpu.current' in line:
                try:
                    vcpu_count = int(line.split('=')[1])
                except:
                    pass
        if name:
            cpu_percent = calculate_cpu_percent(name, cpu_time, interval, vcpu_count)
            cpu_map[name] = cpu_percent
    return cpu_map


def get_vm_stats(ssh, name):
    """Return dict of stats for a VM/domain using `virsh dominfo` and `virsh dommemstat`.
    Values are normalized (MB for memory).
    """
    stats = {'name': name, 'vcpus': None, 'max_mem_MB': None, 'used_mem_MB': None, 'rss_MB': 0}
    try:
        dominfo = run_command(ssh, f"virsh dominfo {name}")
        for line in dominfo.split('\n'):
            if line.startswith('CPU(s):'):
                try:
                    stats['vcpus'] = int(line.split(':')[1].strip())
                except:
                    pass
            if line.startswith('Max memory:'):
                try:
                    # value like: 2097152 KiB
                    parts = line.split(':', 1)[1].strip().split()[0]
                    kb = int(parts)
                    stats['max_mem_MB'] = round(kb / 1024, 2)
                except:
                    pass
            if line.startswith('Used memory:'):
                try:
                    parts = line.split(':', 1)[1].strip().split()[0]
                    kb = int(parts)
                    stats['used_mem_MB'] = round(kb / 1024, 2)
                except:
                    pass

        mem_out = run_command(ssh, f"virsh dommemstat {name} | grep rss")
        if mem_out:
            try:
                mem_kb = int(mem_out.split()[-1])
                stats['rss_MB'] = round(mem_kb / 1024, 2)
            except:
                pass

    except Exception:
        # Keep defaults on parse failure
        pass

    return stats


def get_vm_ip(ssh, name):
    """Try to get a VM IP using `virsh domifaddr <name> --source agent` or lease."""
    try:
        out = run_command(ssh, f"virsh domifaddr {name} --source agent")
        lines = out.strip().split('\n')
        for line in lines:
            if name in line and ':' in line:
                parts = line.split()
                # look for an address like 192.168.x.x/24
                for p in parts:
                    if '/' in p and p.split('/')[0].count('.') == 3:
                        return p.split('/')[0]
        # fallback to lease/source
        out2 = run_command(ssh, f"virsh domifaddr {name} --source lease")
        lines = out2.strip().split('\n')
        for line in lines:
            parts = line.split()
            for p in parts:
                if '/' in p and p.split('/')[0].count('.') == 3:
                    return p.split('/')[0]
    except Exception:
        return None
    return None


def probe_ssh_port(ip, port=22, timeout=2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_guest_session(guest_ip, guest_user, guest_pwd):
    """If guest credentials provided, SSH into guest and run `who` to detect logged-in users."""
    try:
        ssh_guest = paramiko.SSHClient()
        ssh_guest.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_guest.connect(guest_ip, username=guest_user, password=guest_pwd, timeout=5)
        stdin, stdout, stderr = ssh_guest.exec_command('who')
        out = stdout.read().decode().strip()
        ssh_guest.close()
        if out:
            # return first few lines
            lines = out.split('\n')
            return 'Active: ' + ', '.join([l.split()[0] for l in lines[:3]])
        return 'No active sessions'
    except Exception as e:
        return f'Unknown ({str(e)})'

# ---------- UI ----------
st.title("🚀 VM + VPC Monitoring Dashboard")

# ---------- SIDEBAR ----------
st.sidebar.header("⚙️ Configuration")

if "vms" not in st.session_state:
    st.session_state.vms = []

host = st.sidebar.text_input("Host (IP)")
user = st.sidebar.text_input("Username")
pwd = st.sidebar.text_input("Password", type="password")

# Optional guest credentials for Method C session detection
guest_user = st.sidebar.text_input("Guest Username (optional)")
guest_pwd = st.sidebar.text_input("Guest Password (optional)", type="password")

if st.sidebar.button("➕ Add VM"):
    if host and user and pwd:
        st.session_state.vms.append({"host": host, "user": user, "pwd": pwd})

refresh_rate = st.sidebar.slider("Refresh Interval (sec)", 2, 10, 3)
cpu_alert_threshold = st.sidebar.slider("CPU Alert %", 50, 100, 80)

# ---------- STORAGE ----------
if "history" not in st.session_state:
    st.session_state.history = {}

# ---------- MAIN ----------
for vm in st.session_state.vms:
    host = vm["host"]

    if host not in st.session_state.history:
        st.session_state.history[host] = {"cpu": [], "mem": [], "disk": []}

    try:
        ssh = connect_ssh(vm["host"], vm["user"], vm["pwd"])

        # HOST
        cpu, mem, disk, proc = get_host_metrics(ssh)

        # VPCs (list all domains and collect stats)
        vpc_list = list_vpcs(ssh)
        cpu_map = get_vpc_cpu_map(ssh, refresh_rate)

        # Store history
        for key, val in zip(["cpu", "mem", "disk"], [cpu, mem, disk]):
            st.session_state.history[host][key].append(val)
            st.session_state.history[host][key] = st.session_state.history[host][key][-20:]

        # ---------- HOST UI ----------
        st.subheader(f"🖥️ Host: {host}")

        col1, col2, col3 = st.columns(3)
        col1.metric("CPU", f"{cpu}%")
        col2.metric("Memory", f"{mem}%")
        col3.metric("Disk", f"{disk}%")

        if cpu > cpu_alert_threshold:
            st.warning(f"⚠️ High CPU on {host}: {cpu}%")

        # ---------- TABS ----------
        tab1, tab2, tab3 = st.tabs(["📊 Metrics", "🔥 Processes", "📥 Export"])

        with tab1:
            df = pd.DataFrame({
                "CPU": st.session_state.history[host]["cpu"],
                "Memory": st.session_state.history[host]["mem"],
                "Disk": st.session_state.history[host]["disk"]
            })
            st.line_chart(df, use_container_width=True)

        with tab2:
            lines = proc.strip().split("\n")
            data = [line.split() for line in lines[1:]]
            df_proc = pd.DataFrame(data, columns=["PID", "Process", "CPU%", "MEM%"])
            st.dataframe(df_proc)

        with tab3:
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, f"{host}_metrics.csv")

        # ---------- VPC UI ----------
        st.subheader("📦 VPC / KVM Instances")

        if vpc_list:
            rows = []
            for v in vpc_list:
                name = v.get('name')
                vm_stats = get_vm_stats(ssh, name)
                # Try to detect guest IP and session status (Method C)
                guest_ip = get_vm_ip(ssh, name)
                ssh_open = False
                session_status = 'Unknown'
                if guest_ip:
                    ssh_open = probe_ssh_port(guest_ip)
                    if guest_user and guest_pwd and ssh_open:
                        session_status = check_guest_session(guest_ip, guest_user, guest_pwd)
                    else:
                        session_status = 'SSH open' if ssh_open else 'No SSH'

                rows.append({
                    'name': name,
                    'id': v.get('id'),
                    'state': v.get('state'),
                    'guest_ip': guest_ip,
                    'ssh_open': ssh_open,
                    'session_status': session_status,
                    'vcpus': vm_stats.get('vcpus'),
                    'max_mem_MB': vm_stats.get('max_mem_MB'),
                    'used_mem_MB': vm_stats.get('used_mem_MB'),
                    'rss_MB': vm_stats.get('rss_MB'),
                    'cpu_%': cpu_map.get(name, 0)
                })

            df_vpc = pd.DataFrame(rows)
            st.dataframe(df_vpc, use_container_width=True)
        else:
            st.info("No VPCs found")

        st.divider()

    except Exception as e:
        st.error(f"{host}: {e}")

# ---------- AUTO REFRESH ----------
time.sleep(refresh_rate)
st.rerun()