"""
Agentic Intent Router — detects when a user prompt requires multi-step
execution and routes from LangChain to autogen.

Used by the LangChain Agentic_Router tool. When a prompt is classified as
agentic, this module:
1. Searches ExpertAgentRegistry (96 agents) + user recipes for a match
2. Generates a plan (3-7 steps) for the task
3. Returns structured plan data for the frontend Plan Mode UI
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Keywords that strongly signal a multi-step / agentic task
_AGENTIC_KEYWORDS = [
    r'\bbuild\s+(?:me\s+)?(?:a|an|the)\b',
    r'\bcreate\s+(?:a|an|the)\b.*(?:app|website|system|tool|pipeline|agent)',
    r'\bwrite\s+(?:a|an|the|some)?\s*(?:code|script|program|function|class)',
    r'\bresearch\s+and\s+(?:compile|summarize|report)',
    r'\banalyze\s+(?:and|then)\b',
    r'\bstep[- ]by[- ]step\b',
    r'\bplan\s+(?:for|to|out)\b',
    r'\bdesign\s+(?:a|an|the)\b',
    r'\bimplement\b',
    r'\bdevelop\s+(?:a|an|the)\b',
    r'\bgenerate\s+(?:a|an|the)\b.*(?:report|analysis|strategy|campaign)',
    r'\bset\s*up\b.*(?:project|environment|pipeline|workflow)',
    r'\brefactor\b',
    r'\bdeploy\b',
    r'\bintegrate\b.*\bwith\b',
    r'\bmulti[- ]?step\b',
    r'\bcomplex\s+task\b',
]

# Simple Q&A patterns that should NOT be routed to autogen
_SIMPLE_PATTERNS = [
    r'^(?:what|who|when|where|why|how)\s+(?:is|are|was|were|do|does|did|can|could|would|should)\b',
    r'^(?:tell\s+me|explain|describe|define)\b',
    r'^(?:hi|hello|hey|thanks|thank\s+you|bye|goodbye)\b',
    r'^(?:yes|no|ok|okay|sure|nope)\b',
]

# Minimum score threshold for agent matching
AGENT_MATCH_THRESHOLD = 8


def classify_intent(prompt: str) -> bool:
    """Return True if the prompt requires agentic (multi-step) execution.

    Uses keyword heuristics. LLM-based classification happens via the
    Agentic_Router tool itself (LLM decides whether to call the tool).
    """
    if not prompt or len(prompt.strip()) < 15:
        return False

    prompt_lower = prompt.lower().strip()

    # Check simple patterns first — fast exit
    for pattern in _SIMPLE_PATTERNS:
        if re.match(pattern, prompt_lower):
            return False

    # Check agentic keywords
    for pattern in _AGENTIC_KEYWORDS:
        if re.search(pattern, prompt_lower):
            return True

    return False


def find_matching_agent(prompt: str, prompts_dir: str = None) -> Optional[Dict]:
    """Search 96 expert agents + user recipes for the best match.

    Returns: {agent_id, name, score, source: 'expert'|'recipe'} or None.
    """
    best_match = None
    best_score = 0

    # 1) Search ExpertAgentRegistry
    try:
        from integrations.expert_agents.registry import ExpertAgentRegistry
        registry = ExpertAgentRegistry()
        scored = registry.score_match(prompt)
        if scored:
            top_agent, top_score = scored[0]
            if top_score >= AGENT_MATCH_THRESHOLD:
                best_match = {
                    'agent_id': top_agent.agent_id,
                    'name': top_agent.name,
                    'score': top_score,
                    'source': 'expert',
                    'description': top_agent.description,
                }
                best_score = top_score
    except Exception as e:
        logger.debug(f"ExpertAgentRegistry search failed: {e}")

    # 2) Scan user-created recipes in prompts/ directory
    if prompts_dir and os.path.isdir(prompts_dir):
        try:
            prompt_lower = prompt.lower()
            prompt_words = [w for w in prompt_lower.split() if len(w) > 3]

            for fname in os.listdir(prompts_dir):
                if not fname.endswith('.json') or '_recipe' in fname:
                    continue
                fpath = os.path.join(prompts_dir, fname)
                try:
                    with open(fpath, 'r') as f:
                        recipe = json.load(f)
                    goal = (recipe.get('goal', '') or '').lower()
                    name = (recipe.get('name', '') or '').lower()
                    score = 0
                    for word in prompt_words:
                        if word in goal:
                            score += 3
                        if word in name:
                            score += 2
                    if score > best_score and score >= AGENT_MATCH_THRESHOLD:
                        prompt_id = fname.replace('.json', '')
                        best_match = {
                            'agent_id': prompt_id,
                            'name': recipe.get('name', prompt_id),
                            'score': score,
                            'source': 'recipe',
                            'description': recipe.get('goal', ''),
                        }
                        best_score = score
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Recipe scan failed: {e}")

    return best_match


def generate_plan_steps(prompt: str, matched_agent: Optional[Dict] = None) -> List[Dict]:
    """Generate plan steps for an agentic task.

    Returns a list of step dicts: [{step_num, description, tool_or_agent}].
    Uses heuristic decomposition. The LLM refines this in its response.
    """
    steps = []
    prompt_lower = prompt.lower()

    # Step 1: Always start with understanding/analysis
    steps.append({
        'step_num': 1,
        'description': 'Analyze requirements and gather context',
        'tool_or_agent': 'requirement_analysis',
    })

    # Domain-specific intermediate steps
    if any(kw in prompt_lower for kw in ['code', 'build', 'implement', 'develop', 'script', 'program']):
        steps.append({'step_num': 2, 'description': 'Design architecture and components', 'tool_or_agent': 'design'})
        steps.append({'step_num': 3, 'description': 'Implement core functionality', 'tool_or_agent': 'coding'})
        steps.append({'step_num': 4, 'description': 'Test and validate output', 'tool_or_agent': 'testing'})
    elif any(kw in prompt_lower for kw in ['research', 'analyze', 'report', 'compile']):
        steps.append({'step_num': 2, 'description': 'Research and gather information', 'tool_or_agent': 'research'})
        steps.append({'step_num': 3, 'description': 'Synthesize findings', 'tool_or_agent': 'synthesis'})
        steps.append({'step_num': 4, 'description': 'Compile final report', 'tool_or_agent': 'reporting'})
    elif any(kw in prompt_lower for kw in ['marketing', 'campaign', 'strategy', 'content']):
        steps.append({'step_num': 2, 'description': 'Define target audience and goals', 'tool_or_agent': 'strategy'})
        steps.append({'step_num': 3, 'description': 'Create content and materials', 'tool_or_agent': 'content_creation'})
        steps.append({'step_num': 4, 'description': 'Review and optimize', 'tool_or_agent': 'optimization'})
    else:
        steps.append({'step_num': 2, 'description': 'Plan execution approach', 'tool_or_agent': 'planning'})
        steps.append({'step_num': 3, 'description': 'Execute task', 'tool_or_agent': 'execution'})

    # If matched to an agent, note it
    if matched_agent:
        for step in steps:
            if step['tool_or_agent'] in ('coding', 'execution', 'content_creation', 'reporting', 'synthesis'):
                step['tool_or_agent'] = matched_agent.get('name', step['tool_or_agent'])

    # Final step: always deliver
    steps.append({
        'step_num': len(steps) + 1,
        'description': 'Deliver results and get feedback',
        'tool_or_agent': 'delivery',
    })

    return steps


def should_auto_create_agent(prompt: str, prompts_dir: str = None) -> bool:
    """Return True only if NO existing agent can handle this task.

    This is the gate that prevents unnecessary agent creation.
    """
    match = find_matching_agent(prompt, prompts_dir)
    return match is None


def build_agentic_plan(prompt: str, prompts_dir: str = None) -> Dict:
    """Full pipeline: classify → match → plan. Returns structured plan dict."""
    matched_agent = find_matching_agent(prompt, prompts_dir)
    plan_steps = generate_plan_steps(prompt, matched_agent)

    return {
        'task_description': prompt,
        'steps': plan_steps,
        'matched_agent_id': matched_agent['agent_id'] if matched_agent else None,
        'matched_agent_name': matched_agent['name'] if matched_agent else None,
        'matched_agent_source': matched_agent['source'] if matched_agent else None,
        'confidence': 'high' if matched_agent and matched_agent['score'] >= 12 else 'medium',
        'requires_new_agent': matched_agent is None,
    }
