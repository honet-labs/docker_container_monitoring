#!/usr/bin/env python3
"""Read Docker Engine metrics over the local Unix socket (stdlib only).

This script is streamed to Docker hosts over SSH and is never permanently
installed on them. It issues GET requests only, but Docker socket access is
root-equivalent: trust and protect the SSH monitoring account.
"""
import http.client
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone


class DockerUnixHTTP(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__('docker', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def docker_get(socket_path, path, timeout):
    conn = DockerUnixHTTP(socket_path, timeout)
    try:
        conn.request('GET', path, headers={'User-Agent': 'PandoraFMS-Docker-Discovery/1.0'})
        res = conn.getresponse()
        raw = res.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise RuntimeError('Docker API reply exceeds 12 MiB')
        if res.status < 200 or res.status >= 300:
            raise RuntimeError('Docker Engine HTTP {} for {}: {}'.format(res.status, path, raw[:240].decode('utf-8', 'replace')))
        return json.loads(raw.decode('utf-8'))
    finally:
        conn.close()


def get_blkio(stats):
    blk = stats.get('blkio_stats') or {}
    values = blk.get('io_service_bytes_recursive') or blk.get('io_service_bytes') or []
    read_bytes, write_bytes = 0, 0
    for item in values:
        op = str(item.get('op', '')).lower()
        value = int(item.get('value') or 0)
        if op == 'read': read_bytes += value
        elif op == 'write': write_bytes += value
    return read_bytes, write_bytes


def get_cpu(stats):
    cur = stats.get('cpu_stats') or {}
    prev = stats.get('precpu_stats') or {}
    cpu_usage = ((cur.get('cpu_usage') or {}).get('total_usage') or 0)
    prev_usage = ((prev.get('cpu_usage') or {}).get('total_usage') or 0)
    system = cur.get('system_cpu_usage') or 0
    prev_system = prev.get('system_cpu_usage') or 0
    delta_cpu, delta_system = cpu_usage - prev_usage, system - prev_system
    cpus = cur.get('online_cpus') or len((cur.get('cpu_usage') or {}).get('percpu_usage') or []) or 1
    if delta_cpu < 0 or delta_system <= 0:
        return None
    return max(0.0, delta_cpu / delta_system * cpus * 100.0)


def parse_started_at(value):
    if not value or value.startswith('0001-'):
        return 0
    try:
        # Docker timestamps include fractional seconds and UTC Z.
        started = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if started.tzinfo is None: started = started.replace(tzinfo=timezone.utc)
        return max(0, int(time.time() - started.timestamp()))
    except (ValueError, OverflowError, TypeError):
        return 0


def metric_values(stats):
    mem = stats.get('memory_stats') or {}
    nets = stats.get('networks') or {}
    blk_read, blk_write = get_blkio(stats)
    rx = sum(int(n.get('rx_bytes') or 0) for n in nets.values())
    tx = sum(int(n.get('tx_bytes') or 0) for n in nets.values())
    limit = int(mem.get('limit') or 0)
    usage = int(mem.get('usage') or 0)
    metrics = {
        'memory_usage_bytes': usage,
        'memory_limit_bytes': limit,
        'memory_usage_percent': (usage / limit * 100.0) if limit > 0 else None,
        'network_rx_bytes_total': rx,
        'network_tx_bytes_total': tx,
        'block_read_bytes_total': blk_read,
        'block_write_bytes_total': blk_write,
        'pids': int((stats.get('pids_stats') or {}).get('current') or 0),
    }
    cpu = get_cpu(stats)
    if cpu is not None:
        metrics['cpu_usage'] = cpu
    return metrics


def run_probe(settings):
    socket_path = settings.get('socket', '/var/run/docker.sock')
    timeout = float(settings.get('api_timeout', 8))
    include_stopped = bool(settings.get('include_stopped', True))
    collect_stats = bool(settings.get('collect_stats', True))
    limit = int(settings.get('max_containers', 30))
    filter_re = settings.get('name_regex', '')
    pattern = re.compile(filter_re) if filter_re else None
    errors = []
    version = docker_get(socket_path, '/version', timeout)
    inventory = docker_get(socket_path, '/containers/json?all=1', timeout)
    selected = []
    for item in inventory:
        name = next((str(n).lstrip('/') for n in (item.get('Names') or []) if n), item.get('Id', '')[:12])
        if pattern and not pattern.search(name):
            continue
        if not include_stopped and item.get('State') != 'running':
            continue
        selected.append((name, item))
    selected.sort(key=lambda kv: (kv[0].lower(), kv[1].get('Id', '')))
    found = len(selected)
    if found > limit:
        errors.append('{} matching containers; monitoring first {}. Increase Max containers if required.'.format(found, limit))
    selected = selected[:limit]
    result = []
    for name, item in selected:
        container_id = item['Id']
        state = str(item.get('State', 'unknown')).lower()
        entry = {
            'id': container_id, 'name': name, 'state': state,
            'image': item.get('Image') or '', 'health': 'not_configured',
            'restart_count': 0, 'uptime_seconds': 0, 'metrics': {},
        }
        try:
            inspect = docker_get(socket_path, '/containers/{}/json'.format(container_id), timeout)
            container_state = inspect.get('State') or {}
            state = str(container_state.get('Status') or state).lower()
            entry['state'] = state
            health = container_state.get('Health')
            if isinstance(health, dict):
                entry['health'] = str(health.get('Status') or 'unknown').lower()
            entry['restart_count'] = int(inspect.get('RestartCount') or 0)
            if state == 'running':
                entry['uptime_seconds'] = parse_started_at(container_state.get('StartedAt', ''))
        except Exception as exc:
            errors.append('Inspect {}: {}'.format(name, str(exc)[:220]))
        if collect_stats and entry['state'] == 'running':
            try:
                stats = docker_get(socket_path, '/containers/{}/stats?stream=false'.format(container_id), timeout)
                entry['metrics'] = metric_values(stats)
            except Exception as exc:
                errors.append('Stats {}: {}'.format(name, str(exc)[:220]))
        elif collect_stats:
            entry['metrics'] = {k: 0 for k in (
                'cpu_usage', 'memory_usage_bytes', 'memory_limit_bytes',
                'memory_usage_percent', 'network_rx_bytes_total',
                'network_tx_bytes_total', 'block_read_bytes_total',
                'block_write_bytes_total', 'pids')}
        result.append(entry)
    return {
        'collected_at': time.time(), 'docker_version': version.get('Version', 'Unknown'),
        'docker_api': version.get('ApiVersion', 'Unknown'),
        'inventory_count': len(inventory), 'matching_count': found,
        'containers': result, 'errors': errors,
    }


if __name__ == '__main__':
    try:
        print(json.dumps(run_probe(SETTINGS), ensure_ascii=True, separators=(',', ':')))
    except Exception as exc:
        print(json.dumps({'fatal': '{}: {}'.format(type(exc).__name__, str(exc)[:600])}))
        sys.exit(2)
