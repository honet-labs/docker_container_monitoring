#!/usr/bin/env python3
"""Pandora FMS Docker Discovery: one Agent per host; per-container modules."""
import argparse
import base64
import datetime
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import docker_probe


def parse_cfg(path):
    result = {}
    raw = Path(path).read_text(encoding='utf-8')
    # Accept either real newlines or Pandora's escaped separator sequence.
    # Only replace literal backslash-n immediately before another config key.
    raw = re.sub(r'\\n(?=[A-Za-z_][A-Za-z0-9_]*=)', '\n', raw)
    for line in raw.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip()
    return result


def as_bool(val, default=False):
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on') if val is not None else default


def as_int(val, default, lo, hi):
    try: n = int(val)
    except (TypeError, ValueError): n = default
    return max(lo, min(hi, n))


def ident(value):
    return re.sub('[^A-Za-z0-9_]+', '_', str(value)).strip('_').lower()[:90] or 'unnamed'


def agent_ident(value):
    return re.sub('[^A-Za-z0-9_.-]+', '_', str(value)).strip('_')[:120] or 'DOCKER-host'


def write_xml(agent, group, host, version, modules, interval):
    attrs = {
        'agent_name': agent, 'agent_alias': agent,
        'group': group, 'os_name': 'Linux',
        'os_version': 'Docker Engine {}'.format(version) if version else 'Docker Engine',
        'version': 'Docker Engine {}'.format(version) if version else 'Docker Engine',
        'address': host,
        'interval': str(interval),
        'timestamp': datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
    }
    root = ET.Element('agent_data', attrs)
    for d in modules:
        m = ET.SubElement(root, 'module')
        for key in ('name', 'type', 'data', 'unit', 'description', 'module_group'):
            if key in d and d[key] is not None:
                child = ET.SubElement(m, key)
                child.text = str(d[key])
    return ET.tostring(root, encoding='unicode', xml_declaration=False)


def module(items, name, value, mtype='generic_data', unit=None, group=None, desc=None):
    if value is None:
        return
    if isinstance(value, float):
        value = round(value, 4)
    d = {'name': name, 'type': mtype, 'data': value}
    if unit: d['unit'] = unit
    if group: d['module_group'] = group
    if desc: d['description'] = desc
    items.append(d)


def normalize_names(containers):
    used = set()
    out = {}
    for c in containers:
        base = ident(c.get('name'))
        cand = base
        if cand in used:
            cand = '{}_{}'.format(base[:70], str(c.get('id',''))[:8])
        used.add(cand)
        out[c.get('id')] = cand
    return out


def ssh_probe(settings, host, user, port, key, timeout):
    # Stream source code to remote Python's STDIN, without deploying a file.
    src = Path(__file__).with_name('docker_probe.py').read_text(encoding='utf-8')
    payload = base64.b64encode(json.dumps(settings).encode('utf-8')).decode('ascii')
    bootstrap = "import base64,json\nSETTINGS=json.loads(base64.b64decode('{}'))\n".format(payload)
    command = [
        'ssh', '-T', '-p', str(port), '-o', 'BatchMode=yes',
        '-o', 'PasswordAuthentication=no', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout={}'.format(min(30, timeout)),
    ]
    if key:
        command.extend(['-o', 'IdentitiesOnly=yes', '-i', key])
    # Reject dangerous strings rather than feeding unexpected SSH args.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', user):
        raise ValueError('Invalid SSH username')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,250}', host):
        raise ValueError('Invalid Docker host value')
    command.extend(['--', '{}@{}'.format(user, host), 'python3 -'])
    proc = subprocess.run(command, input=bootstrap + src, encoding='utf-8',
                          capture_output=True, timeout=timeout + 8, check=False)
    if proc.returncode != 0:
        # Output may contain a useful, structured fatal error from remote.
        try:
            data = json.loads(proc.stdout.strip())
            if data.get('fatal'): raise RuntimeError(data['fatal'])
        except (ValueError, json.JSONDecodeError):
            pass
        raise RuntimeError('SSH/remote collector failed (exit {}): {}'.format(
            proc.returncode, (proc.stderr or proc.stdout)[-450:].strip()))
    try:
        payload = json.loads(proc.stdout.strip())
    except ValueError:
        raise RuntimeError('Remote Python did not return valid JSON: {}'.format(proc.stdout[-350:].strip()))
    if 'fatal' in payload:
        raise RuntimeError(payload['fatal'])
    return payload


