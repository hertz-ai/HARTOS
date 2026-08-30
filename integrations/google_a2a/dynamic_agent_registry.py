"""
Dynamic Agent Registry for Google A2A Protocol

Automatically discovers and registers agents from prompt JSON files.
Each trained agent (recipe JSON) becomes an A2A-compatible specialist.

Architecture:
- Scans prompts/ directory for {prompt_id}_{flow_id}_recipe.json files
- Extracts agent capabilities from recipe JSONs
- Automatically creates A2A Agent Cards
- Registers with Google A2A Protocol server
- No hardcoded agents - fully dynamic!
"""

import os
import json
import logging
import glob
import threading
import time
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ── What we have already REPORTED, per prompts directory ────────────────────
# discover_all_agents used to log the whole roster on every call. Measured on the
# box 2026-08-29: 6,498 identical "Discovered agent" lines in two minutes, 55
# lines/second from this one module, because build_agent_directory() rescans on
# every GET /a2a/agents and something polls it ~19 times a minute. That was the
# machine's dominant IO load — journald wrote 6.3 GB, systemd 71.8 GB in 48h — on
# a USB stick root that subsequently started returning read errors.
#
# MODULE-level, not per-instance, and that is load-bearing: build_agent_directory
# constructs a FRESH DynamicAgentDiscovery for every request, so an instance
# attribute would be empty each time and would re-log the entire roster forever,
# i.e. it would look like a fix and change nothing.
_reported_lock = threading.Lock()
_reported_agents: Dict[str, Set[str]] = {}
_reported_at: Dict[str, float] = {}

#: Even when nothing changes, say so occasionally. Silence must never be
#: ambiguous between "no changes" and "the scanner died" — that ambiguity would
#: be a REAL loss of diagnostic information, which cutting the repetition is not.
UNCHANGED_HEARTBEAT_SECONDS = 600


@dataclass
class TrainedAgent:
    """
    Represents a trained agent from recipe JSON

    File naming pattern: {prompt_id}_{flow_id}_recipe.json
    Note: Each FLOW has a persona/role, not each role having multiple flows
    """
    agent_id: str  # e.g., "71_0" for prompt 71, flow 0
    prompt_id: int
    flow_id: int
    persona: str
    action: str
    recipe: List[Dict[str, Any]]
    status: str
    can_perform_without_user_input: str
    fallback_action: str
    metadata: Dict[str, Any]
    recipe_file: str
    flow_name: str = ""
    sub_goal: str = ""


