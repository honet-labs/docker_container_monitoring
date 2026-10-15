# Docker Container Monitoring — Pandora FMS Discovery

**Version:** 1.0.0  
**Package:** `honetlabs.docker_container_monitor.disco`  
**Platform:** Pandora FMS Discovery Application  
**Language:** Python 3 (standard library only)

An agentless Docker Engine monitor for Pandora FMS. A single Discovery task monitors **one Docker Host**, creates **one Pandora FMS Agent**, and emits **multiple modules for each Docker container**. Container metrics and status are queried over the local Docker Unix socket, either directly on the Pandora machine or through **one key-authenticated SSH execution** on a remote Docker Host. You do not need to deploy Pandora Software Agent inside the containers.

## Architecture

```
Pandora FMS Discovery Server
    |
    | one SSH connection (key authentication; optional for local mode)
    v
Docker Host: docker-monitor user -> /var/run/docker.sock
    |
    +-- nginx-app   -> status, CPU, RAM, network, disk, health, restarts
    +-- mysql-db    -> status, CPU, RAM, network, disk, health, restarts
    +-- redis       -> status, CPU, RAM, network, disk, health, restarts
    |
    v
One Pandora XML Agent: DOCKER-<host>
    +-- container_nginx_app_status
    +-- container_nginx_app_cpu_usage
    +-- container_nginx_app_memory_usage_percent
    +-- container_mysql_db_status
    +-- container_redis_status
    +-- ...
```

The Docker Engine API is queried through the Unix socket using Python's `http.client` and `socket` modules. This does **not** expose Docker's unauthenticated TCP API. The helper runs using the remote account's privileges, without `sudo`.

## Prerequisites

**Pandora Discovery Server:** Python 3.8+ (tested locally using Python 3), `ssh` client if remote mode, write permission to Pandora's XML data incoming directory. No extra Python packages required.

**Docker Host:** Docker Engine running, Python 3.8+, SSH daemon (SSH mode), account allowed to connect to the Docker Unix socket.

### Important Docker socket security note

A user allowed to connect to `/var/run/docker.sock` can generally control Docker Engine, including starting privileged containers, and should be considered **root-equivalent**. Membership in the `docker` group does not provide a true read-only permission model. Use a tightly controlled account and SSH key, and restrict it with network firewalls. If your security policy requires truly read-only access, implement a dedicated authorization layer or read-only proxy with explicit endpoint restrictions instead of exposing the raw Docker socket to a lower-trust user.

Documentation: https://docs.docker.com/engine/security/protect-access/ and https://docs.docker.com/engine/security/

## 1. Configure the remote Docker Host

Example for Rocky Linux 9 or other Linux systems with Docker Engine:

```bash
# On the DOCKER HOST (not the Pandora Server)
sudo useradd --create-home --shell /bin/bash docker-monitor
sudo usermod -aG docker docker-monitor

# Verify Docker's socket ownership and effective group
ls -l /var/run/docker.sock
id docker-monitor

# Test socket connectivity as the target account
sudo -u docker-monitor python3 -c \
  'import socket; s=socket.socket(socket.AF_UNIX); s.connect("/var/run/docker.sock"); print("Docker socket OK"); s.close()'
```

If the user was already logged in before adding it to the group, start a new login session. Do not chmod the Docker socket to `666`.

## 2. Configure SSH from the Pandora Discovery Server

Create a dedicated SSH key as the local account that runs Pandora Discovery. The example below assumes that account has access to `/etc/pandora/ssh`:

```bash
sudo install -d -m 700 /etc/pandora/ssh
sudo ssh-keygen -t ed25519 -f /etc/pandora/ssh/docker_monitor_ed25519 -N ''
```

Add the **public** key to `/home/docker-monitor/.ssh/authorized_keys` on the Docker Host. Ensure that directory permissions are `700`, `authorized_keys` is `600`, and ownership belongs to `docker-monitor`.

Verify the Docker Host's SSH host key fingerprint by a trusted channel and add it to the `known_hosts` file of the account running Pandora. The collector requires `StrictHostKeyChecking=yes`; it intentionally does not disable host key validation.

```bash
ssh -T -i /etc/pandora/ssh/docker_monitor_ed25519 \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  docker-monitor@172.17.8.131 \
  'python3 --version && id && ls -l /var/run/docker.sock'
```

SSH key authentication must work without a password prompt. SSH key path refers to a file **on the Pandora Discovery Server**, never the Docker Host.

## 3. Upload the `.disco` package

In Pandora FMS Console, go to **Discovery → Applications → Upload DISCO**, upload `honetlabs.docker_container_monitor.disco` and create a new Discovery Task under **Docker Container Monitoring**.

The package contains:

```
honetlabs.docker_container_monitor.disco
├── discovery_definition.ini
├── pandorafms_docker.py
├── docker_probe.py
└── README.md
```

Both Python files are required: the first manages the Pandora execution and XML output, and the second is streamed over SSH and talks to the Docker socket. No remote deployment of collector scripts is necessary.

## 4. Discovery Task settings

| Field | Example | Notes |
|---|---|---|
| Connection mode | `SSH` | `Local` only if Pandora Server is the Docker Host |
| Docker Host / IP | `172.17.8.131` | Used to reach SSH server |
| SSH port | `22` | SSH mode only |
| SSH username | `docker-monitor` | Must have Docker socket permission |
| SSH private key path | `/etc/pandora/ssh/docker_monitor_ed25519` | Stored on Pandora Server |
| Docker Unix socket | `/var/run/docker.sock` | No TCP 2375 needed |
| Pandora Agent name | `DOCKER-172.17.8.131` | **One Agent per host** |
| Pandora Agent group | `Servers` | Must exist in Pandora |
| Discover stopped containers | ON | Emits `status=0` for stopped containers |
| Collect resource metrics | ON | CPU, RAM, network, block I/O, PIDs |
| Calculate network throughput | ON | Mbps based on delta between two polls |
| Max containers | `30` | Prevents unexpectedly expensive polls |
| Name filter | blank | Optional Python regex |
| Interval | `300` | Set Discovery Task schedule accordingly |
| SSH execution timeout | `120` seconds | Increase if monitoring many containers |
| Prevent overlapping executions | ON | Prevents duplicate simultaneous runs |