def new_counters(payload):
    return {
        'sampled_at': payload['collected_at'],
        'containers': {
            c['id']: {key: c.get('metrics', {}).get(key)
                      for key in ('network_rx_bytes_total', 'network_tx_bytes_total')}
            for c in payload['containers'] if c['state'] == 'running' and c.get('metrics')
        },
    }


def apply_rates(payload, state_path, modules, prefix):
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        previous = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    dt = payload['collected_at'] - float(previous.get('sampled_at', 0))
    old = previous.get('containers', {})
    for c in payload['containers']:
        m = c.get('metrics', {})
        prev = old.get(c['id'], {})
        if c.get('state') != 'running' or not (0 < dt < 86400):
            continue
        for name, metric in [('network_rx_bytes_total', 'network_rx_mbps'),
                             ('network_tx_bytes_total', 'network_tx_mbps')]:
            current, before = m.get(name), prev.get(name)
            if current is None or before is None or current < before:
                continue
            module(modules, '{}{}'.format(prefix[c['id']], metric),
                   ((current - before) * 8.0 / dt) / 1e6,
                   unit='Mbps', group='Docker Network')
    tmp_path = state_path.with_name(state_path.name + '.tmp.{}'.format(os.getpid()))
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(new_counters(payload), f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, state_path)


def collect_modules(payload, enable_stats):
    modules = []
    module(modules, 'docker_host_up', 1, mtype='generic_proc', group='Docker Host')
    module(modules, 'docker_containers_total', payload['inventory_count'], group='Docker Host')
    module(modules, 'docker_containers_monitored', len(payload['containers']), group='Docker Host')
    versions = str(payload.get('docker_version') or '')
    if versions:
        module(modules, 'docker_engine_version', versions, mtype='generic_data_string', group='Docker Host')
    mapping = normalize_names(payload['containers'])
    pref = {}
    for c in payload['containers']:
        p = 'container_{}_'.format(mapping[c['id']])
        pref[c['id']] = p
        running = int(c.get('state') == 'running')
        module(modules, p + 'status', running, 'generic_proc', group='Docker Status',
               desc='1=running; 0=not running')
        module(modules, p + 'state', c.get('state') or 'unknown',
               'generic_data_string', group='Docker Status')
        module(modules, p + 'restart_count', c.get('restart_count', 0), group='Docker Status')
        module(modules, p + 'uptime_seconds', c.get('uptime_seconds', 0),
               unit='seconds', group='Docker Status')
        health = c.get('health', 'not_configured')
        if health != 'not_configured':
            module(modules, p + 'health_state', health, 'generic_data_string', group='Docker Health')
            module(modules, p + 'health_ok', int(health == 'healthy'),
                   'generic_proc', group='Docker Health',
                   desc='1=healthy; 0=unhealthy/starting')
        if not enable_stats: continue
        m = c.get('metrics') or {}
        for key, unit, group in (
            ('cpu_usage', '%', 'Docker CPU'),
            ('memory_usage_bytes', 'bytes', 'Docker Memory'),
            ('memory_limit_bytes', 'bytes', 'Docker Memory'),
            ('memory_usage_percent', '%', 'Docker Memory'),
            ('network_rx_bytes_total', 'bytes', 'Docker Network'),
            ('network_tx_bytes_total', 'bytes', 'Docker Network'),
            ('block_read_bytes_total', 'bytes', 'Docker I/O'),
            ('block_write_bytes_total', 'bytes', 'Docker I/O'),
            ('pids', 'processes', 'Docker Resources')):
            module(modules, p + key, m.get(key), unit=unit, group=group)
    return modules, pref


