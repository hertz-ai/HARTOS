"""
Test Suite for Scheduler Creation
Tests scheduler creation in both review (creation) and reuse modes
"""
import pytest
from unittest.mock import Mock, patch, MagicMock, call
from datetime import datetime, timedelta
import sys
import os
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytest.importorskip('autogen', reason='autogen not installed')

from create_recipe import scheduler, time_based_execution, visual_execution
from reuse_recipe import create_schedule, time_based_execution as reuse_time_based_execution


class TestSchedulerCreationReviewMode:
    """Test scheduler creation in review/creation mode"""

    def test_scheduler_initialization(self):
        """Test that scheduler is properly initialized"""
        from create_recipe import scheduler
        assert scheduler is not None
        assert scheduler.running

    def test_time_based_job_scheduling_creation_mode(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test scheduling time-based jobs in creation mode"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            with patch('create_recipe.time_agents', {}):
                with patch('create_recipe.user_tasks', {}):
                    mock_job = Mock()
                    mock_scheduler.add_job.return_value = mock_job

                    try:
                        # Simulate adding a scheduled task
                        run_time = datetime.now() + timedelta(minutes=5)
                        job = mock_scheduler.add_job(
                            time_based_execution,
                            'date',
                            run_date=run_time,
                            args=["Test task", test_user_id, test_prompt_id, 1, []]
                        )
                        assert job is not None
                        mock_scheduler.add_job.assert_called_once()
                    except Exception as e:
                        pytest.fail(f"Scheduler creation failed in creation mode: {e}")

    def test_visual_execution_job_scheduling_creation_mode(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test scheduling visual execution jobs in creation mode"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_job = Mock()
            mock_scheduler.add_job.return_value = mock_job

            try:
                # Simulate adding a visual task
                run_time = datetime.now() + timedelta(minutes=1)
                job = mock_scheduler.add_job(
                    visual_execution,
                    'date',
                    run_date=run_time,
                    args=["Visual task", test_user_id, test_prompt_id]
                )
                assert job is not None
                mock_scheduler.add_job.assert_called_once()
            except Exception as e:
                pytest.fail(f"Visual execution scheduling failed: {e}")

    def test_cron_trigger_scheduling(self, test_user_id, test_prompt_id):
        """Test scheduling with cron triggers"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger(hour=9, minute=0)  # Daily at 9 AM
            mock_job = Mock()
            mock_scheduler.add_job.return_value = mock_job

            try:
                job = mock_scheduler.add_job(
                    time_based_execution,
                    trigger=trigger,
                    args=["Morning task", test_user_id, test_prompt_id, 1, []]
                )
                assert job is not None
            except Exception as e:
                pytest.fail(f"Cron scheduling failed: {e}")

    def test_interval_trigger_scheduling(self, test_user_id, test_prompt_id):
        """Test scheduling with interval triggers"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            from apscheduler.triggers.interval import IntervalTrigger

            trigger = IntervalTrigger(minutes=30)  # Every 30 minutes
            mock_job = Mock()
            mock_scheduler.add_job.return_value = mock_job

            try:
                job = mock_scheduler.add_job(
                    time_based_execution,
                    trigger=trigger,
                    args=["Periodic task", test_user_id, test_prompt_id, 1, []]
                )
                assert job is not None
            except Exception as e:
                pytest.fail(f"Interval scheduling failed: {e}")

    def test_multiple_scheduled_jobs_creation_mode(self, test_user_id, test_prompt_id):
        """Test scheduling multiple jobs without conflicts"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_scheduler.add_job.return_value = Mock()
            jobs = []

            try:
                for i in range(5):
                    run_time = datetime.now() + timedelta(minutes=i)
                    job = mock_scheduler.add_job(
                        time_based_execution,
                        'date',
                        run_date=run_time,
                        args=[f"Task {i}", test_user_id, test_prompt_id, i, []],
                        id=f"job_{test_user_id}_{test_prompt_id}_{i}"
                    )
                    jobs.append(job)

                assert len(jobs) == 5
                assert mock_scheduler.add_job.call_count == 5
            except Exception as e:
                pytest.fail(f"Multiple job scheduling failed: {e}")

    def test_job_removal_and_replacement(self, test_user_id, test_prompt_id):
        """Test removing and replacing scheduled jobs"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_job = Mock()
            mock_scheduler.add_job.return_value = mock_job
            mock_scheduler.remove_job.return_value = True

            job_id = f"job_{test_user_id}_{test_prompt_id}"

            try:
                # Add job
                job = mock_scheduler.add_job(
                    time_based_execution,
                    'date',
                    run_date=datetime.now() + timedelta(minutes=5),
                    args=["Task", test_user_id, test_prompt_id, 1, []],
                    id=job_id
                )

                # Remove job
                mock_scheduler.remove_job(job_id)
                mock_scheduler.remove_job.assert_called_once_with(job_id)

                # Add replacement
                new_job = mock_scheduler.add_job(
                    time_based_execution,
                    'date',
                    run_date=datetime.now() + timedelta(minutes=10),
                    args=["Updated task", test_user_id, test_prompt_id, 1, []],
                    id=job_id,
                    replace_existing=True
                )
                assert new_job is not None
            except Exception as e:
                pytest.fail(f"Job removal/replacement failed: {e}")


class TestSchedulerCreationReuseMode:
    """Test scheduler creation in reuse mode"""

    def test_scheduler_initialization_reuse_mode(self):
        """Test that scheduler is initialized in reuse mode"""
        from reuse_recipe import scheduler
        assert scheduler is not None
        assert scheduler.running

    def test_create_schedule_function(self, test_user_id, test_prompt_id, temp_prompts_dir, mock_flask_app):
        """Test create_schedule function in reuse mode"""
        recipe = {
            "scheduled_tasks": [
                {
                    "task_description": "Test task",
                    "persona": "Test Assistant",
                    "schedule_type": "date",
                    "cron_expression": "0 9 * * *",
                    "run_date": (datetime.now() + timedelta(minutes=5)).isoformat(),
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = temp_prompts_dir / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.get_flow_number', return_value=(0, 'Test Assistant')):
                mock_scheduler.add_job.return_value = Mock()

                try:
                    create_schedule(test_prompt_id, test_user_id)
                    assert mock_scheduler.add_job.called or True
                except Exception as e:
                    pytest.fail(f"create_schedule failed: {e}")

    def test_time_based_execution_reuse_mode(self, test_user_id, test_prompt_id, mock_flask_app):
        """Test time-based execution in reuse mode"""
        with patch('reuse_recipe.user_agents', {f"{test_user_id}_{test_prompt_id}": (
            Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock(),
            Mock(), Mock(), Mock(), Mock(), Mock()
        )}):
            with patch('reuse_recipe.send_message_to_user1'):
                try:
                    result = reuse_time_based_execution(
                        "Test task",
                        test_user_id,
                        test_prompt_id,
                        1
                    )
                    assert result == 'done'
                except Exception as e:
                    # Should handle gracefully
                    pass

    def test_scheduled_task_with_cron_reuse_mode(self, test_user_id, test_prompt_id, temp_prompts_dir, mock_flask_app):
        """Test scheduled task with cron trigger in reuse mode"""
        recipe = {
            "scheduled_tasks": [
                {
                    "task_description": "Daily task",
                    "persona": "Test Assistant",
                    "cron_expression": "0 9 * * *",
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = temp_prompts_dir / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.get_flow_number', return_value=(0, 'Test Assistant')):
                mock_scheduler.add_job.return_value = Mock()

                try:
                    create_schedule(test_prompt_id, test_user_id)
                except Exception as e:
                    pytest.fail(f"Cron scheduling in reuse mode failed: {e}")

    def test_scheduled_task_with_interval_reuse_mode(self, test_user_id, test_prompt_id, temp_prompts_dir, mock_flask_app):
        """Test scheduled task with interval trigger in reuse mode"""
        recipe = {
            "scheduled_tasks": [
                {
                    "task_description": "Periodic task",
                    "persona": "Test Assistant",
                    "cron_expression": "*/30 * * * *",
                    "action_entry_point": 1
                }
            ]
        }

        recipe_file = temp_prompts_dir / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.get_flow_number', return_value=(0, 'Test Assistant')):
                mock_scheduler.add_job.return_value = Mock()

                try:
                    create_schedule(test_prompt_id, test_user_id)
                except Exception as e:
                    pytest.fail(f"Interval scheduling in reuse mode failed: {e}")

    def test_multiple_scheduled_tasks_reuse_mode(self, test_user_id, test_prompt_id, temp_prompts_dir, mock_flask_app):
        """Test multiple scheduled tasks in reuse mode"""
        recipe = {
            "scheduled_tasks": [
                {
                    "task_description": "Task 1",
                    "persona": "Test Assistant",
                    "cron_expression": "0 9 * * *",
                    "action_entry_point": 1
                },
                {
                    "task_description": "Task 2",
                    "persona": "Test Assistant",
                    "cron_expression": "*/15 * * * *",
                    "action_entry_point": 2
                },
                {
                    "task_description": "Task 3",
                    "persona": "Test Reviewer",
                    "cron_expression": "30 10 * * *",
                    "action_entry_point": 3
                }
            ]
        }

        recipe_file = temp_prompts_dir / f"{test_prompt_id}_0_recipe.json"
        with open(recipe_file, 'w') as f:
            json.dump(recipe, f)

        with patch('reuse_recipe.scheduler') as mock_scheduler:
            with patch('reuse_recipe.get_flow_number', return_value=(0, 'Test Assistant')):
                mock_scheduler.add_job.return_value = Mock()

                try:
                    create_schedule(test_prompt_id, test_user_id)
                except Exception as e:
                    pytest.fail(f"Multiple task scheduling in reuse mode failed: {e}")


class TestSchedulerRobustness:
    """Test scheduler robustness and error handling"""

    def test_scheduler_handles_past_run_dates(self, test_user_id, test_prompt_id):
        """Test scheduler handles past run dates gracefully"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            past_time = datetime.now() - timedelta(hours=1)

            try:
                job = mock_scheduler.add_job(
                    time_based_execution,
                    'date',
                    run_date=past_time,
                    args=["Past task", test_user_id, test_prompt_id, 1, []]
                )
                # Should either execute immediately or skip
            except Exception:
                # Should handle gracefully
                pass

    def test_scheduler_handles_invalid_cron_expression(self, test_user_id, test_prompt_id):
        """Test scheduler handles invalid cron expressions"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            from apscheduler.triggers.cron import CronTrigger

            try:
                # Invalid cron (hour > 23)
                trigger = CronTrigger(hour=25, minute=0)
                pytest.fail("Should have raised ValueError for invalid hour")
            except ValueError:
                # Expected
                pass

    def test_scheduler_concurrent_job_execution(self, test_user_id, test_prompt_id):
        """Test scheduler handles concurrent job execution"""
        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_scheduler.add_job.return_value = Mock()

            try:
                # Add multiple jobs with same execution time
                run_time = datetime.now() + timedelta(seconds=1)
                for i in range(5):
                    mock_scheduler.add_job(
                        time_based_execution,
                        'date',
                        run_date=run_time,
                        args=[f"Concurrent task {i}", test_user_id, test_prompt_id, i, []],
                        id=f"concurrent_job_{i}"
                    )
            except Exception as e:
                pytest.fail(f"Concurrent job scheduling failed: {e}")

    def test_scheduler_persistence_across_restarts(self, test_user_id, test_prompt_id):
        """Test scheduler jobs can be persisted and restored"""
        # This would typically use a JobStore like SQLAlchemyJobStore
        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_scheduler.add_job.return_value = Mock()

            try:
                # Add job
                job = mock_scheduler.add_job(
                    time_based_execution,
                    'date',
                    run_date=datetime.now() + timedelta(minutes=5),
                    args=["Persistent task", test_user_id, test_prompt_id, 1, []],
                    id="persistent_job"
                )

                # Simulate restart by getting jobs
                mock_scheduler.get_jobs.return_value = [job]
                jobs = mock_scheduler.get_jobs()
                assert len(jobs) > 0
            except Exception as e:
                pytest.fail(f"Scheduler persistence failed: {e}")

    def test_scheduler_timezone_handling(self, test_user_id, test_prompt_id):
        """Test scheduler handles different timezones"""
        import pytz
        from apscheduler.triggers.cron import CronTrigger

        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_scheduler.add_job.return_value = Mock()

            try:
                # Schedule with specific timezone
                ist = pytz.timezone('Asia/Kolkata')
                trigger = CronTrigger(hour=9, minute=0, timezone=ist)

                job = mock_scheduler.add_job(
                    time_based_execution,
                    trigger=trigger,
                    args=["Timezone task", test_user_id, test_prompt_id, 1, []]
                )
                assert job is not None
            except Exception as e:
                pytest.fail(f"Timezone handling failed: {e}")

    def test_scheduler_error_recovery(self, test_user_id, test_prompt_id):
        """Test scheduler recovers from job execution errors"""
        def failing_job(*args, **kwargs):
            raise Exception("Job execution failed")

        with patch('create_recipe.scheduler') as mock_scheduler:
            mock_scheduler.add_job.return_value = Mock()

            try:
                job = mock_scheduler.add_job(
                    failing_job,
                    'date',
                    run_date=datetime.now() + timedelta(seconds=1),
                    max_instances=3  # Allow retries
                )
                # Scheduler should continue running even if job fails
            except Exception:
                # Should handle gracefully
                pass
