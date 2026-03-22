import streamlit as st
import paramiko
import pandas as pd
import time

# ---------- PAGE CONFIG ----------
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

# ---------- METRICS ----------
def get_metrics(ssh):
    # CPU
    cpu_out = run_command(ssh, "top -bn1 | grep 'Cpu(s)'")
    cpu_idle = float(cpu_out.split(",")[3].split()[0])
    cpu = round(100 - cpu_idle, 2)

    # Memory
    mem_out = run_command(ssh, "free -m")
    mem_line = mem_out.split("\n")[1].split()
    mem = round((int(mem_line[2]) / int(mem_line[1])) * 100, 2)

    # Disk
    disk_out = run_command(ssh, "df -h /")
    disk = int(disk_out.split("\n")[1].split()[4].replace("%", ""))

    # Processes
    proc = run_command(ssh, "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 6")

    return cpu, mem, disk, proc


# ---------- UI HEADER ----------
st.title("🚀 VM Monitoring Dashboard")

# ---------- SIDEBAR ----------
st.sidebar.header("⚙️ Configuration")

if "vms" not in st.session_state:
    st.session_state.vms = []

host = st.sidebar.text_input("Host (IP)")
user = st.sidebar.text_input("Username")
pwd = st.sidebar.text_input("Password", type="password")

if st.sidebar.button("➕ Add VM"):
    if host and user and pwd:
        st.session_state.vms.append({"host": host, "user": user, "pwd": pwd})
        st.sidebar.success(f"Added {host}")

refresh_rate = st.sidebar.slider("Refresh Interval (sec)", 2, 10, 3)
cpu_alert_threshold = st.sidebar.slider("CPU Alert %", 50, 100, 80)

# ---------- DATA STORAGE ----------
if "history" not in st.session_state:
    st.session_state.history = {}

# ---------- MAIN ----------
if not st.session_state.vms:
    st.info("👈 Add at least one VM from sidebar to start monitoring")

for vm in st.session_state.vms:
    host = vm["host"]

    if host not in st.session_state.history:
        st.session_state.history[host] = {"cpu": [], "mem": [], "disk": []}

    try:
        ssh = connect_ssh(vm["host"], vm["user"], vm["pwd"])
        cpu, mem, disk, proc = get_metrics(ssh)

        # Store history (last 20 points)
        for key, val in zip(["cpu", "mem", "disk"], [cpu, mem, disk]):
            st.session_state.history[host][key].append(val)
            st.session_state.history[host][key] = st.session_state.history[host][key][-20:]

        # ---------- CARD ----------
        with st.container():
            col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

            status = "🟢 Healthy"
            if cpu > cpu_alert_threshold:
                status = "🔴 High CPU"

            col1.markdown(f"### 🖥️ {host}")
            col2.metric("CPU", f"{cpu}%")
            col3.metric("Memory", f"{mem}%")
            col4.metric("Disk", f"{disk}%")

            st.markdown(f"**Status:** {status}")

        # ---------- TABS ----------
        tab1, tab2, tab3 = st.tabs(["📊 Metrics", "🔥 Processes", "📥 Export"])

        # Metrics Graph
        with tab1:
            df = pd.DataFrame({
                "CPU": st.session_state.history[host]["cpu"],
                "Memory": st.session_state.history[host]["mem"],
                "Disk": st.session_state.history[host]["disk"]
            })
            st.line_chart(df, use_container_width=True)

        # Processes
        with tab2:
            st.code(proc)

        # Export
        with tab3:
            csv = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="Download Metrics CSV",
                data=csv,
                file_name=f"{host}_metrics.csv",
                mime="text/csv"
            )

        st.divider()

    except Exception as e:
        st.error(f"{host}: {e}")

# ---------- AUTO REFRESH ----------
time.sleep(refresh_rate)
st.rerun()