"""
Cron Manager for HevolveBot Integration.

Provides enhanced scheduling capabilities with cron expressions.
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union
import threading


class JobStatus(Enum):
    """Status of a scheduled job."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IntervalUnit(Enum):
    """Units for interval scheduling."""
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"


@dataclass
class CronJob:
    """A scheduled cron job."""
    id: str
    name: str
    callback: Callable
    schedule_type: str  # 'at', 'every', 'cron'
    schedule_spec: Union[str, datetime, Dict[str, Any]]
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class CronExpression:
    """Parsed cron expression."""
    minute: str = "*"
    hour: str = "*"
    day_of_month: str = "*"
    month: str = "*"
    day_of_week: str = "*"

    @classmethod
    def parse(cls, expression: str) -> "CronExpression":
        """
        Parse a cron expression string.

        Args:
            expression: Cron expression (e.g., "0 9 * * 1-5")

        Returns:
            Parsed CronExpression

        Raises:
            ValueError: If expression is invalid
        """
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Invalid cron expression: expected 5 fields, got {len(parts)}"
            )

        return cls(
            minute=parts[0],
            hour=parts[1],
            day_of_month=parts[2],
            month=parts[3],
            day_of_week=parts[4]
        )

    def matches(self, dt: datetime) -> bool:
        """
        Check if a datetime matches this cron expression.

        Args:
            dt: The datetime to check

        Returns:
            True if the datetime matches
        """
        return (
            self._matches_field(self.minute, dt.minute, 0, 59) and
            self._matches_field(self.hour, dt.hour, 0, 23) and
            self._matches_field(self.day_of_month, dt.day, 1, 31) and
            self._matches_field(self.month, dt.month, 1, 12) and
            self._matches_field(self.day_of_week, dt.weekday(), 0, 6)
        )

    def _matches_field(
        self,
        field: str,
        value: int,
        min_val: int,
        max_val: int
    ) -> bool:
        """Check if a value matches a cron field."""
        if field == "*":
            return True

        for part in field.split(","):
            if "-" in part:
                # Range
                start, end = map(int, part.split("-"))
                if start <= value <= end:
                    return True
            elif "/" in part:
                # Step
                base, step = part.split("/")
                step = int(step)
                if base == "*":
                    if (value - min_val) % step == 0:
                        return True
                else:
                    base = int(base)
                    if value >= base and (value - base) % step == 0:
                        return True
            else:
                # Single value
                if int(part) == value:
                    return True

        return False

    def next_occurrence(self, after: datetime) -> datetime:
        """
        Find the next occurrence after a given datetime.

        Args:
            after: The datetime to search after

        Returns:
            The next matching datetime
        """
        # Start from the next minute
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Search up to a year ahead
        max_iterations = 525600  # minutes in a year
        for _ in range(max_iterations):
            if self.matches(dt):
                return dt
            dt += timedelta(minutes=1)

        raise ValueError("No matching time found within a year")