**Important:** The Discovery schedule interval is configured separately in Pandora's Task definition. Keep it longer than the longest possible execution time.

## 5. Generated metrics

Container names are normalized to lowercase alphanumeric characters and underscores. For example, `nginx-app` becomes `nginx_app`, resulting in `container_nginx_app_cpu_usage`.

| Suffix | Type | Description |
|---|---|---|
| `status` | `generic_proc` | 1 = running, 0 = stopped/exited/paused |
| `state` | `generic_data_string` | Docker state (`running`, `exited`, etc.) |
| `restart_count` | `generic_data` | Number of container restarts reported by Docker |
| `uptime_seconds` | `generic_data` | Current run uptime |
| `health_state` | `generic_data_string` | Docker healthcheck state, only if configured |
| `health_ok` | `generic_proc` | 1 = healthy, 0 = unhealthy/starting |
| `cpu_usage` | `generic_data` | CPU use, percent of one logical CPU baseline |
| `memory_usage_bytes` | `generic_data` | Raw cgroup memory usage, bytes |
| `memory_limit_bytes` | `generic_data` | Container memory limit, bytes |
| `memory_usage_percent` | `generic_data` | Raw usage divided by memory limit |
| `network_rx_bytes_total` | `generic_data` | Cumulative received bytes |
| `network_tx_bytes_total` | `generic_data` | Cumulative transmitted bytes |
| `network_rx_mbps` | `generic_data` | RX Mbps across polls; available from second poll |
| `network_tx_mbps` | `generic_data` | TX Mbps across polls; available from second poll |
| `block_read_bytes_total` | `generic_data` | Cumulative block read bytes, if supported |
| `block_write_bytes_total` | `generic_data` | Cumulative block written bytes, if supported |
| `pids` | `generic_data` | Processes/threads reported by Docker |

Host-level modules: `docker_host_up`, `docker_containers_total`, `docker_containers_monitored`, and `docker_engine_version`.

**Notes:** Docker's CPU percentage can be above 100% for multicore usage. Memory usage is the raw Docker Engine API value; it may differ from `docker stats` because the Docker CLI can subtract page cache. Cumulative network counters must not be confused with throughput. Block I/O counters may be absent depending on Docker/cgroup/storage-driver version. Removed containers stop producing modules; Pandora manages module expiry separately. The first execution has no valid previous sample for Mbps calculations, so rate modules start with the next execution. For stopped containers, resource metrics are zeroed to prevent retaining old values.

## 6. Manual testing (without Pandora)

After the package is extracted, test locally using a plain configuration file (the example is for SSH):

```bash
cat > /tmp/pandora-docker-test.conf <<'EOF'
mode=ssh
host=172.17.8.131
ssh_port=22
ssh_user=docker-monitor
ssh_key=/etc/pandora/ssh/docker_monitor_ed25519
socket=/var/run/docker.sock
agent=DOCKER-172.17.8.131
group=Servers
include_stopped=true
collect_stats=true
network_rates=true
max_containers=30
interval=300
ssh_timeout=120
api_timeout=8
overlap_protection=true
EOF

python3 pandorafms_docker.py --config /tmp/pandora-docker-test.conf \
  --outdir /var/spool/pandora/data_in
```

A successful execution prints a JSON **Discovery Summary** and writes one XML `.data` file. Do not manually run the collector concurrently with an active scheduled task. The `.disco` itself has a per-task lock to prevent overlap.

## Troubleshooting

| Error | Cause / action |
|---|---|
| `PermissionError: [Errno 13]` | Docker socket access denied; verify account/group/socket |
| `No such file or directory` | Docker socket path wrong; verify `/var/run/docker.sock` |
| `Host key verification failed` | Add/verify trusted SSH host key to correct user's known_hosts |
| `Permission denied (publickey)` | Verify SSH account, private key path and file permissions |
| `python3: command not found` | Install Python 3 on the Docker Host |
| `SSH collector timeout` | Increase SSH timeout or reduce max containers |
| `PARTIAL` | Check errors in Discovery Task Summary; other containers may still be monitored |
| `FAILED` | Check SSH/Docker socket access and Summary error |
| Missing Mbps modules on first run | Expected: need two samples |

## Security recommendations

- Treat access to `docker.sock` as privileged/root-equivalent.
- Never publish port 2375 unauthenticated.
- Use SSH public-key authentication, strict host key validation and firewall restrictions.
- Do not put SSH private key material into the Discovery form; enter its **path** only.
- Prefer an Engine authorization layer or restricted proxy if Docker API privileges must be limited to read-only GET endpoints.
- This integration is community-developed; validate in a lab prior to production deployment.

## References

- [Pandora FMS — Discovery application development](https://pandorafms.com/manual/!current/en/documentation/pandorafms/technical_reference/12_disco_development)
- [Pandora FMS — XML Data interface](https://pandorafms.com/manual/!current/en/documentation/pandorafms/technical_reference/01_development_and_extension)
- [Docker — Protect the Docker daemon socket](https://docs.docker.com/engine/security/protect-access/)
- [Docker — Docker Engine security](https://docs.docker.com/engine/security/)
- [Docker — Container stats API](https://docs.docker.com/reference/api/engine/)
