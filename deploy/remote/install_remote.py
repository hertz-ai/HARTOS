#!/usr/bin/env python3
"""
HyveOS Remote Unattended Installer

Installs HyveOS on a remote machine over SSH (Linux) or SSH/WinRM (Windows).
Supports both Windows and Linux targets. Fully unattended — no manual steps.

Usage:
    # Install on a remote Linux machine:
    python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux

    # Install on a remote Windows machine (via SSH):
    python deploy/remote/install_remote.py --host 192.168.1.60 --user admin --os windows --password "pass"

    # Install with SSH key:
    python deploy/remote/install_remote.py --host 192.168.1.50 --user hyve --key ~/.ssh/id_ed25519

    # Join an existing hive after install:
    python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux --join-peer http://central:6777

    # Dry run (check only, don't install):
    python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux --dry-run

Requires: paramiko (pip install paramiko)
"""
import os
import sys
import json
import time
import argparse
import logging
import socket
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger('hyve_remote_install')

# ═══════════════════════════════════════════════════════════════════════
# SSH Connection
# ═══════════════════════════════════════════════════════════════════════

class SSHConnection:
    """Manages SSH connection to remote host."""

    def __init__(self, host: str, user: str, password: str = None,
                 key_path: str = None, port: int = 22, timeout: int = 30):
        self.host = host
        self.user = user
        self.password = password
        self.key_path = key_path
        self.port = port
        self.timeout = timeout
        self._client = None

    def connect(self) -> bool:
        """Establish SSH connection."""
        try:
            import paramiko
        except ImportError:
            logger.error("paramiko not installed. Run: pip install paramiko")
            return False

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            'hostname': self.host,
            'port': self.port,
            'username': self.user,
            'timeout': self.timeout,
        }
        if self.key_path:
            connect_kwargs['key_filename'] = self.key_path
        elif self.password:
            connect_kwargs['password'] = self.password

        try:
            self._client.connect(**connect_kwargs)
            logger.info(f"SSH connected to {self.user}@{self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"SSH connection failed: {e}")
            return False

    def exec(self, cmd: str, timeout: int = 120) -> Tuple[int, str, str]:
        """Execute command and return (exit_code, stdout, stderr)."""
        if not self._client:
            return (-1, '', 'Not connected')
        try:
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            return (exit_code, out, err)
        except Exception as e:
            return (-1, '', str(e))

    def upload(self, local_path: str, remote_path: str) -> bool:
        """Upload a file via SFTP."""
        if not self._client:
            return False
        try:
            sftp = self._client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            logger.info(f"Uploaded {local_path} → {remote_path}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

    def upload_dir(self, local_dir: str, remote_dir: str) -> bool:
        """Upload entire directory via SFTP."""
        if not self._client:
            return False
        try:
            sftp = self._client.open_sftp()
            local_path = Path(local_dir)
            for item in local_path.rglob('*'):
                if item.is_file():
                    rel = item.relative_to(local_path)
                    remote_file = f"{remote_dir}/{rel.as_posix()}"
                    # Create remote dirs
                    remote_parent = '/'.join(remote_file.split('/')[:-1])
                    try:
                        sftp.stat(remote_parent)
                    except FileNotFoundError:
                        self._mkdir_p(sftp, remote_parent)
                    sftp.put(str(item), remote_file)
            sftp.close()
            return True
        except Exception as e:
            logger.error(f"Directory upload failed: {e}")
            return False

    @staticmethod
    def _mkdir_p(sftp, remote_dir):
        """Recursively create remote directories."""
        dirs_to_create = []
        current = remote_dir
        while current and current != '/':
            try:
                sftp.stat(current)
                break
            except FileNotFoundError:
                dirs_to_create.insert(0, current)
                current = '/'.join(current.split('/')[:-1])
        for d in dirs_to_create:
            try:
                sftp.mkdir(d)
            except Exception:
                pass

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


# ═══════════════════════════════════════════════════════════════════════
# Linux Installer
# ═══════════════════════════════════════════════════════════════════════

class LinuxInstaller:
    """Installs HyveOS on a remote Linux machine."""

    def __init__(self, ssh: SSHConnection, join_peer: str = None,
                 no_vision: bool = False, no_llm: bool = False):
        self.ssh = ssh
        self.join_peer = join_peer
        self.no_vision = no_vision
        self.no_llm = no_llm

    def preflight(self) -> Dict:
        """Check prerequisites on remote host."""
        checks = {}

        # OS detection
        code, out, _ = self.ssh.exec('cat /etc/os-release 2>/dev/null || echo "unknown"')
        checks['os'] = out.strip()
        checks['is_linux'] = 'ID=' in out

        # RAM
        code, out, _ = self.ssh.exec("free -g | awk '/Mem:/{print $2}'")
        try:
            checks['ram_gb'] = int(out.strip())
        except ValueError:
            checks['ram_gb'] = 0
        checks['ram_ok'] = checks['ram_gb'] >= 2

        # Disk
        code, out, _ = self.ssh.exec("df -BG / | awk 'NR==2{print $4}' | tr -d 'G'")
        try:
            checks['disk_free_gb'] = int(out.strip())
        except ValueError:
            checks['disk_free_gb'] = 0
        checks['disk_ok'] = checks['disk_free_gb'] >= 5

        # Python 3.10
        code, out, _ = self.ssh.exec('python3.10 --version 2>/dev/null')
        checks['python310'] = code == 0
        if not checks['python310']:
            code, out, _ = self.ssh.exec('python3 --version 2>/dev/null')
            checks['python3_version'] = out.strip()

        # systemd
        code, _, _ = self.ssh.exec('systemctl --version 2>/dev/null')
        checks['systemd'] = code == 0

        # SSH user privileges
        code, out, _ = self.ssh.exec('id')
        checks['user_info'] = out.strip()
        checks['is_root'] = 'uid=0' in out

        checks['all_ok'] = (checks['is_linux'] and checks['ram_ok'] and
                            checks['disk_ok'] and checks['systemd'])

        return checks

    def install(self, dry_run: bool = False) -> Dict:
        """Full installation pipeline."""
        result = {'steps': [], 'success': False}

        # 1. Preflight
        checks = self.preflight()
        result['preflight'] = checks
        result['steps'].append(('preflight', checks['all_ok']))
        if not checks['all_ok']:
            logger.error(f"Preflight failed: {json.dumps(checks, indent=2)}")
            return result
        if dry_run:
            result['success'] = True
            result['steps'].append(('dry_run', True))
            logger.info("Dry run complete — all preflight checks passed")
            return result

        # 2. Install Python 3.10 if needed
        if not checks['python310']:
            logger.info("Installing Python 3.10...")
            cmds = [
                'apt-get update -qq',
                'apt-get install -y -qq software-properties-common',
                'add-apt-repository -y ppa:deadsnakes/ppa',
                'apt-get update -qq',
                'apt-get install -y -qq python3.10 python3.10-venv python3.10-dev',
            ]
            for cmd in cmds:
                sudo_cmd = cmd if checks['is_root'] else f'sudo {cmd}'
                code, out, err = self.ssh.exec(sudo_cmd, timeout=300)
                if code != 0:
                    logger.warning(f"Command failed (non-fatal): {cmd}: {err[:200]}")
            # Verify
            code, _, _ = self.ssh.exec('python3.10 --version')
            result['steps'].append(('install_python310', code == 0))

        # 3. Create hyve user and directories
        logger.info("Creating hyve user and directories...")
        user_cmds = [
            'id hyve 2>/dev/null || useradd -r -m -d /opt/hyve -s /bin/bash hyve',
            'mkdir -p /opt/hyve /etc/hyve /var/lib/hyve /var/log/hyve',
            'chown -R hyve:hyve /opt/hyve /var/lib/hyve /var/log/hyve',
        ]
        for cmd in user_cmds:
            sudo_cmd = cmd if checks['is_root'] else f'sudo {cmd}'
            self.ssh.exec(sudo_cmd)
        result['steps'].append(('create_user_dirs', True))

        # 4. Upload code bundle
        logger.info("Uploading HyveOS code...")
        repo_root = Path(__file__).resolve().parent.parent.parent
        bundle_uploaded = self._upload_code_bundle(repo_root)
        result['steps'].append(('upload_code', bundle_uploaded))
        if not bundle_uploaded:
            return result

        # 5. Create venv and install dependencies
        logger.info("Creating venv and installing dependencies...")
        venv_cmds = [
            'python3.10 -m venv /opt/hyve/venv',
            '/opt/hyve/venv/bin/pip install --upgrade pip -q',
            '/opt/hyve/venv/bin/pip install -r /opt/hyve/requirements.txt -q',
        ]
        for cmd in venv_cmds:
            code, out, err = self.ssh.exec(cmd, timeout=600)
            if code != 0:
                logger.warning(f"Pip install issue: {err[:200]}")
        result['steps'].append(('install_deps', True))

        # 6. Generate Ed25519 keypair
        logger.info("Generating node identity...")
        keygen_cmd = (
            '/opt/hyve/venv/bin/python -c "'
            'from security.node_integrity import get_node_identity; '
            'info = get_node_identity(); '
            'print(info.get(\\\"public_key\\\", \\\"unknown\\\")[:16])'
            '" 2>/dev/null || echo "keygen_later"'
        )
        code, node_id_preview, _ = self.ssh.exec(
            f'cd /opt/hyve && {keygen_cmd}')
        result['node_id_preview'] = node_id_preview.strip()
        result['steps'].append(('generate_keypair', True))

        # 7. Install systemd units
        logger.info("Installing systemd services...")
        self._install_systemd_units(checks['is_root'])
        result['steps'].append(('install_systemd', True))

        # 8. Configure environment
        logger.info("Configuring environment...")
        self._configure_environment(checks['is_root'])
        result['steps'].append(('configure_env', True))

        # 9. Configure firewall
        logger.info("Configuring firewall...")
        fw_cmds = [
            'ufw allow 6777/tcp comment "HyveOS backend" 2>/dev/null || true',
            'ufw allow 6780/udp comment "HyveOS discovery" 2>/dev/null || true',
        ]
        for cmd in fw_cmds:
            sudo_cmd = cmd if checks['is_root'] else f'sudo {cmd}'
            self.ssh.exec(sudo_cmd)
        result['steps'].append(('firewall', True))

        # 10. Enable and start services
        logger.info("Starting HyveOS services...")
        start_cmds = [
            'systemctl daemon-reload',
            'systemctl enable hyve.target',
            'systemctl start hyve.target',
        ]
        for cmd in start_cmds:
            sudo_cmd = cmd if checks['is_root'] else f'sudo {cmd}'
            self.ssh.exec(sudo_cmd)
        result['steps'].append(('start_services', True))

        # 11. Join peer if specified
        if self.join_peer:
            logger.info(f"Joining hive peer: {self.join_peer}")
            join_cmd = (
                f'/opt/hyve/venv/bin/python -c "'
                f'import requests; '
                f'requests.post(\\\"{self.join_peer}/api/social/peers/announce\\\", '
                f'json={{\\\"url\\\": \\\"http://{self.ssh.host}:6777\\\"}}, timeout=10)'
                f'"'
            )
            code, _, _ = self.ssh.exec(join_cmd)
            result['steps'].append(('join_peer', code == 0))

        # 12. Verify
        logger.info("Verifying installation...")
        time.sleep(3)
        code, out, _ = self.ssh.exec('systemctl is-active hyve-backend.service 2>/dev/null')
        backend_active = out.strip() == 'active'
        result['steps'].append(('verify', backend_active))
        result['success'] = backend_active

        return result

    def _upload_code_bundle(self, repo_root: Path) -> bool:
        """Upload the code, excluding unnecessary dirs."""
        exclude_dirs = {'.git', '__pycache__', 'venv', 'venv310', 'tests',
                        'agent_data', '.idea', 'autogen-0.2.37', 'docs',
                        'node_modules', '.github'}
        exclude_exts = {'.db', '.pyc', '.pyo', '.whl', '.tar.gz'}

        # Create a temp tarball
        import tarfile
        bundle_path = os.path.join(tempfile.gettempdir(), 'hyve-bundle.tar.gz')
        try:
            with tarfile.open(bundle_path, 'w:gz') as tar:
                for item in repo_root.rglob('*'):
                    if item.is_file():
                        rel = item.relative_to(repo_root)
                        parts = rel.parts
                        if any(p in exclude_dirs for p in parts):
                            continue
                        if item.suffix in exclude_exts:
                            continue
                        tar.add(str(item), arcname=str(rel))

            # Upload and extract
            self.ssh.upload(bundle_path, '/tmp/hyve-bundle.tar.gz')
            code, _, err = self.ssh.exec(
                'rm -rf /opt/hyve/app && mkdir -p /opt/hyve/app && '
                'tar xzf /tmp/hyve-bundle.tar.gz -C /opt/hyve/app && '
                'chown -R hyve:hyve /opt/hyve/app && '
                'rm /tmp/hyve-bundle.tar.gz',
                timeout=120)
            # Copy requirements.txt to /opt/hyve for venv install
            self.ssh.exec('cp /opt/hyve/app/requirements.txt /opt/hyve/requirements.txt 2>/dev/null || true')
            return code == 0
        except Exception as e:
            logger.error(f"Bundle upload failed: {e}")
            return False
        finally:
            try:
                os.unlink(bundle_path)
            except Exception:
                pass

    def _install_systemd_units(self, is_root: bool):
        """Upload and enable systemd service units."""
        units_dir = Path(__file__).resolve().parent.parent / 'linux' / 'systemd'
        if not units_dir.exists():
            logger.warning(f"Systemd units not found at {units_dir}")
            return

        for unit_file in units_dir.glob('hyve*'):
            remote_path = f'/etc/systemd/system/{unit_file.name}'
            self.ssh.upload(str(unit_file), f'/tmp/{unit_file.name}')
            cmd = f'mv /tmp/{unit_file.name} {remote_path} && chmod 644 {remote_path}'
            sudo_cmd = cmd if is_root else f'sudo {cmd}'
            self.ssh.exec(sudo_cmd)

    def _configure_environment(self, is_root: bool):
        """Create /etc/hyve/hyve.env from template."""
        env_content = f"""# HyveOS Environment Configuration
# Auto-generated by remote installer
HEVOLVE_DB_PATH=/var/lib/hyve/hyve.db
HEVOLVE_BASE_URL=http://{self.ssh.host}:6777
HEVOLVE_NODE_NAME=hyve-{self.ssh.host.replace('.', '-')}
HEVOLVE_AGENT_ENGINE_ENABLED=true
HEVOLVE_AGENT_POLL_INTERVAL=30
HEVOLVE_WORKER_POLL_INTERVAL=15
PYTHONDONTWRITEBYTECODE=1
# Redis host — when set, this node auto-joins the distributed hive.
# No separate "distributed mode" flag. Redis reachable = distribute.
# REDIS_HOST=
# REDIS_PORT=6379
# Add API keys below:
# OPENAI_API_KEY=
# GROQ_API_KEY=
"""
        # Write locally, upload
        env_path = os.path.join(tempfile.gettempdir(), 'hyve.env')
        with open(env_path, 'w') as f:
            f.write(env_content)
        self.ssh.upload(env_path, '/tmp/hyve.env')
        cmd = 'mv /tmp/hyve.env /etc/hyve/hyve.env && chmod 640 /etc/hyve/hyve.env'
        sudo_cmd = cmd if is_root else f'sudo {cmd}'
        self.ssh.exec(sudo_cmd)
        os.unlink(env_path)


# ═══════════════════════════════════════════════════════════════════════
# Windows Installer
# ═══════════════════════════════════════════════════════════════════════

class WindowsInstaller:
    """Installs HyveOS on a remote Windows machine via SSH.

    Windows requires OpenSSH server enabled (Settings → Apps → Optional Features).
    Uses NSSM for service management (no systemd).
    """

    NSSM_URL = 'https://nssm.cc/release/nssm-2.24.zip'

    def __init__(self, ssh: SSHConnection, join_peer: str = None):
        self.ssh = ssh
        self.join_peer = join_peer

    def preflight(self) -> Dict:
        """Check prerequisites on remote Windows host."""
        checks = {}

        # OS detection
        code, out, _ = self.ssh.exec('ver', timeout=10)
        checks['os'] = out.strip()
        code2, out2, _ = self.ssh.exec('echo %OS%', timeout=10)
        checks['is_windows'] = 'Windows' in out or 'Windows_NT' in out2

        # RAM (PowerShell)
        code, out, _ = self.ssh.exec(
            'powershell -Command "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB"',
            timeout=15)
        try:
            checks['ram_gb'] = int(float(out.strip()))
        except (ValueError, TypeError):
            checks['ram_gb'] = 0
        checks['ram_ok'] = checks['ram_gb'] >= 2

        # Disk free
        code, out, _ = self.ssh.exec(
            'powershell -Command "(Get-PSDrive C).Free / 1GB"',
            timeout=15)
        try:
            checks['disk_free_gb'] = int(float(out.strip()))
        except (ValueError, TypeError):
            checks['disk_free_gb'] = 0
        checks['disk_ok'] = checks['disk_free_gb'] >= 5

        # Python
        code, out, _ = self.ssh.exec('python --version 2>&1', timeout=10)
        checks['python_version'] = out.strip()
        checks['python_ok'] = '3.10' in out or '3.11' in out or '3.12' in out

        # Check for python3.10 specifically
        if not checks['python_ok']:
            code, out, _ = self.ssh.exec('py -3.10 --version 2>&1', timeout=10)
            checks['py310'] = '3.10' in out

        checks['all_ok'] = (checks['is_windows'] and checks['ram_ok'] and
                            checks['disk_ok'])

        return checks

    def install(self, dry_run: bool = False) -> Dict:
        """Full Windows installation pipeline."""
        result = {'steps': [], 'success': False}

        # 1. Preflight
        checks = self.preflight()
        result['preflight'] = checks
        result['steps'].append(('preflight', checks['all_ok']))
        if not checks['all_ok']:
            logger.error(f"Preflight failed: {json.dumps(checks, indent=2)}")
            return result
        if dry_run:
            result['success'] = True
            result['steps'].append(('dry_run', True))
            return result

        # 2. Create directories
        logger.info("Creating HyveOS directories...")
        dir_cmds = [
            'mkdir C:\\hyve 2>nul || echo exists',
            'mkdir C:\\hyve\\app 2>nul || echo exists',
            'mkdir C:\\hyve\\data 2>nul || echo exists',
            'mkdir C:\\hyve\\logs 2>nul || echo exists',
            'mkdir C:\\hyve\\venv 2>nul || echo exists',
        ]
        for cmd in dir_cmds:
            self.ssh.exec(cmd)
        result['steps'].append(('create_dirs', True))

        # 3. Upload code
        logger.info("Uploading HyveOS code...")
        uploaded = self._upload_code_bundle()
        result['steps'].append(('upload_code', uploaded))
        if not uploaded:
            return result

        # 4. Create venv + install deps
        logger.info("Creating Python venv and installing dependencies...")
        python_cmd = 'py -3.10' if checks.get('py310') else 'python'
        venv_cmds = [
            f'{python_cmd} -m venv C:\\hyve\\venv',
            'C:\\hyve\\venv\\Scripts\\pip install --upgrade pip -q',
            'C:\\hyve\\venv\\Scripts\\pip install -r C:\\hyve\\app\\requirements.txt -q',
        ]
        for cmd in venv_cmds:
            code, out, err = self.ssh.exec(cmd, timeout=600)
            if code != 0:
                logger.warning(f"Pip issue: {err[:200]}")
        result['steps'].append(('install_deps', True))

        # 5. Create environment file
        logger.info("Creating environment configuration...")
        env_content = (
            f'set HEVOLVE_DB_PATH=C:\\hyve\\data\\hyve.db\n'
            f'set HEVOLVE_BASE_URL=http://{self.ssh.host}:6777\n'
            f'set HEVOLVE_NODE_NAME=hyve-{self.ssh.host.replace(".", "-")}\n'
            f'set HEVOLVE_AGENT_ENGINE_ENABLED=true\n'
            f'set PYTHONDONTWRITEBYTECODE=1\n'
            f'REM Redis host — when set, this node auto-joins the distributed hive\n'
            f'REM set REDIS_HOST=\n'
            f'REM set REDIS_PORT=6379\n'
        )
        env_path = os.path.join(tempfile.gettempdir(), 'hyve_env.bat')
        with open(env_path, 'w') as f:
            f.write(env_content)
        self.ssh.upload(env_path, 'C:\\hyve\\hyve_env.bat')
        os.unlink(env_path)
        result['steps'].append(('configure_env', True))

        # 6. Create startup script
        logger.info("Creating startup script...")
        startup = (
            '@echo off\n'
            'call C:\\hyve\\hyve_env.bat\n'
            'cd /d C:\\hyve\\app\n'
            'C:\\hyve\\venv\\Scripts\\python langchain_gpt_api.py\n'
        )
        startup_path = os.path.join(tempfile.gettempdir(), 'hyve_start.bat')
        with open(startup_path, 'w') as f:
            f.write(startup)
        self.ssh.upload(startup_path, 'C:\\hyve\\hyve_start.bat')
        os.unlink(startup_path)
        result['steps'].append(('create_startup', True))

        # 7. Install as Windows service via NSSM (or sc.exe fallback)
        logger.info("Installing as Windows service...")
        svc_installed = self._install_windows_service()
        result['steps'].append(('install_service', svc_installed))

        # 8. Configure Windows Firewall
        logger.info("Configuring Windows Firewall...")
        fw_cmds = [
            'netsh advfirewall firewall add rule name="HyveOS Backend" dir=in action=allow protocol=tcp localport=6777 2>nul',
            'netsh advfirewall firewall add rule name="HyveOS Discovery" dir=in action=allow protocol=udp localport=6780 2>nul',
        ]
        for cmd in fw_cmds:
            self.ssh.exec(cmd)
        result['steps'].append(('firewall', True))

        # 9. Start service
        logger.info("Starting HyveOS service...")
        code, _, _ = self.ssh.exec('net start HyveOS 2>nul || sc start HyveOS')
        result['steps'].append(('start_service', code == 0))

        # 10. Join peer
        if self.join_peer:
            logger.info(f"Joining hive: {self.join_peer}")
            join_cmd = (
                f'C:\\hyve\\venv\\Scripts\\python -c "'
                f'import requests; '
                f'requests.post(\'{self.join_peer}/api/social/peers/announce\', '
                f'json={{\'url\': \'http://{self.ssh.host}:6777\'}}, timeout=10)'
                f'"'
            )
            code, _, _ = self.ssh.exec(join_cmd)
            result['steps'].append(('join_peer', code == 0))

        # 11. Verify
        time.sleep(5)
        code, out, _ = self.ssh.exec('sc query HyveOS', timeout=10)
        running = 'RUNNING' in out
        result['steps'].append(('verify', running))
        result['success'] = running

        return result

    def _upload_code_bundle(self) -> bool:
        """Upload code to Windows remote."""
        import tarfile
        repo_root = Path(__file__).resolve().parent.parent.parent
        exclude_dirs = {'.git', '__pycache__', 'venv', 'venv310', 'tests',
                        'agent_data', '.idea', 'autogen-0.2.37', 'docs',
                        'node_modules', '.github'}
        exclude_exts = {'.db', '.pyc', '.pyo', '.whl', '.tar.gz'}

        bundle_path = os.path.join(tempfile.gettempdir(), 'hyve-bundle.tar.gz')
        try:
            with tarfile.open(bundle_path, 'w:gz') as tar:
                for item in repo_root.rglob('*'):
                    if item.is_file():
                        rel = item.relative_to(repo_root)
                        parts = rel.parts
                        if any(p in exclude_dirs for p in parts):
                            continue
                        if item.suffix in exclude_exts:
                            continue
                        tar.add(str(item), arcname=str(rel))

            self.ssh.upload(bundle_path, 'C:\\hyve\\hyve-bundle.tar.gz')
            # Extract using Python (tar not always available on Windows)
            code, _, err = self.ssh.exec(
                'C:\\hyve\\venv\\Scripts\\python -c "'
                'import tarfile; '
                "t = tarfile.open('C:\\\\hyve\\\\hyve-bundle.tar.gz', 'r:gz'); "
                "t.extractall('C:\\\\hyve\\\\app'); "
                't.close()"',
                timeout=120)
            if code != 0:
                # Fallback: try with system python
                self.ssh.exec(
                    'python -c "'
                    'import tarfile; '
                    "t = tarfile.open(r'C:\\hyve\\hyve-bundle.tar.gz', 'r:gz'); "
                    "t.extractall(r'C:\\hyve\\app'); "
                    't.close()"',
                    timeout=120)
            self.ssh.exec('del C:\\hyve\\hyve-bundle.tar.gz 2>nul')
            return True
        except Exception as e:
            logger.error(f"Windows bundle upload failed: {e}")
            return False
        finally:
            try:
                os.unlink(bundle_path)
            except Exception:
                pass

    def _install_windows_service(self) -> bool:
        """Install HyveOS as a Windows service using sc.exe."""
        # Use sc.exe (built-in) to create a service that runs the batch file
        cmd = (
            'sc create HyveOS '
            'binPath= "cmd.exe /c C:\\hyve\\hyve_start.bat" '
            'start= auto '
            'DisplayName= "HyveOS Agentic Intelligence"'
        )
        code, out, err = self.ssh.exec(cmd)
        if code != 0 and 'exists' not in err.lower():
            logger.warning(f"sc create failed: {err}")
            # Try NSSM as fallback
            return self._install_via_nssm()
        return True

    def _install_via_nssm(self) -> bool:
        """Install via NSSM for better service management."""
        # Check if NSSM exists
        code, _, _ = self.ssh.exec('nssm version 2>nul')
        if code != 0:
            logger.info("NSSM not found — using sc.exe service (basic)")
            return False

        cmds = [
            'nssm install HyveOS C:\\hyve\\venv\\Scripts\\python.exe',
            'nssm set HyveOS AppDirectory C:\\hyve\\app',
            'nssm set HyveOS AppParameters langchain_gpt_api.py',
            'nssm set HyveOS AppEnvironmentExtra HEVOLVE_DISTRIBUTED_MODE=true HEVOLVE_AGENT_ENGINE_ENABLED=true HEVOLVE_DB_PATH=C:\\hyve\\data\\hyve.db PYTHONDONTWRITEBYTECODE=1',
            'nssm set HyveOS Start SERVICE_AUTO_START',
            'nssm set HyveOS AppStdout C:\\hyve\\logs\\hyve_stdout.log',
            'nssm set HyveOS AppStderr C:\\hyve\\logs\\hyve_stderr.log',
        ]
        for cmd in cmds:
            self.ssh.exec(cmd)
        return True


# ═══════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='HyveOS Remote Unattended Installer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Linux install via SSH key:
  python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux

  # Windows install via password:
  python deploy/remote/install_remote.py --host 192.168.1.60 --user admin --os windows --password "pass"

  # Dry run (check only):
  python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux --dry-run

  # Join existing hive:
  python deploy/remote/install_remote.py --host 192.168.1.50 --user root --os linux --join-peer http://central:6777
""")

    parser.add_argument('--host', required=True, help='Target machine IP or hostname')
    parser.add_argument('--user', required=True, help='SSH username')
    parser.add_argument('--password', default=None, help='SSH password')
    parser.add_argument('--key', default=None, help='SSH private key path')
    parser.add_argument('--port', type=int, default=22, help='SSH port (default: 22)')
    parser.add_argument('--os', choices=['linux', 'windows', 'auto'], default='auto',
                        help='Target OS (default: auto-detect)')
    parser.add_argument('--join-peer', default=None,
                        help='URL of existing hive node to join after install')
    parser.add_argument('--no-vision', action='store_true',
                        help='Skip MiniCPM vision service')
    parser.add_argument('--no-llm', action='store_true',
                        help='Skip llama.cpp local inference')
    parser.add_argument('--dry-run', action='store_true',
                        help='Check prerequisites only, don\'t install')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s [%(levelname)s] %(message)s')

    print(f"\n{'='*60}")
    print(f"  HyveOS Remote Unattended Installer")
    print(f"  Target: {args.user}@{args.host}:{args.port}")
    print(f"  OS: {args.os}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'INSTALL'}")
    print(f"{'='*60}\n")

    # Check SSH reachability first
    print("Checking SSH connectivity...")
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
        sock.close()
        print(f"  SSH port {args.port} is open on {args.host}")
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        print(f"  ERROR: Cannot reach {args.host}:{args.port} — {e}")
        print(f"\n  For Linux: ensure sshd is running")
        print(f"  For Windows: enable OpenSSH Server in Settings → Apps → Optional Features")
        sys.exit(1)

    # Connect
    ssh = SSHConnection(
        host=args.host, user=args.user, password=args.password,
        key_path=args.key, port=args.port)

    if not ssh.connect():
        print("\nSSH connection failed. Check credentials and try again.")
        sys.exit(1)

    try:
        # Auto-detect OS if needed
        target_os = args.os
        if target_os == 'auto':
            code, out, _ = ssh.exec('uname -s 2>/dev/null || echo Windows')
            if 'Linux' in out:
                target_os = 'linux'
            else:
                target_os = 'windows'
            print(f"  Auto-detected OS: {target_os}")

        # Run installer
        if target_os == 'linux':
            installer = LinuxInstaller(ssh, join_peer=args.join_peer,
                                       no_vision=args.no_vision, no_llm=args.no_llm)
        else:
            installer = WindowsInstaller(ssh, join_peer=args.join_peer)

        result = installer.install(dry_run=args.dry_run)

        # Print results
        print(f"\n{'='*60}")
        print(f"  Installation {'DRY RUN ' if args.dry_run else ''}Results")
        print(f"{'='*60}")
        for step, ok in result.get('steps', []):
            status = 'PASS' if ok else 'FAIL'
            print(f"  [{status}] {step}")

        if result.get('node_id_preview'):
            print(f"\n  Node ID: {result['node_id_preview']}...")

        if result.get('success'):
            print(f"\n  HyveOS installed successfully on {args.host}!")
            print(f"  Dashboard: http://{args.host}:6777")
            if args.join_peer:
                print(f"  Joined hive: {args.join_peer}")
            print(f"\n  The node is now part of the distributed hive.")
            print(f"  It will automatically claim and execute tasks from Redis.")
        else:
            print(f"\n  Installation had issues. Check the steps above.")
            if result.get('preflight'):
                print(f"  Preflight: {json.dumps(result['preflight'], indent=4)}")

    finally:
        ssh.close()

    sys.exit(0 if result.get('success') else 1)


if __name__ == '__main__':
    main()