class DynamicAgentDiscovery:
    """Discovers trained agents from prompts directory"""

    def __init__(self, prompts_dir: Optional[str] = None):
        if prompts_dir is None:
            # Deployment-aware default: the SAME resolver recipe SAVE
            # (helper.PROMPTS_DIR), REUSE read (cache_loaders), and the
            # daemon classifier (agent_daemon._flow_recipe_exists) share,
            # so the A2A surface advertises the recipes that actually
            # exist in every deployment mode (bundled/Docker/dev).
            try:
                from core.platform_paths import get_recipe_prompts_dir
                prompts_dir = get_recipe_prompts_dir()
            except Exception as e:
                logger.warning(
                    f"DynamicAgentDiscovery: canonical prompts-dir resolver "
                    f"unavailable ({e}); falling back to relative 'prompts'")
                prompts_dir = "prompts"
        self.prompts_dir = prompts_dir
        self.discovered_agents: Dict[str, TrainedAgent] = {}
        self.prompt_definitions: Dict[int, Dict[str, Any]] = {}

    def discover_all_agents(self) -> int:
        """
        Discover all trained agents from recipe JSON files

        Returns:
            Number of agents discovered
        """
        # Report the DELTA, not the roster. Every fact the old logging carried is
        # still emitted -- each agent is named with its persona the first time it
        # is seen, the count is stated whenever it changes -- and two facts it
        # never carried are added: agents that DISAPPEAR, and an explicit
        # unchanged heartbeat. In a wall of 171 identical lines every 3 seconds, a
        # recipe vanishing was invisible; now it is one line. See the note on
        # _reported_agents for the measurements that forced this.
        with _reported_lock:
            previous = _reported_agents.get(self.prompts_dir)
            last_said = _reported_at.get(self.prompts_dir, 0.0)
        first_scan = previous is None

        if first_scan:
            logger.info(f"Scanning {self.prompts_dir} for trained agents...")

        # First, load all main prompt definitions (e.g., 71.json, 8888.json)
        self._load_prompt_definitions()

        # Then discover all recipe JSONs (e.g., 71_0_recipe.json)
        recipe_pattern = os.path.join(self.prompts_dir, "*_*_recipe.json")
        recipe_files = glob.glob(recipe_pattern)

        for recipe_file in recipe_files:
            try:
                agent = self._load_agent_from_recipe(recipe_file)
                if agent:
                    self.discovered_agents[agent.agent_id] = agent
                    if first_scan or agent.agent_id not in previous:
                        logger.info(f"Discovered agent: {agent.agent_id} (persona: {agent.persona})")
            except Exception as e:
                # UNTOUCHED, deliberately: a recipe that fails to load is exactly
                # the kind of thing this log exists to tell us, and it is rare.
                logger.warning(f"Failed to load agent from {recipe_file}: {e}")

        current = set(self.discovered_agents)
        now = time.time()
        if first_scan:
            logger.info(f"Discovered {len(current)} trained agents")
            last_said = now
        else:
            gone = previous - current
            added = current - previous
            for agent_id in sorted(gone):
                logger.info(f"Agent no longer present: {agent_id}")
            if added or gone:
                logger.info(f"Discovered {len(current)} trained agents "
                            f"(+{len(added)} -{len(gone)})")
                last_said = now
            elif now - last_said >= UNCHANGED_HEARTBEAT_SECONDS:
                logger.info(f"Discovered {len(current)} trained agents (unchanged)")
                last_said = now

        with _reported_lock:
            _reported_agents[self.prompts_dir] = current
            _reported_at[self.prompts_dir] = last_said
        return len(self.discovered_agents)

    def _load_prompt_definitions(self):
        """Load main prompt definition files (e.g., 71.json, 8888.json)"""
        prompt_files = glob.glob(os.path.join(self.prompts_dir, "*.json"))

        for prompt_file in prompt_files:
            filename = os.path.basename(prompt_file)

            # Skip recipe files (they have underscores)
            if "_" in filename:
                continue

            try:
                prompt_id = int(filename.replace(".json", ""))

                with open(prompt_file, 'r', encoding='utf-8') as f:
                    prompt_def = json.load(f)

                self.prompt_definitions[prompt_id] = prompt_def
                logger.debug(f"Loaded prompt definition: {prompt_id}")

            except (ValueError, json.JSONDecodeError) as e:
                logger.debug(f"Skipping non-prompt file: {filename}")

    def _load_agent_from_recipe(self, recipe_file: str) -> Optional[TrainedAgent]:
        """
        Load a trained agent from recipe JSON file

        Pattern: {prompt_id}_{flow_id}_recipe.json
        Example: 71_0_recipe.json = prompt 71, flow 0
        """
        filename = os.path.basename(recipe_file)

        # Parse filename: {prompt_id}_{flow_id}_recipe.json
        parts = filename.replace("_recipe.json", "").split("_")
        if len(parts) != 2:
            logger.debug(f"Skipping {filename} - doesn't match pattern")
            return None

        try:
            prompt_id = int(parts[0])
            flow_id = int(parts[1])
        except ValueError:
            return None

        # Load recipe JSON
        with open(recipe_file, 'r', encoding='utf-8') as f:
            recipe_data = json.load(f)

        # Create agent ID
        agent_id = f"{prompt_id}_{flow_id}"

        # Get flow information from prompt definition
        prompt_def = self.prompt_definitions.get(prompt_id, {})
        flows = prompt_def.get("flows", [])

        flow_name = ""
        sub_goal = ""
        if flow_id < len(flows):
            flow_info = flows[flow_id]
            flow_name = flow_info.get("flow_name", "")
            sub_goal = flow_info.get("sub_goal", "")

        # Extract agent information
        agent = TrainedAgent(
            agent_id=agent_id,
            prompt_id=prompt_id,
            flow_id=flow_id,
            persona=recipe_data.get("persona", "unknown"),
            action=recipe_data.get("action", ""),
            recipe=recipe_data.get("recipe", []),
            status=recipe_data.get("status", "unknown"),
            can_perform_without_user_input=recipe_data.get("can_perform_without_user_input", "no"),
            fallback_action=recipe_data.get("fallback_action", ""),
            metadata=recipe_data.get("metadata", {}),
            recipe_file=recipe_file,
            flow_name=flow_name,
            sub_goal=sub_goal
        )

        return agent

    def get_agent_skills(self, agent: TrainedAgent) -> List[Dict[str, Any]]:
        """
        Extract skills from trained agent's recipe

        Returns A2A-compatible skills list
        """
        skills = []

        # Get prompt definition for context
        prompt_def = self.prompt_definitions.get(agent.prompt_id, {})
        prompt_name = prompt_def.get("name", f"Prompt {agent.prompt_id}")

        # Get persona description
        personas = prompt_def.get("personas", [])
        persona_desc = next(
            (p["description"] for p in personas if p["name"] == agent.persona),
            f"Specialist for {agent.persona}"
        )

        # Get flow information
        flows = prompt_def.get("flows", [])
        if agent.flow_id < len(flows):
            flow = flows[agent.flow_id]
            flow_name = flow.get("flow_name", f"Flow {agent.flow_id}")
            sub_goal = flow.get("sub_goal", "")
        else:
            flow_name = f"Flow {agent.flow_id}"
            sub_goal = ""

        # Create primary skill based on agent's trained action
        primary_skill = {
            "name": f"{agent.persona}_{flow_name.replace(' ', '_')}".lower(),
            "description": agent.action,
            "examples": [
                agent.action,
                sub_goal if sub_goal else agent.action
            ],
            "input_modes": ["text", "text/plain"],
            "output_modes": ["text", "text/plain", "application/json"],
            "metadata": {
                "prompt_id": agent.prompt_id,
                "flow_id": agent.flow_id,
                "flow_name": agent.flow_name,
                "persona": agent.persona,
                "autonomous": agent.can_perform_without_user_input == "yes",
                "has_fallback": bool(agent.fallback_action),
                "recipe_steps": len(agent.recipe)
            }
        }

        skills.append(primary_skill)

        # Add individual recipe steps as sub-skills
        for idx, step in enumerate(agent.recipe):
            step_skill = {
                "name": f"step_{idx+1}_{step.get('tool_name', 'action')}".lower().replace(' ', '_'),
                "description": step.get("steps", ""),
                "examples": [step.get("steps", "")],
                "input_modes": ["text", "text/plain"],
                "output_modes": ["text", "text/plain"],
                "metadata": {
                    "step_number": idx + 1,
                    "tool_name": step.get("tool_name", "None"),
                    "agent_performer": step.get("agent_to_perform_this_action", "")
                }
            }
            skills.append(step_skill)

        return skills

    def get_agent_description(self, agent: TrainedAgent) -> str:
        """Generate comprehensive agent description"""
        prompt_def = self.prompt_definitions.get(agent.prompt_id, {})
        prompt_name = prompt_def.get("name", f"Prompt {agent.prompt_id}")

        personas = prompt_def.get("personas", [])
        persona_desc = next(
            (p["description"] for p in personas if p["name"] == agent.persona),
            ""
        )

        description = f"Trained specialist for '{prompt_name}' - {persona_desc}. "
        description += f"Specialized in: {agent.action}. "
        description += f"Recipe contains {len(agent.recipe)} steps. "

        if agent.can_perform_without_user_input == "yes":
            description += "Can operate autonomously. "

        if agent.fallback_action:
            description += f"Has fallback strategy: {agent.fallback_action}"

        return description

    def get_all_agents(self) -> List[TrainedAgent]:
        """Get list of all discovered agents"""
        return list(self.discovered_agents.values())

    def get_agent_by_id(self, agent_id: str) -> Optional[TrainedAgent]:
        """Get specific agent by ID"""
        return self.discovered_agents.get(agent_id)