class CronManager:
    """
    Manages scheduled jobs with cron-like scheduling.

    Features:
    - Schedule jobs at specific times
    - Schedule recurring jobs with intervals
    - Schedule jobs with cron expressions
    - Pause and resume jobs
    - Track job execution history
    """

    def __init__(self):
        """Initialize the CronManager."""
        self._jobs: Dict[str, CronJob] = {}
        self._lock = threading.Lock()
        self._running = False
        self._execution_history: List[Dict[str, Any]] = []

    def schedule_at(
        self,
        callback: Callable,
        run_at: datetime,
        name: Optional[str] = None,
        job_id: Optional[str] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> CronJob:
        """
        Schedule a job to run at a specific time.

        Args:
            callback: The function to execute
            run_at: When to run the job
            name: Optional job name
            job_id: Optional custom job ID
            args: Positional arguments for the callback
            kwargs: Keyword arguments for the callback
            metadata: Optional metadata

        Returns:
            The created CronJob

        Raises:
            ValueError: If run_at is in the past
        """
        if run_at < datetime.now():
            raise ValueError("Cannot schedule job in the past")

        job_id = job_id or f"job_{secrets.token_hex(6)}"
        name = name or f"Job at {run_at.isoformat()}"

        if job_id in self._jobs:
            raise ValueError(f"Job with ID '{job_id}' already exists")

        job = CronJob(
            id=job_id,
            name=name,
            callback=callback,
            schedule_type="at",
            schedule_spec=run_at,
            next_run=run_at,
            max_runs=1,
            args=args,
            kwargs=kwargs or {},
            metadata=metadata or {}
        )

        with self._lock:
            self._jobs[job_id] = job

        return job

    def schedule_every(
        self,
        callback: Callable,
        interval: int,
        unit: IntervalUnit = IntervalUnit.MINUTES,
        name: Optional[str] = None,
        job_id: Optional[str] = None,
        start_at: Optional[datetime] = None,
        max_runs: Optional[int] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> CronJob:
        """
        Schedule a recurring job with a fixed interval.

        Args:
            callback: The function to execute
            interval: The interval value
            unit: The interval unit (seconds, minutes, hours, days, weeks)
            name: Optional job name
            job_id: Optional custom job ID
            start_at: When to start (defaults to now)
            max_runs: Maximum number of executions (None for unlimited)
            args: Positional arguments for the callback
            kwargs: Keyword arguments for the callback
            metadata: Optional metadata

        Returns:
            The created CronJob
        """
        job_id = job_id or f"job_{secrets.token_hex(6)}"
        name = name or f"Every {interval} {unit.value}"

        if job_id in self._jobs:
            raise ValueError(f"Job with ID '{job_id}' already exists")

        start_at = start_at or datetime.now()

        # Calculate next run based on interval
        delta = self._get_timedelta(interval, unit)
        next_run = start_at + delta if start_at <= datetime.now() else start_at

        job = CronJob(
            id=job_id,
            name=name,
            callback=callback,
            schedule_type="every",
            schedule_spec={"interval": interval, "unit": unit.value},
            next_run=next_run,
            max_runs=max_runs,
            args=args,
            kwargs=kwargs or {},
            metadata=metadata or {}
        )

        with self._lock:
            self._jobs[job_id] = job

        return job

    def schedule_cron(
        self,
        callback: Callable,
        expression: str,
        name: Optional[str] = None,
        job_id: Optional[str] = None,
        max_runs: Optional[int] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> CronJob:
        """
        Schedule a job with a cron expression.

        Args:
            callback: The function to execute
            expression: Cron expression (minute hour day month weekday)
            name: Optional job name
            job_id: Optional custom job ID
            max_runs: Maximum number of executions (None for unlimited)
            args: Positional arguments for the callback
            kwargs: Keyword arguments for the callback
            metadata: Optional metadata

        Returns:
            The created CronJob

        Raises:
            ValueError: If cron expression is invalid
        """
        # Validate the expression
        cron = CronExpression.parse(expression)

        job_id = job_id or f"job_{secrets.token_hex(6)}"
        name = name or f"Cron: {expression}"

        if job_id in self._jobs:
            raise ValueError(f"Job with ID '{job_id}' already exists")

        # Calculate next run
        next_run = cron.next_occurrence(datetime.now())

        job = CronJob(
            id=job_id,
            name=name,
            callback=callback,
            schedule_type="cron",
            schedule_spec=expression,
            next_run=next_run,
            max_runs=max_runs,
            args=args,
            kwargs=kwargs or {},
            metadata=metadata or {}
        )

        with self._lock:
            self._jobs[job_id] = job

        return job

    def pause(self, job_id: str) -> bool:
        """
        Pause a scheduled job.

        Args:
            job_id: The job ID to pause

        Returns:
            True if paused, False if not found
        """
        with self._lock:
            if job_id in self._jobs:
                job = self._jobs[job_id]
                if job.status not in (JobStatus.COMPLETED, JobStatus.CANCELLED):
                    job.status = JobStatus.PAUSED
                    return True
        return False

    def resume(self, job_id: str) -> bool:
        """
        Resume a paused job.

        Args:
            job_id: The job ID to resume

        Returns:
            True if resumed, False if not found or not paused
        """
        with self._lock:
            if job_id in self._jobs:
                job = self._jobs[job_id]
                if job.status == JobStatus.PAUSED:
                    job.status = JobStatus.PENDING
                    # Recalculate next run if needed
                    if job.next_run and job.next_run < datetime.now():
                        self._update_next_run(job)
                    return True
        return False

    def cancel(self, job_id: str) -> bool:
        """
        Cancel a scheduled job.

        Args:
            job_id: The job ID to cancel

        Returns:
            True if cancelled, False if not found
        """
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].status = JobStatus.CANCELLED
                return True
        return False

    def remove(self, job_id: str) -> bool:
        """
        Remove a job from the scheduler.

        Args:
            job_id: The job ID to remove

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                return True
        return False

    def list_jobs(
        self,
        status: Optional[JobStatus] = None,
        schedule_type: Optional[str] = None
    ) -> List[CronJob]:
        """
        List all scheduled jobs.

        Args:
            status: Optional filter by status
            schedule_type: Optional filter by schedule type ('at', 'every', 'cron')

        Returns:
            List of matching jobs
        """
        with self._lock:
            jobs = list(self._jobs.values())

        if status:
            jobs = [j for j in jobs if j.status == status]

        if schedule_type:
            jobs = [j for j in jobs if j.schedule_type == schedule_type]

        return jobs

    def get_job(self, job_id: str) -> Optional[CronJob]:
        """
        Get a specific job by ID.

        Args:
            job_id: The job ID

        Returns:
            The job or None if not found
        """
        return self._jobs.get(job_id)

    def run_due_jobs(self) -> List[Dict[str, Any]]:
        """
        Execute all jobs that are due.

        Returns:
            List of execution results
        """
        results = []
        now = datetime.now()

        with self._lock:
            due_jobs = [
                j for j in self._jobs.values()
                if j.status == JobStatus.PENDING and j.next_run and j.next_run <= now
            ]

        for job in due_jobs:
            result = self._execute_job(job)
            results.append(result)

        return results

    def run_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Manually execute a job immediately.

        Args:
            job_id: The job ID to execute

        Returns:
            Execution result or None if job not found
        """
        job = self._jobs.get(job_id)
        if job:
            return self._execute_job(job)
        return None

    def _execute_job(self, job: CronJob) -> Dict[str, Any]:
        """Execute a job and update its state."""
        job.status = JobStatus.RUNNING
        job.last_run = datetime.now()

        result = {
            "job_id": job.id,
            "job_name": job.name,
            "started_at": job.last_run,
            "success": False,
            "result": None,
            "error": None
        }

        try:
            result["result"] = job.callback(*job.args, **job.kwargs)
            result["success"] = True
            job.run_count += 1
            job.error = None

            # Check if max runs reached
            if job.max_runs and job.run_count >= job.max_runs:
                job.status = JobStatus.COMPLETED
            else:
                job.status = JobStatus.PENDING
                self._update_next_run(job)

        except Exception as e:
            result["error"] = str(e)
            job.error = str(e)
            job.status = JobStatus.FAILED

        result["completed_at"] = datetime.now()
        self._execution_history.append(result)

        return result

    def _update_next_run(self, job: CronJob) -> None:
        """Update the next run time for a job."""
        now = datetime.now()

        if job.schedule_type == "at":
            # One-time job, no next run
            job.next_run = None

        elif job.schedule_type == "every":
            spec = job.schedule_spec
            interval = spec["interval"]
            unit = IntervalUnit(spec["unit"])
            delta = self._get_timedelta(interval, unit)
            job.next_run = now + delta

        elif job.schedule_type == "cron":
            cron = CronExpression.parse(job.schedule_spec)
            job.next_run = cron.next_occurrence(now)

    def _get_timedelta(self, interval: int, unit: IntervalUnit) -> timedelta:
        """Convert interval and unit to timedelta."""
        if unit == IntervalUnit.SECONDS:
            return timedelta(seconds=interval)
        elif unit == IntervalUnit.MINUTES:
            return timedelta(minutes=interval)
        elif unit == IntervalUnit.HOURS:
            return timedelta(hours=interval)
        elif unit == IntervalUnit.DAYS:
            return timedelta(days=interval)
        elif unit == IntervalUnit.WEEKS:
            return timedelta(weeks=interval)
        else:
            raise ValueError(f"Unknown interval unit: {unit}")

    def get_execution_history(
        self,
        job_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get job execution history.

        Args:
            job_id: Optional filter by job ID
            limit: Maximum number of records

        Returns:
            List of execution records
        """
        history = self._execution_history.copy()

        if job_id:
            history = [h for h in history if h["job_id"] == job_id]

        return history[-limit:]

    def clear_history(self) -> None:
        """Clear execution history."""
        self._execution_history.clear()