def main():
    ap = argparse.ArgumentParser(description='Pandora Docker Host Discovery')
    ap.add_argument('--config', required=True)
    ap.add_argument('--outdir', default='/var/spool/pandora/data_in')
    ap.add_argument('--lock-file', default='/tmp/pandorafms-docker.lock')
    ap.add_argument('--stdout', action='store_true')
    args = ap.parse_args()
    cfg = parse_cfg(args.config)
    host = cfg.get('host', '').strip()
    agent = agent_ident(cfg.get('agent') or 'DOCKER-{}'.format(host or 'localhost'))
    group = cfg.get('group', '') or 'Servers'
    mode = cfg.get('mode', 'ssh').lower()
    socket_path = cfg.get('socket', '/var/run/docker.sock').strip() or '/var/run/docker.sock'
    interval = as_int(cfg.get('interval'), 300, 60, 86400)
    ssh_timeout = as_int(cfg.get('ssh_timeout'), 120, 15, 600)
    max_containers = as_int(cfg.get('max_containers'), 30, 1, 200)
    include_stopped = as_bool(cfg.get('include_stopped'), True)
    collect_stats = as_bool(cfg.get('collect_stats'), True)
    rates_enabled = as_bool(cfg.get('network_rates'), True)
    lock = None
    start = time.monotonic()
    errors = []
    modules = []
    output_path = ''
    status = 'FAILED'
    try:
        if mode not in ('ssh', 'local'):
            raise ValueError('Connection mode must be SSH or Local')
        if not host:
            raise ValueError('Docker Host / IP is required')
        if not socket_path.startswith('/') or '\x00' in socket_path:
            raise ValueError('Docker socket must be an absolute Unix socket path')
        if as_bool(cfg.get('overlap_protection'), True):
            lock = open(args.lock_file, 'a+')
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'summary': {'Status': 'SKIPPED', 'Agent': agent},
                                  'info': 'Previous Discovery execution is still running.'}))
                return 0
        settings = {
            'socket': socket_path, 'include_stopped': include_stopped,
            'collect_stats': collect_stats,
            'max_containers': max_containers,
            'api_timeout': as_int(cfg.get('api_timeout'), 8, 2, 30),
            'name_regex': cfg.get('name_regex', ''),
        }
        if mode == 'ssh':
            user = cfg.get('ssh_user', '').strip()
            if not user: raise ValueError('SSH username required for remote Docker Host')
            payload = ssh_probe(settings, host, user,
                                as_int(cfg.get('ssh_port'), 22, 1, 65535),
                                cfg.get('ssh_key', '').strip(), ssh_timeout)
        else:
            payload = docker_probe.run_probe(settings)
        modules, prefixes = collect_modules(payload, collect_stats)
        errors.extend(payload.get('errors') or [])
        if rates_enabled and collect_stats:
            state_dir = Path(os.path.expanduser(cfg.get('state_dir') or '~/.cache/pandora-docker-discovery'))
            safe_host = ident(host)
            state_path = state_dir / '{}_{}.json'.format(safe_host, ident(agent))
            try:
                apply_rates(payload, state_path, modules, prefixes)
            except (OSError, ValueError) as exc:
                errors.append('Network rate cache: {}'.format(exc))
        xml = write_xml(agent, group, host, payload.get('docker_version'), modules, interval)
        ET.fromstring(xml)
        if args.stdout:
            print(xml)
            output_path = 'stdout'
        else:
            outdir = Path(args.outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            output_file = outdir / '{}_{}_{}.data'.format(agent, int(time.time()), os.getpid())
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(xml)
            output_path = str(output_file)
        status = 'PARTIAL' if errors else 'OK'
        summary = {
            'Status': status, 'Agent': agent, 'Docker Host': host,
            'Docker version': payload.get('docker_version', 'Unknown'),
            'Containers found': payload.get('matching_count', 0),
            'Containers monitored': len(payload['containers']),
            'Modules generated': len(modules), 'Errors': len(errors),
            'Collection time': '{:.2f} seconds'.format(time.monotonic() - start),
            'Output': output_path,
        }
    except Exception as exc:
        errors.append('{}: {}'.format(type(exc).__name__, str(exc)[:500]))
        # Still send host-down numeric module, while reporting diagnostic in Summary.
        modules = []
        module(modules, 'docker_host_up', 0, 'generic_proc', group='Docker Host')
        if not args.stdout:
            try:
                target = Path(args.outdir)
                target.mkdir(parents=True, exist_ok=True)
                filename = target / '{}_{}_{}.data'.format(agent, int(time.time()), os.getpid())
                filename.write_text(write_xml(agent, group, host, '', modules, interval), encoding='utf-8')
                output_path = str(filename)
            except OSError as exc2:
                errors.append('Failed to write XML: {}'.format(exc2))
        summary = {'Status': 'FAILED', 'Agent': agent, 'Docker Host': host,
                   'Modules generated': len(modules), 'Errors': len(errors),
                   'Output': output_path}
    finally:
        if lock:
            lock.close()
    for idx, error in enumerate(errors[:6], 1):
        summary['Error {}'.format(idx)] = error
    if len(errors) > 6:
        summary['Additional errors'] = len(errors) - 6
    if args.stdout:
        print(json.dumps({'summary': summary, 'info': 'Docker Discovery Collector result'}, ensure_ascii=False), file=sys.stderr)
    else:
        print(json.dumps({'summary': summary, 'info': 'Docker Discovery Collector result'}, ensure_ascii=False))
    return 2 if summary['Status'] == 'FAILED' else 0


if __name__ == '__main__':
    sys.exit(main())