class DynamicAgentExecutor:
    """Executes tasks for dynamically discovered agents"""

    def __init__(self):
        self.discovery = DynamicAgentDiscovery()
        self.discovery.discover_all_agents()

    async def execute_agent_task(self, agent_id: str, message: str, context_id: str) -> Dict[str, Any]:
        """
        Execute a task for a dynamically discovered agent

        Args:
            agent_id: Agent identifier (e.g., "71_0_1")
            message: Task message
            context_id: A2A context ID

        Returns:
            A2A response format
        """
        agent = self.discovery.get_agent_by_id(agent_id)

        if not agent:
            return {
                "role": "model",
                "parts": [{
                    "text": f"Error: Agent {agent_id} not found. Agent may not be trained yet."
                }]
            }

        try:
            # Import execution functions
            from create_recipe import recipe
            from reuse_recipe import chat_agent

            logger.info(f"Executing task for agent {agent_id} (persona: {agent.persona})")

            # Determine execution mode based on agent status
            from core.constants import DEFAULT_USER_ID
            if agent.status == "done" or agent.status == "completed":
                # Use reuse mode (agent has trained recipe)
                result = chat_agent(
                    message,
                    user_id=agent.metadata.get("user_id", DEFAULT_USER_ID),
                    prompt_id=agent.prompt_id
                )
            else:
                # Use create mode (agent still learning)
                result = recipe(
                    user_id=agent.metadata.get("user_id", DEFAULT_USER_ID),
                    message=message,
                    prompt_id=agent.prompt_id
                )

            return {
                "role": "model",
                "parts": [{
                    "text": str(result),
                    "metadata": {
                        "agent_id": agent_id,
                        "persona": agent.persona,
                        "execution_mode": "reuse" if agent.status == "done" else "create"
                    }
                }]
            }

        except Exception as e:
            logger.error(f"Agent {agent_id} execution failed: {e}")
            return {
                "role": "model",
                "parts": [{
                    "text": f"Error executing agent {agent_id}: {str(e)}"
                }]
            }


# Global instances
_dynamic_discovery = None
_dynamic_executor = None


def get_dynamic_discovery() -> DynamicAgentDiscovery:
    """Get global dynamic discovery instance"""
    global _dynamic_discovery
    if _dynamic_discovery is None:
        _dynamic_discovery = DynamicAgentDiscovery()
    return _dynamic_discovery


def get_dynamic_executor() -> DynamicAgentExecutor:
    """Get global dynamic executor instance"""
    global _dynamic_executor
    if _dynamic_executor is None:
        _dynamic_executor = DynamicAgentExecutor()
    return _dynamic_executor
