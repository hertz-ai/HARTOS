"""
Basic usage example for Agent Ledger.

This example demonstrates:
- Creating a ledger
- Adding tasks
- Tracking task execution
- Getting progress summary
"""

from agent_ledger import SmartLedger, Task, TaskType, TaskStatus


def main():
    # Create a ledger for tracking tasks
    ledger = SmartLedger(agent_id="example_agent", session_id="demo_001")

    print("=== Agent Ledger Basic Example ===\n")

    # Define some tasks
    tasks = [
        Task(
            task_id="fetch_data",
            description="Fetch data from external API",
            task_type=TaskType.PRE_ASSIGNED,
            priority=100
        ),
        Task(
            task_id="process_data",
            description="Process and transform data",
            task_type=TaskType.PRE_ASSIGNED,
            prerequisites=["fetch_data"],
            priority=90
        ),
        Task(
            task_id="generate_report",
            description="Generate final report",
            task_type=TaskType.PRE_ASSIGNED,
            prerequisites=["process_data"],
            priority=80
        )
    ]

    # Add all tasks to ledger
    print("Adding tasks...")
    for task in tasks:
        ledger.add_task(task)
        print(f"  Added: {task.description}")

    print(f"\nInitial progress: {ledger.get_progress_summary()['progress']}")

    # Execute tasks in order
    print("\n--- Executing Tasks ---\n")

    while True:
        # Get next ready task
        next_task = ledger.get_next_task()
        if not next_task:
            print("No more tasks to execute!")
            break

        print(f"Starting: {next_task.description}")
        ledger.update_task_status(next_task.task_id, TaskStatus.IN_PROGRESS)

        # Simulate work
        import time
        time.sleep(0.5)

        # Complete the task
        result = {"status": "success", "task_id": next_task.task_id}
        ledger.complete_task(next_task.task_id, result=result)
        print(f"Completed: {next_task.description}")
        print(f"Progress: {ledger.get_progress_summary()['progress']}\n")

    # Final summary
    print("\n=== Final Summary ===")
    summary = ledger.get_progress_summary()
    print(f"Total tasks: {summary['total']}")
    print(f"Completed: {summary['completed']}")
    print(f"Progress: {summary['progress']}")


if __name__ == "__main__":
    main()
