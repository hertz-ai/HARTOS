"""
Private Repo Access Service — GitHub invite/revoke + access control

Controls access to private repos (e.g. HevolveAI hivemind core).
Regional hosts get push access via GitHub collaborator invite after
steward approval. Central has full access. Local nodes are denied.

Uses GitHub REST API via `gh` CLI or direct HTTP with PAT.
"""
import json
import logging
import os
import re
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')

# GitHub username: alphanumeric + hyphens, 1-39 chars, no leading/trailing hyphen
_GITHUB_USERNAME_RE = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$')


def _validate_github_username(username: str) -> bool:
    """Validate GitHub username format to prevent API path traversal."""
    return bool(username and _GITHUB_USERNAME_RE.match(username))

_PRIVATE_REPOS = None
_GITHUB_TOKEN = None


def _get_private_repos() -> List[str]:
    global _PRIVATE_REPOS
    if _PRIVATE_REPOS is None:
        raw = os.environ.get('HEVOLVE_PRIVATE_REPOS', '')
        _PRIVATE_REPOS = [r.strip() for r in raw.split(',') if r.strip()]
    return _PRIVATE_REPOS


def _get_github_token() -> str:
    global _GITHUB_TOKEN
    if _GITHUB_TOKEN is None:
        _GITHUB_TOKEN = os.environ.get('HEVOLVE_GITHUB_TOKEN', '')
    return _GITHUB_TOKEN


