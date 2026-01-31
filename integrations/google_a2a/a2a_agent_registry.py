"""
Google A2A Agent Registration and Executor Functions

This module registers our agents (Assistant, Helper, Executor, Verify) with the
Google A2A Protocol server and provides executor functions for A2A message handling.
"""

import logging
from typing import Dict, Any
from .google_a2a_integration import get_a2a_server

logger = logging.getLogger(__name__)


# ============================================================================
# Agent Executor Functions
# ============================================================================

async def assistant_executor(message: str, context_id: str) -> Dict[str, Any]:
    """
    Execute Assistant agent tasks for A2A protocol

    Args:
        message: User message text
        context_id: A2A context ID for conversation tracking

    Returns:
        A2A response in format: {"role": "model", "parts": [{"text": "..."}]}
    """
    try:
        # Import here to avoid circular dependencies
        from reuse_recipe import chat_agent

        logger.info(f"Assistant A2A executing: {message[:100]}... (context: {context_id})")

        # Execute using the chat_agent (Assistant) from reuse_recipe
        result = chat_agent(message)

        # Format as A2A response
        return {
            "role": "model",
            "parts": [{"text": str(result)}]
        }

    except Exception as e:
        logger.error(f"Assistant A2A executor error: {e}")
        return {
            "role": "model",
            "parts": [{"text": f"Error executing task: {str(e)}"}]
        }


async def helper_executor(message: str, context_id: str) -> Dict[str, Any]:
    """
    Execute Helper agent tasks for A2A protocol

    Args:
        message: User message text
        context_id: A2A context ID for conversation tracking

    Returns:
        A2A response in format: {"role": "model", "parts": [{"text": "..."}]}
    """
    try:
        # Import here to avoid circular dependencies
        from create_recipe import recipe

        logger.info(f"Helper A2A executing: {message[:100]}... (context: {context_id})")

        # Execute using the recipe (Helper) from create_recipe
        result = recipe(message, agent_name="helper")

        # Format as A2A response
        return {
            "role": "model",
            "parts": [{"text": str(result)}]
        }

    except Exception as e:
        logger.error(f"Helper A2A executor error: {e}")
        return {
            "role": "model",
            "parts": [{"text": f"Error executing task: {str(e)}"}]
        }


async def executor_executor(message: str, context_id: str) -> Dict[str, Any]:
    """
    Execute Executor agent tasks for A2A protocol

    Args:
        message: User message text
        context_id: A2A context ID for conversation tracking

    Returns:
        A2A response in format: {"role": "model", "parts": [{"text": "..."}]}
    """
    try:
        # Import here to avoid circular dependencies
        from create_recipe import recipe

        logger.info(f"Executor A2A executing: {message[:100]}... (context: {context_id})")

        # Execute using the recipe (Executor) from create_recipe
        result = recipe(message, agent_name="executor")

        # Format as A2A response
        return {
            "role": "model",
            "parts": [{"text": str(result)}]
        }

    except Exception as e:
        logger.error(f"Executor A2A executor error: {e}")
        return {
            "role": "model",
            "parts": [{"text": f"Error executing task: {str(e)}"}]
        }


async def verify_executor(message: str, context_id: str) -> Dict[str, Any]:
    """
    Execute Verify agent tasks for A2A protocol

    Args:
        message: User message text
        context_id: A2A context ID for conversation tracking

    Returns:
        A2A response in format: {"role": "model", "parts": [{"text": "..."}]}
    """
    try:
        # Import here to avoid circular dependencies
        from create_recipe import recipe

        logger.info(f"Verify A2A executing: {message[:100]}... (context: {context_id})")

        # Execute using the recipe (Verify) from create_recipe
        result = recipe(message, agent_name="verify")

        # Format as A2A response
        return {
            "role": "model",
            "parts": [{"text": str(result)}]
        }

    except Exception as e:
        logger.error(f"Verify A2A executor error: {e}")
        return {
            "role": "model",
            "parts": [{"text": f"Error executing task: {str(e)}"}]
        }


# ============================================================================
# Agent Skills Definitions (A2A Format)
# ============================================================================

