"""
HyveOS Network Provisioner — Agent-driven remote installation via SSH.

When a user says "use 192.168.1.50 to install hyve" or "provision that network
machine", the agent:
  1. SSHs into the target (using provided credentials)
  2. Runs system checks (OS, RAM, CPU, GPU, disk)
  3. Transfers the install bundle
  4. Executes installation
  5. Registers the new node with the hive
  6. Reports back to user with node identity

Uses paramiko for SSH operations.
"""

import io
import json
import logging
import os
import re
import socket
import tarfile
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_provisioner')

# Try import paramiko — graceful fallback if not installed
try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    paramiko = None
    PARAMIKO_AVAILABLE = False
    logger.warning("paramiko not installed — network provisioning disabled. "
                    "Install with: pip install paramiko")

INSTALL_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'deploy', 'linux', 'install.sh')
BUNDLE_SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'deploy', 'linux', 'build_bundle.sh')

# Preflight thresholds
MIN_RAM_GB = 4
MIN_DISK_GB = 10
SUPPORTED_OS = ['ubuntu', 'debian']


_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,254}$')
_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,31}$')


class NetworkProvisioner:
    """SSH-based remote HyveOS installation and management."""

    @staticmethod
    def _validate_params(target_host: str, ssh_user: str, backend_port: int = 6777):
        """Validate provisioning parameters to prevent command injection."""
        if not _HOSTNAME_RE.match(target_host):
            raise ValueError(f"Invalid target_host: {target_host!r} — must be FQDN or IPv4")
        if not _USERNAME_RE.match(ssh_user):
            raise ValueError(f"Invalid ssh_user: {ssh_user!r} — alphanumeric + underscore only")
        if not (1 <= int(backend_port) <= 65535):
            raise ValueError(f"Invalid backend_port: {backend_port} — must be 1-65535")

    @staticmethod
    def _get_ssh_client(target_host: str, ssh_user: str = 'root',
                        ssh_key_path: str = None,
                        ssh_password: str = None,
                        timeout: int = 15,
                        **kwargs) -> 'paramiko.SSHClient':
        """Create and connect an SSH client."""
        if not PARAMIKO_AVAILABLE:
            raise RuntimeError("paramiko not installed. Run: pip install paramiko")

        client = paramiko.SSHClient()

        # Load system known_hosts for host key verification
        known_hosts_path = os.path.expanduser('~/.ssh/known_hosts')
        if os.path.exists(known_hosts_path):
            client.load_host_keys(known_hosts_path)

        # Strict mode rejects unknown hosts; default adds with warning
        strict_host_key = kwargs.pop('strict_host_key', False)
        if strict_host_key:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            logger.info("SSH strict host key: unknown hosts will be REJECTED for %s", target_host)
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            logger.warning("SSH auto-add: accepting host key for %s (add to known_hosts for production)",
                           target_host)

        connect_kwargs = {
            'hostname': target_host,
            'username': ssh_user,
            'timeout': timeout,
        }

        if ssh_key_path:
            connect_kwargs['key_filename'] = ssh_key_path
        elif ssh_password:
            connect_kwargs['password'] = ssh_password
        else:
            # Try default SSH key locations
            default_keys = [
                os.path.expanduser('~/.ssh/id_ed25519'),
                os.path.expanduser('~/.ssh/id_rsa'),
            ]
            for key_path in default_keys:
                if os.path.exists(key_path):
                    connect_kwargs['key_filename'] = key_path
                    break

        client.connect(**connect_kwargs)
        return client

    @staticmethod
    def _exec_remote(client, cmd: str, timeout: int = 120) -> Dict:
        """Execute command on remote host and return result."""
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return {
            'stdout': stdout.read().decode('utf-8', errors='replace').strip(),
            'stderr': stderr.read().decode('utf-8', errors='replace').strip(),
            'exit_code': exit_code,
        }

    @staticmethod
    def preflight_check(target_host: str, ssh_user: str = 'root',
                        ssh_key_path: str = None,
                        ssh_password: str = None) -> Dict:
        """Run preflight checks on remote machine without installing.

        Returns:
            Dict with keys: ok (bool), checks (list of check results),
            system_info (dict with OS, RAM, disk, CPU, GPU)
        """
        checks = []
        system_info = {}

        try:
            client = NetworkProvisioner._get_ssh_client(
                target_host, ssh_user, ssh_key_path, ssh_password)
        except Exception as e:
            return {
                'ok': False,
                'checks': [{'name': 'ssh_connect', 'ok': False, 'detail': str(e)}],
                'system_info': {},
            }

        try:
            # SSH connectivity
            checks.append({'name': 'ssh_connect', 'ok': True, 'detail': 'Connected'})

            # OS detection
            result = NetworkProvisioner._exec_remote(client, 'cat /etc/os-release')
            os_info = {}
            for line in result['stdout'].split('\n'):
                if '=' in line:
                    key, val = line.split('=', 1)
                    os_info[key] = val.strip('"')
            os_id = os_info.get('ID', 'unknown')
            os_version = os_info.get('VERSION_ID', 'unknown')
            system_info['os'] = os_info.get('PRETTY_NAME', f'{os_id} {os_version}')
            system_info['os_id'] = os_id

            os_ok = os_id in SUPPORTED_OS or 'ubuntu' in os_info.get('ID_LIKE', '')
            checks.append({
                'name': 'os_supported',
                'ok': os_ok,
                'detail': system_info['os'],
            })

            # RAM check
            result = NetworkProvisioner._exec_remote(
                client, "grep MemTotal /proc/meminfo | awk '{print $2}'")
            ram_kb = int(result['stdout']) if result['stdout'].isdigit() else 0
            ram_gb = ram_kb / 1048576
            system_info['ram_gb'] = round(ram_gb, 1)
            checks.append({
                'name': 'ram_sufficient',
                'ok': ram_gb >= MIN_RAM_GB,
                'detail': f'{ram_gb:.1f}GB (min {MIN_RAM_GB}GB)',
            })

            # Disk check
            result = NetworkProvisioner._exec_remote(
                client, "df /opt --output=avail | tail -1 | tr -d ' '")
            disk_kb = int(result['stdout']) if result['stdout'].isdigit() else 0
            disk_gb = disk_kb / 1048576
            system_info['disk_gb'] = round(disk_gb, 1)
            checks.append({
                'name': 'disk_sufficient',
                'ok': disk_gb >= MIN_DISK_GB,
                'detail': f'{disk_gb:.1f}GB available (min {MIN_DISK_GB}GB)',
            })

            # CPU info
            result = NetworkProvisioner._exec_remote(client, 'nproc')
            cpu_cores = int(result['stdout']) if result['stdout'].isdigit() else 0
            system_info['cpu_cores'] = cpu_cores
            checks.append({
                'name': 'cpu_cores',
                'ok': cpu_cores >= 2,
                'detail': f'{cpu_cores} cores',
            })

            # GPU detection
            result = NetworkProvisioner._exec_remote(
                client, 'nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "none"')
            gpu = result['stdout'].strip()
            system_info['gpu'] = gpu if gpu != 'none' else 'none'

            # systemd check
            result = NetworkProvisioner._exec_remote(client, 'systemctl --version')
            has_systemd = result['exit_code'] == 0
            checks.append({
                'name': 'systemd_available',
                'ok': has_systemd,
                'detail': 'present' if has_systemd else 'missing',
            })

            # Python check
            result = NetworkProvisioner._exec_remote(
                client, 'python3.10 --version 2>/dev/null || python3 --version 2>/dev/null || echo "none"')
            python_ver = result['stdout'].strip()
            system_info['python'] = python_ver
            checks.append({
                'name': 'python_available',
                'ok': python_ver != 'none',
                'detail': python_ver,
            })

            all_ok = all(c['ok'] for c in checks)
            return {'ok': all_ok, 'checks': checks, 'system_info': system_info}

        finally:
            client.close()

    @staticmethod
    def provision_remote(target_host: str, ssh_user: str = 'root',
                         ssh_key_path: str = None,
                         ssh_password: str = None,
                         join_peer: str = None,
                         backend_port: int = 6777,
                         no_vision: bool = False,
                         no_llm: bool = False,
                         provisioned_by: str = 'system') -> Dict:
        """Full remote provisioning pipeline.

        Steps:
        1. SSH connect
        2. Preflight checks
        3. Transfer install bundle
        4. Execute install.sh
        5. Wait for backend to come up
        6. Register node with hive
        7. Record in ProvisionedNode table

        Returns:
            Dict with: success (bool), node_id, tier, message, error
        """
        if not PARAMIKO_AVAILABLE:
            return {'success': False, 'error': 'paramiko not installed'}

        # Validate inputs to prevent command injection
        NetworkProvisioner._validate_params(target_host, ssh_user, backend_port)

        # Step 1: Preflight
        logger.info("Provisioning %s@%s — running preflight checks...",
                     ssh_user, target_host)
        preflight = NetworkProvisioner.preflight_check(
            target_host, ssh_user, ssh_key_path, ssh_password)

        if not preflight['ok']:
            failed = [c for c in preflight['checks'] if not c['ok']]
            return {
                'success': False,
                'error': f"Preflight failed: {', '.join(c['name'] for c in failed)}",
                'preflight': preflight,
            }

        try:
            client = NetworkProvisioner._get_ssh_client(
                target_host, ssh_user, ssh_key_path, ssh_password)
        except Exception as e:
            return {'success': False, 'error': f'SSH connect failed: {e}'}

        try:
            # Step 2: Build and transfer bundle
            logger.info("Transferring install bundle to %s...", target_host)

            # Create a minimal install archive on the fly
            sftp = client.open_sftp()

            # Create remote temp directory
            NetworkProvisioner._exec_remote(client, 'mkdir -p /tmp/hyve-install')

            # Transfer install script
            if os.path.exists(INSTALL_SCRIPT_PATH):
                sftp.put(INSTALL_SCRIPT_PATH, '/tmp/hyve-install/install.sh')
            else:
                # Fallback: generate install script path from bundle
                return {'success': False, 'error': 'install.sh not found locally'}

            # Transfer the application code (tar from repo root)
            repo_root = os.path.join(os.path.dirname(__file__), '..', '..')
            repo_root = os.path.abspath(repo_root)

            # Create tar in memory (exclude large dirs and __pycache__ recursively)
            logger.info("Creating transfer archive...")
            EXCLUDE_TOP = {'.git', '__pycache__', 'venv310', 'venv',
                           'tests', '.idea', 'autogen-0.2.37', 'docs',
                           '.pycharm_plugin', 'dist', 'node_modules'}
            EXCLUDE_PATTERNS = {'__pycache__', '.pyc', '.pyo', '*.egg-info'}

            def _tar_filter(tarinfo):
                """Filter out __pycache__, .pyc, and dev artifacts from archive."""
                parts = tarinfo.name.split('/')
                for part in parts:
                    if part == '__pycache__' or part.endswith('.egg-info'):
                        return None
                if tarinfo.name.endswith(('.pyc', '.pyo')):
                    return None
                return tarinfo

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                for item in os.listdir(repo_root):
                    if item in EXCLUDE_TOP:
                        continue
                    full_path = os.path.join(repo_root, item)
                    tar.add(full_path, arcname=item, filter=_tar_filter)
            buf.seek(0)

            # Transfer archive
            logger.info("Uploading archive to %s (%d bytes)...",
                         target_host, buf.getbuffer().nbytes)
            sftp.putfo(buf, '/tmp/hyve-install/hyve-code.tar.gz')
            sftp.close()

            # Step 3: Extract and install on remote
            logger.info("Running installer on %s...", target_host)

            # Extract code
            NetworkProvisioner._exec_remote(
                client,
                'cd /tmp/hyve-install && mkdir -p code && '
                'tar xzf hyve-code.tar.gz -C code',
                timeout=60,
            )

            # Copy install script to extracted code
            NetworkProvisioner._exec_remote(
                client,
                'mkdir -p /tmp/hyve-install/code/deploy/linux && '
                'cp /tmp/hyve-install/install.sh /tmp/hyve-install/code/deploy/linux/',
            )

            # Build install command
            install_cmd = 'bash /tmp/hyve-install/code/deploy/linux/install.sh'
            if join_peer:
                install_cmd += f' --join-peer {join_peer}'
            if backend_port != 6777:
                install_cmd += f' --port {backend_port}'
            if no_vision:
                install_cmd += ' --no-vision'
            if no_llm:
                install_cmd += ' --no-llm'

            result = NetworkProvisioner._exec_remote(
                client, install_cmd, timeout=300)

            if result['exit_code'] != 0:
                return {
                    'success': False,
                    'error': f"Install failed (exit {result['exit_code']})",
                    'stdout': result['stdout'][-500:],
                    'stderr': result['stderr'][-500:],
                }

            # Step 4: Wait for backend
            logger.info("Waiting for backend on %s:%d...", target_host, backend_port)
            node_up = False
            for _ in range(30):
                check = NetworkProvisioner._exec_remote(
                    client,
                    f'curl -s http://localhost:{backend_port}/status',
                    timeout=10,
                )
                if check['exit_code'] == 0 and check['stdout']:
                    node_up = True
                    break
                time.sleep(2)

            if not node_up:
                return {
                    'success': False,
                    'error': 'Backend did not start within 60 seconds',
                    'install_output': result['stdout'][-500:],
                }

            # Step 5: Get node identity
            id_result = NetworkProvisioner._exec_remote(
                client,
                'cat /var/lib/hyve/node_public.key | xxd -p | tr -d "\\n"',
            )
            node_id = id_result['stdout'][:64] if id_result['exit_code'] == 0 else 'unknown'

            # Step 6: Determine capability tier
            tier_result = NetworkProvisioner._exec_remote(
                client,
                f'curl -s http://localhost:{backend_port}/api/social/dashboard/health',
                timeout=10,
            )
            tier = 'unknown'
            if tier_result['exit_code'] == 0:
                try:
                    health = json.loads(tier_result['stdout'])
                    tier = health.get('capability_tier', 'unknown')
                except (json.JSONDecodeError, KeyError):
                    pass

            # Read actual installed version from remote
            ver_result = NetworkProvisioner._exec_remote(
                client,
                'cat /opt/hyve/VERSION 2>/dev/null || echo "1.0.0"',
            )
            installed_version = ver_result['stdout'].strip() or '1.0.0'

            # Step 7: Record in database
            NetworkProvisioner._record_provision(
                target_host=target_host,
                ssh_user=ssh_user,
                node_id=node_id,
                capability_tier=tier,
                provisioned_by=provisioned_by,
                installed_version=installed_version,
            )

            # Clean up remote temp files
            NetworkProvisioner._exec_remote(client, 'rm -rf /tmp/hyve-install')

            logger.info("Successfully provisioned %s — node_id=%s tier=%s",
                         target_host, node_id[:16], tier)

            return {
                'success': True,
                'node_id': node_id,
                'tier': tier,
                'target_host': target_host,
                'backend_url': f'http://{target_host}:{backend_port}',
                'system_info': preflight.get('system_info', {}),
                'message': f'HyveOS installed on {target_host}. '
                           f'Node ID: {node_id[:16]}..., Tier: {tier}',
            }

        except Exception as e:
            logger.error("Provisioning failed for %s: %s", target_host, e)
            return {'success': False, 'error': str(e)}

        finally:
            client.close()

    @staticmethod
    def _record_provision(target_host: str, ssh_user: str, node_id: str,
                          capability_tier: str, provisioned_by: str,
                          installed_version: str):
        """Record successful provisioning in the database."""
        try:
            from integrations.social.models import get_db, ProvisionedNode
            db = get_db()
            try:
                existing = db.query(ProvisionedNode).filter_by(
                    target_host=target_host).first()
                if existing:
                    existing.node_id = node_id
                    existing.capability_tier = capability_tier
                    existing.status = 'active'
                    existing.installed_version = installed_version
                    existing.provisioned_at = datetime.utcnow()
                    existing.last_health_check = datetime.utcnow()
                    existing.error_message = None
                else:
                    node = ProvisionedNode(
                        target_host=target_host,
                        ssh_user=ssh_user,
                        node_id=node_id,
                        capability_tier=capability_tier,
                        status='active',
                        installed_version=installed_version,
                        provisioned_at=datetime.utcnow(),
                        provisioned_by=provisioned_by,
                        last_health_check=datetime.utcnow(),
                    )
                    db.add(node)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Could not record provisioning: %s", e)

    @staticmethod
    def discover_network_targets(subnet: str = None) -> List[Dict]:
        """Scan local network for machines with SSH accessible.

        Args:
            subnet: CIDR notation (e.g., '192.168.1.0/24').
                    Auto-detects if None.

        Returns:
            List of {ip, hostname, ssh_accessible, port_22_open}
        """
        targets = []

        if subnet is None:
            # Auto-detect local subnet from default gateway
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
                # Assume /24 subnet
                parts = local_ip.split('.')
                subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
            except Exception:
                return []

        # Parse subnet
        try:
            base_parts = subnet.split('/')[0].split('.')
            prefix = int(subnet.split('/')[1])
            if prefix != 24:
                logger.warning("Only /24 subnets supported for scan")
                return targets
        except (IndexError, ValueError):
            return targets

        base = f"{base_parts[0]}.{base_parts[1]}.{base_parts[2]}"

        # Scan port 22 on each IP (quick socket connect)
        for i in range(1, 255):
            ip = f"{base}.{i}"
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                result = sock.connect_ex((ip, 22))
                sock.close()

                if result == 0:
                    # Try reverse DNS
                    try:
                        hostname = socket.gethostbyaddr(ip)[0]
                    except socket.herror:
                        hostname = ip

                    targets.append({
                        'ip': ip,
                        'hostname': hostname,
                        'ssh_accessible': True,
                        'port_22_open': True,
                    })
            except Exception:
                continue

        return targets

    @staticmethod
    def check_remote_health(target_host: str, ssh_user: str = 'root',
                            ssh_key_path: str = None,
                            ssh_password: str = None) -> Dict:
        """Check health of a provisioned HyveOS node via SSH."""
        if not PARAMIKO_AVAILABLE:
            return {'healthy': False, 'error': 'paramiko not installed'}

        try:
            client = NetworkProvisioner._get_ssh_client(
                target_host, ssh_user, ssh_key_path, ssh_password)
        except Exception as e:
            return {'healthy': False, 'error': f'SSH failed: {e}'}

        try:
            health = {'target_host': target_host, 'services': {}}

            # Check each service
            for svc in ['hyve-backend', 'hyve-discovery', 'hyve-agent-daemon',
                        'hyve-vision', 'hyve-llm']:
                result = NetworkProvisioner._exec_remote(
                    client, f'systemctl is-active {svc}.service')
                health['services'][svc] = result['stdout'].strip()

            # Backend HTTP health
            result = NetworkProvisioner._exec_remote(
                client, 'curl -s http://localhost:6777/status')
            health['backend_responding'] = result['exit_code'] == 0

            # Node ID
            result = NetworkProvisioner._exec_remote(
                client, 'cat /var/lib/hyve/node_public.key | xxd -p | tr -d "\\n"')
            health['node_id'] = result['stdout'][:64] if result['exit_code'] == 0 else 'unknown'

            # Uptime
            result = NetworkProvisioner._exec_remote(client, 'uptime -p')
            health['uptime'] = result['stdout'].strip()

            health['healthy'] = (
                health['services'].get('hyve-backend') == 'active' and
                health['backend_responding']
            )

            # Update DB record
            try:
                from integrations.social.models import get_db, ProvisionedNode
                db = get_db()
                try:
                    node = db.query(ProvisionedNode).filter_by(
                        target_host=target_host).first()
                    if node:
                        node.last_health_check = datetime.utcnow()
                        node.status = 'active' if health['healthy'] else 'offline'
                        db.commit()
                finally:
                    db.close()
            except Exception:
                pass

            return health

        finally:
            client.close()

    @staticmethod
    def update_remote(target_host: str, ssh_user: str = 'root',
                      ssh_key_path: str = None,
                      ssh_password: str = None) -> Dict:
        """Update HyveOS on a remote provisioned node.

        Uses rsync-over-SSH to transfer updated code (no git required).
        Falls back to full archive transfer if rsync is unavailable.
        """
        if not PARAMIKO_AVAILABLE:
            return {'success': False, 'error': 'paramiko not installed'}

        try:
            client = NetworkProvisioner._get_ssh_client(
                target_host, ssh_user, ssh_key_path, ssh_password)
        except Exception as e:
            return {'success': False, 'error': f'SSH failed: {e}'}

        try:
            # Stop services before update
            NetworkProvisioner._exec_remote(client, 'systemctl stop hyve.target')

            # Build fresh archive and transfer
            logger.info("Transferring update to %s...", target_host)
            repo_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), '..', '..'))

            EXCLUDE_TOP = {'.git', '__pycache__', 'venv310', 'venv',
                           'tests', '.idea', 'autogen-0.2.37', 'docs',
                           '.pycharm_plugin', 'dist', 'node_modules'}

            def _tar_filter(tarinfo):
                parts = tarinfo.name.split('/')
                for part in parts:
                    if part == '__pycache__' or part.endswith('.egg-info'):
                        return None
                if tarinfo.name.endswith(('.pyc', '.pyo')):
                    return None
                return tarinfo

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                for item in os.listdir(repo_root):
                    if item in EXCLUDE_TOP:
                        continue
                    full_path = os.path.join(repo_root, item)
                    tar.add(full_path, arcname=item, filter=_tar_filter)
            buf.seek(0)

            # Transfer and extract (preserve config and data)
            sftp = client.open_sftp()
            sftp.putfo(buf, '/tmp/hyve-update.tar.gz')
            sftp.close()

            NetworkProvisioner._exec_remote(
                client,
                'mkdir -p /tmp/hyve-update-extract && '
                'tar xzf /tmp/hyve-update.tar.gz -C /tmp/hyve-update-extract && '
                'rsync -a --exclude=.env --exclude="agent_data/*.db" '
                '--exclude="agent_data/*.json" '
                '/tmp/hyve-update-extract/ /opt/hyve/ && '
                'rm -rf /tmp/hyve-update.tar.gz /tmp/hyve-update-extract',
                timeout=120,
            )

            # Update pip dependencies
            NetworkProvisioner._exec_remote(
                client,
                '/opt/hyve/venv/bin/pip install -r /opt/hyve/requirements.txt -q',
                timeout=120,
            )

            # Restart services
            NetworkProvisioner._exec_remote(
                client, 'systemctl daemon-reload && systemctl start hyve.target')

            # Wait for backend
            for _ in range(15):
                check = NetworkProvisioner._exec_remote(
                    client, 'curl -s http://localhost:6777/status', timeout=5)
                if check['exit_code'] == 0:
                    # Update DB record
                    ver_result = NetworkProvisioner._exec_remote(
                        client, 'cat /opt/hyve/VERSION 2>/dev/null || echo "unknown"')
                    try:
                        from integrations.social.models import get_db, ProvisionedNode
                        db = get_db()
                        try:
                            node = db.query(ProvisionedNode).filter_by(
                                target_host=target_host).first()
                            if node:
                                node.installed_version = ver_result['stdout'].strip()
                                node.last_health_check = datetime.utcnow()
                                node.status = 'active'
                                db.commit()
                        finally:
                            db.close()
                    except Exception:
                        pass
                    return {'success': True,
                            'message': f'Updated and restarted {target_host}'}
                time.sleep(2)

            return {'success': False, 'error': 'Backend did not restart after update'}

        except Exception as e:
            # Try to restart services even if update failed
            try:
                NetworkProvisioner._exec_remote(
                    client, 'systemctl start hyve.target')
            except Exception:
                pass
            return {'success': False, 'error': str(e)}

        finally:
            client.close()

    @staticmethod
    def list_provisioned() -> List[Dict]:
        """List all provisioned nodes from the database."""
        try:
            from integrations.social.models import get_db, ProvisionedNode
            db = get_db()
            try:
                nodes = db.query(ProvisionedNode).all()
                return [
                    {
                        'id': n.id,
                        'target_host': n.target_host,
                        'ssh_user': n.ssh_user,
                        'node_id': n.node_id,
                        'capability_tier': n.capability_tier,
                        'status': n.status,
                        'installed_version': n.installed_version,
                        'provisioned_at': n.provisioned_at.isoformat() if n.provisioned_at else None,
                        'last_health_check': n.last_health_check.isoformat() if n.last_health_check else None,
                        'provisioned_by': n.provisioned_by,
                    }
                    for n in nodes
                ]
            finally:
                db.close()
        except Exception as e:
            logger.warning("Could not list provisioned nodes: %s", e)
            return []