class PrivateRepoAccessService:
    """Access control for private repos. Static methods only."""

    @staticmethod
    def is_private_repo(repo_url: str) -> bool:
        """Check if a repo URL is in the private repos list."""
        repos = _get_private_repos()
        if not repos:
            return False
        # Normalize: strip .git, trailing slash
        normalized = repo_url.rstrip('/').removesuffix('.git').lower()
        for r in repos:
            rn = r.rstrip('/').removesuffix('.git').lower()
            if rn and (normalized == rn or normalized.endswith('/' + rn)):
                return True
        return False

    @staticmethod
    def verify_access(
        node_certificate: Optional[Dict],
        repo_url: str,
        access_level: str = 'read',
    ) -> Dict:
        """Verify a node's access to a private repo.

        Central: full read/write
        Regional (with valid certificate + invite): push to branches
        Local: DENIED
        """
        if not PrivateRepoAccessService.is_private_repo(repo_url):
            return {'allowed': True, 'reason': 'Not a private repo'}

        if not node_certificate:
            return {'allowed': False, 'reason': 'No certificate provided'}

        tier = node_certificate.get('tier', 'local')

        if tier == 'central':
            return {'allowed': True, 'tier': 'central',
                    'access_level': 'full'}

        if tier == 'regional':
            # Regional hosts can push if they have a valid certificate
            try:
                from security.key_delegation import verify_certificate_chain
                valid = verify_certificate_chain(node_certificate)
                if not valid:
                    return {'allowed': False,
                            'reason': 'Invalid certificate chain'}
            except Exception as e:
                logger.debug(f"Certificate verification failed: {e}")
                return {'allowed': False,
                        'reason': f'Certificate verification error: {e}'}

            if access_level in ('read', 'push'):
                return {'allowed': True, 'tier': 'regional',
                        'access_level': 'push'}
            return {'allowed': False,
                    'reason': 'Regional hosts limited to push access'}

        return {'allowed': False, 'tier': tier,
                'reason': 'Only central and regional hosts can access '
                          'private repos'}

    @staticmethod
    def send_github_invite(
        repo_url: str,
        github_username: str,
        permission: str = 'push',
    ) -> Dict:
        """Send GitHub collaborator invite.

        Uses gh CLI if available, falls back to HTTP API.
        permission: 'pull', 'push', or 'admin'
        """
        if not github_username:
            return {'invited': False, 'error': 'No GitHub username'}
        if not _validate_github_username(github_username):
            return {'invited': False,
                    'error': f'Invalid GitHub username format: {github_username}'}

        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            return {'invited': False,
                    'error': f'Cannot parse repo: {repo_url}'}

        owner, repo = owner_repo

        # Try gh CLI first
        try:
            result = subprocess.run(
                ['gh', 'api', '--method', 'PUT',
                 f'repos/{owner}/{repo}/collaborators/{github_username}',
                 '-f', f'permission={permission}'],
                capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info(
                    f"GitHub invite sent: {github_username} → "
                    f"{owner}/{repo} ({permission})")
                return {'invited': True, 'method': 'gh_cli',
                        'username': github_username}
        except FileNotFoundError:
            pass  # gh CLI not installed, try HTTP
        except Exception as e:
            logger.debug(f"gh CLI invite failed: {e}")

        # Fallback: direct HTTP with PAT
        token = _get_github_token()
        if not token:
            return {'invited': False,
                    'error': 'No HEVOLVE_GITHUB_TOKEN configured'}

        try:
            import requests
            resp = requests.put(
                f'https://api.github.com/repos/{owner}/{repo}'
                f'/collaborators/{github_username}',
                headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github.v3+json',
                },
                json={'permission': permission},
                timeout=30,
            )
            if resp.status_code in (201, 204):
                logger.info(
                    f"GitHub invite sent via API: {github_username} → "
                    f"{owner}/{repo}")
                return {'invited': True, 'method': 'http_api',
                        'username': github_username}
            return {'invited': False, 'status': resp.status_code,
                    'error': resp.text[:200]}
        except Exception as e:
            return {'invited': False, 'error': str(e)}

    @staticmethod
    def revoke_github_access(
        repo_url: str,
        github_username: str,
    ) -> Dict:
        """Revoke GitHub collaborator access."""
        if not github_username:
            return {'revoked': False, 'error': 'No GitHub username'}
        if not _validate_github_username(github_username):
            return {'revoked': False,
                    'error': f'Invalid GitHub username format: {github_username}'}

        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            return {'revoked': False,
                    'error': f'Cannot parse repo: {repo_url}'}

        owner, repo = owner_repo

        # Try gh CLI first
        try:
            result = subprocess.run(
                ['gh', 'api', '--method', 'DELETE',
                 f'repos/{owner}/{repo}/collaborators/{github_username}'],
                capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info(
                    f"GitHub access revoked: {github_username} from "
                    f"{owner}/{repo}")
                return {'revoked': True, 'method': 'gh_cli'}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"gh CLI revoke failed: {e}")

        # Fallback: HTTP API
        token = _get_github_token()
        if not token:
            return {'revoked': False,
                    'error': 'No HEVOLVE_GITHUB_TOKEN configured'}

        try:
            import requests
            resp = requests.delete(
                f'https://api.github.com/repos/{owner}/{repo}'
                f'/collaborators/{github_username}',
                headers={
                    'Authorization': f'token {token}',
                    'Accept': 'application/vnd.github.v3+json',
                },
                timeout=30,
            )
            if resp.status_code == 204:
                return {'revoked': True, 'method': 'http_api'}
            return {'revoked': False, 'status': resp.status_code,
                    'error': resp.text[:200]}
        except Exception as e:
            return {'revoked': False, 'error': str(e)}

    @staticmethod
    def split_repo_task(
        task_description: str,
        repo_url: str,
        target_files: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Central splits a full-repo task into file-level subtasks.

        Regional hosts receive individual file-level subtasks instead of
        full repository access. Central coordinates and merges results.
        """
        if not target_files:
            # Infer target files from task description keywords
            target_files = []

        subtasks = []
        for i, fpath in enumerate(target_files):
            subtasks.append({
                'subtask_id': i + 1,
                'file_path': fpath,
                'repo_url': repo_url,
                'description': f'{task_description} [file: {fpath}]',
                'access_level': 'push',
            })

        if not subtasks:
            subtasks.append({
                'subtask_id': 1,
                'file_path': None,
                'repo_url': repo_url,
                'description': task_description,
                'access_level': 'push',
            })

        return subtasks

    @staticmethod
    def create_file_extract(
        repo_url: str,
        file_paths: List[str],
    ) -> Dict:
        """Extract specific files from a repo for delegation.

        Regional gets file content, NOT full clone.
        Uses gh CLI to fetch individual file contents.
        """
        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            return {'error': f'Cannot parse repo: {repo_url}'}

        owner, repo = owner_repo
        files = {}

        for fpath in file_paths:
            # Sanitize: reject path traversal attempts
            if '..' in fpath or fpath.startswith('/') or '\\' in fpath:
                files[fpath] = None
                logger.warning(f"Path traversal rejected in file extract: {fpath}")
                continue
            try:
                result = subprocess.run(
                    ['gh', 'api',
                     f'repos/{owner}/{repo}/contents/{fpath}',
                     '--jq', '.content'],
                    capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout.strip():
                    import base64
                    content = base64.b64decode(
                        result.stdout.strip()).decode('utf-8', errors='replace')
                    files[fpath] = content
                else:
                    files[fpath] = None
            except Exception as e:
                files[fpath] = None
                logger.debug(f"File extract failed for {fpath}: {e}")

        return {'repo_url': repo_url, 'files': files,
                'extracted': sum(1 for v in files.values() if v is not None)}


def _extract_owner_repo(repo_url: str) -> Optional[tuple]:
    """Extract (owner, repo) from a GitHub URL or owner/repo string."""
    if not repo_url:
        return None

    # Handle owner/repo format
    if '/' in repo_url and '://' not in repo_url:
        parts = repo_url.strip('/').split('/')
        if len(parts) == 2:
            return (parts[0], parts[1].removesuffix('.git'))

    # Handle full URL
    try:
        from urllib.parse import urlparse
        parsed = urlparse(repo_url)
        path = parsed.path.strip('/').removesuffix('.git')
        parts = path.split('/')
        if len(parts) >= 2:
            return (parts[0], parts[1])
    except Exception:
        pass

    return None
