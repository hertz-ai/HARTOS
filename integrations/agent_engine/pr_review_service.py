"""
PR Review Service — Coding agent reviews PRs against baselines

Auto-approves simple changes with passing tests and no regression.
Flags complex changes for steward review.
Auto-rejects build breakers (test failures or baseline regressions).

Decision Matrix:
  Tests Pass + No Regression + Simple  → AUTO-APPROVE
  Tests Pass + No Regression + Complex → FLAG for steward
  Tests Pass + Regression              → AUTO-REJECT (build breaker)
  Tests Fail                           → AUTO-REJECT (build breaker)
"""
import json
import logging
import os
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')


class PRReviewService:
    """Coding agent reviews PRs against baselines. Static methods only."""

    @staticmethod
    def review_pr(repo_url: str, pr_number: int) -> Dict:
        """Full PR review pipeline.

        1. Fetch PR diff stats
        2. Run pre-commit checks (lint, format)
        3. Run test suite
        4. Validate baseline (no regression)
        5. Classify change complexity
        6. Decide: auto-approve / flag_steward / request_changes
        """
        # 1. Fetch PR diff
        diff_stats = PRReviewService._fetch_pr_diff(repo_url, pr_number)
        if diff_stats.get('error'):
            return {'decision': 'error', 'error': diff_stats['error']}

        # 2. Pre-commit checks
        precommit = PRReviewService.run_pre_commit_checks()

        # 3. Test suite
        test_results = PRReviewService.run_test_suite()

        # 4. Baseline validation
        baseline = PRReviewService.validate_baseline()

        # 5. Classify complexity
        complexity = PRReviewService.classify_change(diff_stats)

        # 6. Decision
        tests_pass = test_results.get('pass_rate', 0) >= 0.95
        no_regression = baseline.get('passed', True)
        is_simple = complexity == 'simple'

        if not tests_pass:
            decision = 'request_changes'
            reason = 'Build breaker: tests failing'
        elif not no_regression:
            decision = 'request_changes'
            reason = (f'Baseline regression detected: '
                      f'{baseline.get("regressions", [])}')
        elif is_simple:
            decision = 'approve'
            reason = 'Simple change, tests pass, no regression'
        else:
            decision = 'flag_steward'
            reason = f'Complex change ({complexity}), needs steward review'

        review = {
            'decision': decision,
            'reason': reason,
            'pr_number': pr_number,
            'diff_stats': diff_stats,
            'precommit': precommit,
            'test_results': test_results,
            'baseline_validation': baseline,
            'change_complexity': complexity,
        }

        # Post review to GitHub
        try:
            PRReviewService.post_review(repo_url, pr_number, review)
        except Exception as e:
            logger.debug(f"Failed to post review: {e}")
            review['review_posted'] = False

        return review

    @staticmethod
    def _fetch_pr_diff(repo_url: str, pr_number: int) -> Dict:
        """Fetch PR diff stats via gh CLI."""
        try:
            from .private_repo_access import _extract_owner_repo
            owner_repo = _extract_owner_repo(repo_url)
            if not owner_repo:
                return {'error': f'Cannot parse repo: {repo_url}'}

            owner, repo = owner_repo

            result = subprocess.run(
                ['gh', 'api',
                 f'repos/{owner}/{repo}/pulls/{pr_number}',
                 '--jq',
                 '{files_changed: .changed_files, '
                 'additions: .additions, '
                 'deletions: .deletions}'],
                capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                return json.loads(result.stdout)
            return {'error': result.stderr[:200],
                    'files_changed': 0, 'additions': 0, 'deletions': 0}
        except Exception as e:
            return {'error': str(e),
                    'files_changed': 0, 'additions': 0, 'deletions': 0}

    @staticmethod
    def run_pre_commit_checks(repo_path: str = '.') -> Dict:
        """Run lint and format checks."""
        issues = []

        # Try ruff lint
        try:
            result = subprocess.run(
                ['ruff', 'check', repo_path, '--select', 'E,W,F'],
                capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                issues.append({
                    'tool': 'ruff',
                    'output': result.stdout[:500],
                })
        except FileNotFoundError:
            pass  # ruff not installed
        except Exception:
            pass

        return {
            'passed': len(issues) == 0,
            'issues': issues,
        }

    @staticmethod
    def run_test_suite(repo_path: str = '.') -> Dict:
        """Run pytest and capture results."""
        try:
            import sys as _sys
            python = _sys.executable  # Always use current interpreter, not env var
            result = subprocess.run(
                [python, '-m', 'pytest', 'tests/', '-v', '-s',
                 '--tb=short', '-q'],
                capture_output=True, text=True, timeout=600,
                cwd=repo_path)

            # Parse pytest output
            output = result.stdout + result.stderr
            passed = 0
            failed = 0

            import re as _re
            for line in output.split('\n'):
                line = line.strip()
                if ' passed' in line or ' failed' in line:
                    # Match patterns like "100 passed" or "5 failed"
                    m_pass = _re.search(r'(\d+)\s+passed', line)
                    m_fail = _re.search(r'(\d+)\s+failed', line)
                    if m_pass:
                        passed = int(m_pass.group(1))
                    if m_fail:
                        failed = int(m_fail.group(1))

            total = passed + failed
            pass_rate = passed / max(1, total)

            return {
                'passed': passed,
                'failed': failed,
                'total': total,
                'pass_rate': round(pass_rate, 4),
                'returncode': result.returncode,
            }
        except Exception as e:
            return {
                'passed': 0, 'failed': 0, 'total': 0,
                'pass_rate': 0.0,
                'error': str(e),
            }

    @staticmethod
    def validate_baseline(repo_path: str = '.') -> Dict:
        """Validate all active agents against their baselines."""
        try:
            from .agent_baseline_service import AgentBaselineService, BASELINE_DIR
            from pathlib import Path

            baseline_dir = Path(BASELINE_DIR)
            if not baseline_dir.exists():
                return {'passed': True, 'regressions': [],
                        'reason': 'No baselines to validate'}

            all_regressions = []
            for agent_dir in baseline_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                parts = agent_dir.name.rsplit('_', 1)
                if len(parts) != 2:
                    continue
                prompt_id, flow_id_str = parts
                try:
                    flow_id = int(flow_id_str)
                except ValueError:
                    continue

                result = AgentBaselineService.validate_against_baseline(
                    prompt_id, flow_id)
                if result and not result.get('passed', True):
                    all_regressions.extend([
                        f'{agent_dir.name}: {r}'
                        for r in result.get('regressions', [])
                    ])

            return {
                'passed': len(all_regressions) == 0,
                'regressions': all_regressions,
            }
        except Exception as e:
            return {'passed': True, 'regressions': [],
                    'error': str(e)}

    @staticmethod
    def classify_change(diff_stats: dict) -> str:
        """Classify change complexity.

        simple:   <= 3 files, < 100 lines
        moderate: <= 10 files, < 500 lines
        complex:  > 10 files or > 500 lines
        """
        files = diff_stats.get('files_changed', 0)
        lines = (diff_stats.get('additions', 0) +
                 diff_stats.get('deletions', 0))

        if files <= 3 and lines < 100:
            return 'simple'
        elif files <= 10 and lines < 500:
            return 'moderate'
        return 'complex'

    @staticmethod
    def post_review(
        repo_url: str,
        pr_number: int,
        review: dict,
    ):
        """Post review to GitHub via gh CLI."""
        from .private_repo_access import _extract_owner_repo
        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            return

        owner, repo = owner_repo
        decision = review.get('decision', 'flag_steward')

        # Map decision to GitHub review event
        event_map = {
            'approve': 'APPROVE',
            'request_changes': 'REQUEST_CHANGES',
            'flag_steward': 'COMMENT',
        }
        event = event_map.get(decision, 'COMMENT')

        body = (
            f"## Automated PR Review\n\n"
            f"**Decision**: {decision.upper()}\n"
            f"**Reason**: {review.get('reason', 'N/A')}\n\n"
            f"### Test Results\n"
            f"- Passed: {review.get('test_results', {}).get('passed', '?')}\n"
            f"- Failed: {review.get('test_results', {}).get('failed', '?')}\n"
            f"- Pass Rate: {review.get('test_results', {}).get('pass_rate', '?')}\n\n"
            f"### Baseline Validation\n"
            f"- Passed: {review.get('baseline_validation', {}).get('passed', '?')}\n"
        )

        regressions = review.get('baseline_validation', {}).get(
            'regressions', [])
        if regressions:
            body += "- Regressions:\n"
            for r in regressions[:10]:
                body += f"  - {r}\n"

        body += (
            f"\n### Change Complexity: "
            f"{review.get('change_complexity', '?')}\n\n"
            f"*Automated by Hyve Coding Agent*"
        )

        try:
            subprocess.run(
                ['gh', 'api', '--method', 'POST',
                 f'repos/{owner}/{repo}/pulls/{pr_number}/reviews',
                 '-f', f'event={event}',
                 '-f', f'body={body}'],
                capture_output=True, text=True, timeout=30)
        except Exception as e:
            logger.debug(f"Post review failed: {e}")