ASSISTANT_SKILLS = [
    {
        "name": "task_coordination",
        "description": "Coordinate complex multi-step tasks across multiple agents",
        "examples": [
            "Plan and execute a research project",
            "Orchestrate data analysis workflow",
            "Manage multi-agent collaboration"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain"]
    },
    {
        "name": "decision_making",
        "description": "Make strategic decisions based on context and requirements",
        "examples": [
            "Choose the best approach for a task",
            "Prioritize competing requirements",
            "Select optimal tools and methods"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain"]
    },
    {
        "name": "context_management",
        "description": "Manage conversation context and maintain coherent dialogue",
        "examples": [
            "Track conversation history",
            "Maintain task context across turns",
            "Summarize complex discussions"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain"]
    }
]

HELPER_SKILLS = [
    {
        "name": "tool_execution",
        "description": "Execute various tools and functions for data manipulation",
        "examples": [
            "Run API calls to external services",
            "Execute file operations",
            "Use web search and retrieval tools"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain", "application/json"]
    },
    {
        "name": "data_processing",
        "description": "Process and transform data in various formats",
        "examples": [
            "Parse JSON and XML data",
            "Transform data structures",
            "Filter and aggregate data"
        ],
        "input_modes": ["text", "text/plain", "application/json"],
        "output_modes": ["text", "text/plain", "application/json"]
    },
    {
        "name": "external_api",
        "description": "Interact with external APIs and web services",
        "examples": [
            "Call REST APIs",
            "Fetch web resources",
            "Submit data to external services"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain", "application/json"]
    }
]

EXECUTOR_SKILLS = [
    {
        "name": "code_execution",
        "description": "Execute code safely in various languages",
        "examples": [
            "Run Python scripts",
            "Execute shell commands",
            "Evaluate mathematical expressions"
        ],
        "input_modes": ["text", "text/plain", "application/x-python-code"],
        "output_modes": ["text", "text/plain"]
    },
    {
        "name": "computation",
        "description": "Perform complex computations and calculations",
        "examples": [
            "Solve mathematical problems",
            "Run statistical analysis",
            "Perform numerical simulations"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain", "application/json"]
    },
    {
        "name": "data_analysis",
        "description": "Analyze data and generate insights",
        "examples": [
            "Generate statistical summaries",
            "Identify patterns and trends",
            "Create data visualizations"
        ],
        "input_modes": ["text", "text/plain", "application/json"],
        "output_modes": ["text", "text/plain", "application/json"]
    }
]

VERIFY_SKILLS = [
    {
        "name": "status_verification",
        "description": "Verify task completion status and results",
        "examples": [
            "Check if task completed successfully",
            "Verify output correctness",
            "Monitor task progress"
        ],
        "input_modes": ["text", "text/plain"],
        "output_modes": ["text", "text/plain"]
    },
    {
        "name": "quality_assurance",
        "description": "Ensure output quality meets requirements",
        "examples": [
            "Validate output format",
            "Check for errors and inconsistencies",
            "Verify data integrity"
        ],
        "input_modes": ["text", "text/plain", "application/json"],
        "output_modes": ["text", "text/plain"]
    },
    {
        "name": "validation",
        "description": "Validate results and outputs against specifications",
        "examples": [
            "Verify API response format",
            "Check data against schema",
            "Validate calculations"
        ],
        "input_modes": ["text", "text/plain", "application/json"],
        "output_modes": ["text", "text/plain"]
    }
]


# ============================================================================
# Agent Registration Function
# ============================================================================

def register_all_agents():
    """
    Register all agents (Assistant, Helper, Executor, Verify) with the A2A server
    """
    try:
        a2a_server = get_a2a_server()

        if a2a_server is None:
            logger.warning("A2A server not initialized, skipping agent registration")
            return

        logger.info("Registering agents with Google A2A Protocol...")

        # Register Assistant Agent
        a2a_server.register_agent(
            agent_id="assistant",
            name="Assistant Agent",
            description="Coordinate tasks, make decisions, and manage conversation context",
            skills=ASSISTANT_SKILLS,
            executor_func=assistant_executor,
            capabilities={"streaming": False, "async": True}
        )
        logger.info("Registered Assistant agent with A2A")

        # Register Helper Agent
        a2a_server.register_agent(
            agent_id="helper",
            name="Helper Agent",
            description="Execute tools, process data, and interact with external APIs",
            skills=HELPER_SKILLS,
            executor_func=helper_executor,
            capabilities={"streaming": False, "async": True}
        )
        logger.info("Registered Helper agent with A2A")

        # Register Executor Agent
        a2a_server.register_agent(
            agent_id="executor",
            name="Executor Agent",
            description="Execute code, perform computations, and analyze data",
            skills=EXECUTOR_SKILLS,
            executor_func=executor_executor,
            capabilities={"streaming": False, "async": True}
        )
        logger.info("Registered Executor agent with A2A")

        # Register Verify Agent
        a2a_server.register_agent(
            agent_id="verify",
            name="Verify Agent",
            description="Verify task status, ensure quality, and validate results",
            skills=VERIFY_SKILLS,
            executor_func=verify_executor,
            capabilities={"streaming": False, "async": True}
        )
        logger.info("Registered Verify agent with A2A")

        logger.info("All agents registered with Google A2A Protocol successfully")

    except Exception as e:
        logger.error(f"Error registering agents with A2A: {e}")
        import traceback
        traceback.print_exc()
