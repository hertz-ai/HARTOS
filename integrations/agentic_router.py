"""
Agentic Intent Router — detects when a user prompt requires multi-step
execution and routes from LangChain to autogen.

Used by the LangChain Agentic_Router tool. When a prompt is classified as
agentic, this module:
1. Uses the LLM to semantically match against 96 expert agents + user recipes
2. Uses the LLM to generate a real execution plan (3-7 steps)
3. Returns structured plan data for the frontend Plan Mode UI

Intent classification itself is handled by the LLM deciding whether to call
the Agentic_Router tool — no keyword heuristics needed.
"""

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def find_matching_agent(prompt: str, prompts_dir: str = None) -> Optional[Dict]:
    """Use LLM to semantically match prompt against available agents + recipes.

    Sends agent summaries to the LLM and asks it to select the best match.
    Falls back to None if LLM fails or no match found.
    """
    agent_summaries = _build_agent_catalog(prompts_dir)
    if not agent_summaries:
        return None

    try:
        from hart_intelligence import get_llm
        llm = get_llm(temperature=0.1, max_tokens=300)

        catalog_text = "\n".join(
            f"- ID:{a['id']} | {a['name']} | {a['source']} | {a['description'][:120]}"
            for a in agent_summaries[:50]
        )

        result = llm.invoke(
            f"Given this user task, select the single best matching agent from the catalog below. "
            f"If no agent is a good semantic match, respond with just 'NONE'.\n"
            f"Otherwise respond with ONLY the agent ID.\n\n"
            f"User task: {prompt}\n\n"
            f"Agent catalog:\n{catalog_text}"
        )

        text = (result.content if hasattr(result, 'content') else str(result)).strip()

        if text.upper() == 'NONE' or not text:
            return None

        for a in agent_summaries:
            if a['id'] in text:
                return {
                    'agent_id': a['id'],
                    'name': a['name'],
                    'score': 15,
                    'source': a['source'],
                    'description': a['description'],
                }
        return None
    except Exception as e:
        logger.warning(f"LLM agent matching failed: {e}")
        return None


def _build_agent_catalog(prompts_dir: str = None) -> List[Dict]:
    """Build unified catalog of expert agents + user recipes for LLM matching."""
    catalog = []

    try:
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        for agent in registry.agents.values():
            catalog.append({
                'id': agent.agent_id,
                'name': agent.name,
                'description': agent.description,
                'source': 'expert',
            })
    except Exception:
        pass

    if prompts_dir and os.path.isdir(prompts_dir):
        try:
            for fname in os.listdir(prompts_dir):
                if not fname.endswith('.json') or '_recipe' in fname:
                    continue
                try:
                    with open(os.path.join(prompts_dir, fname)) as f:
                        recipe = json.load(f)
                    catalog.append({
                        'id': fname.replace('.json', ''),
                        'name': recipe.get('name', fname),
                        'description': recipe.get('goal', ''),
                        'source': 'recipe',
                    })
                except Exception:
                    continue
        except Exception:
            pass

    # Hive recipes — federated index from peer nodes
    try:
        from integrations.agent_engine.federated_aggregator import get_federated_aggregator
        agg = get_federated_aggregator()
        hive_index = agg.aggregate_recipes()
        if hive_index and isinstance(hive_index, dict):
            for rid, info in hive_index.items():
                if isinstance(info, dict) and info.get('name'):
                    catalog.append({
                        'id': rid,
                        'name': info['name'],
                        'description': info.get('description', ''),
                        'source': 'hive',
                    })
    except Exception:
        pass

    # Google A2A registered agents
    try:
        from integrations.google_a2a.dynamic_agent_registry import get_registry
        a2a_registry = get_registry()
        for agent in a2a_registry.list_agents():
            catalog.append({
                'id': agent.get('id', ''),
                'name': agent.get('name', ''),
                'description': agent.get('description', ''),
                'source': 'a2a',
            })
    except Exception:
        pass

    return catalog


def generate_plan_steps(prompt: str, matched_agent: Optional[Dict] = None) -> List[Dict]:
    """Generate plan steps using the LLM. Falls back to generic steps on failure."""
    try:
        from hart_intelligence import get_llm
        llm = get_llm(temperature=0.3, max_tokens=800)

        agent_context = ""
        if matched_agent:
            agent_context = (f"\nMatched expert: {matched_agent['name']} — "
                             f"{matched_agent.get('description', '')}")

        result = llm.invoke(
            f"Generate a 3-7 step execution plan for this task. "
            f"Return ONLY a JSON array: "
            f'[{{"step_num": 1, "description": "...", "tool_or_agent": "..."}}]'
            f"{agent_context}\n\nTask: {prompt}"
        )

        text = result.content if hasattr(result, 'content') else str(result)
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            steps = json.loads(match.group())
            if isinstance(steps, list) and len(steps) >= 2:
                return steps
    except Exception as e:
        logger.warning(f"LLM plan generation failed, using fallback: {e}")

    agent_name = matched_agent['name'] if matched_agent else 'execution'
    return [
        {'step_num': 1, 'description': 'Analyze requirements and gather context', 'tool_or_agent': 'analysis'},
        {'step_num': 2, 'description': 'Plan approach and identify resources', 'tool_or_agent': 'planning'},
        {'step_num': 3, 'description': 'Execute the task', 'tool_or_agent': agent_name},
        {'step_num': 4, 'description': 'Deliver results and get feedback', 'tool_or_agent': 'delivery'},
    ]


def should_auto_create_agent(prompt: str, prompts_dir: str = None) -> bool:
    """Return True only if NO existing agent can handle this task.

    This is the gate that prevents unnecessary agent creation.
    """
    match = find_matching_agent(prompt, prompts_dir)
    return match is None


def build_agentic_plan(prompt: str, prompts_dir: str = None) -> Dict:
    """Full pipeline: match → plan. Returns structured plan dict."""
    matched_agent = find_matching_agent(prompt, prompts_dir)
    plan_steps = generate_plan_steps(prompt, matched_agent)

    return {
        'task_description': prompt,
        'steps': plan_steps,
        'matched_agent_id': matched_agent['agent_id'] if matched_agent else None,
        'matched_agent_name': matched_agent['name'] if matched_agent else None,
        'matched_agent_source': matched_agent['source'] if matched_agent else None,
        'confidence': 'high' if matched_agent else 'medium',
        'requires_new_agent': matched_agent is None,
    }
