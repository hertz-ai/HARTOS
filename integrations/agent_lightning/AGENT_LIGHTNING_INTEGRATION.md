# Agent Lightning Integration

**Status:** IN PROGRESS
**Date:** 2025-11-03
**Purpose:** Integrate Microsoft Agent Lightning for continuous agent training and optimization

---

## Overview

Agent Lightning enables iterative improvement of AI agents through reinforcement learning, automatic prompt optimization, and supervised fine-tuning. This integration adds training capabilities to our existing agent system with minimal code changes.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Agent Lightning Integration                    │
│                                                                   │
│  ┌─────────────────┐        ┌──────────────────┐                │
│  │ AgentInstrument │        │ LightningTrainer │                │
│  │                 │        │                  │                │
│  │ - Auto-trace    │───────>│ - Store data     │                │
│  │ - Emit events   │        │ - Run algorithms │                │
│  │ - Track rewards │        │ - Update agents  │                │
│  └─────────────────┘        └──────────────────┘                │
│          │                           │                           │
│          ↓                           ↓                           │
│  ┌──────────────────────────────────────────┐                   │
│  │        LightningStore (Persistence)      │                   │
│  │  - Spans (traces)                        │                   │
│  │  - Resources (prompts, weights)          │                   │
│  │  - Tasks (training jobs)                 │                   │
│  └──────────────────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ↓
        ┌──────────────────────────────────────────┐
        │       Existing Agent System              │
        │                                          │
        │  - create_recipe.py (AutoGen)           │
        │  - reuse_recipe.py  (AutoGen)           │
        │  - Task Ledger                          │
        │  - A2A Communication                    │
        │  - AP2 Payments                         │
        └──────────────────────────────────────────┘
```

## Key Components

### 1. AgentLightningWrapper
Wraps existing AutoGen agents with Agent Lightning instrumentation:
- Intercepts agent messages and tool calls
- Emits spans to LightningStore
- Tracks rewards and outcomes
- Zero code change to agent logic

### 2. LightningTracer
Automatic tracing for agent interactions:
- Captures prompts sent to LLM
- Records tool executions
- Tracks task completion/failure
- Measures execution time and cost

### 3. RewardCalculator
Computes rewards for agent actions:
- Task completion rewards
- Quality metrics (accuracy, relevance)
- Efficiency metrics (time, cost)
- User feedback integration

### 4. TrainingOrchestrator
Manages continuous improvement loop:
- Collects training data from spans
- Runs optimization algorithms
- Updates agent prompts and policies
- Deploys improved agents

## Integration Points

### In create_recipe.py

```python
from integrations.agent_lightning import AgentLightningWrapper, enable_auto_tracing

# Wrap assistant agent with Lightning
if ENABLE_AGENT_LIGHTNING:
    enable_auto_tracing()  # Global auto-tracing
    assistant = AgentLightningWrapper(
        agent=assistant,
        agent_id='create_recipe_assistant',
        track_rewards=True
    )
```

### In reuse_recipe.py

```python
from integrations.agent_lightning import AgentLightningWrapper, enable_auto_tracing

# Wrap reuse agent with Lightning
if ENABLE_AGENT_LIGHTNING:
    enable_auto_tracing()  # Global auto-tracing
    assistant = AgentLightningWrapper(
        agent=assistant,
        agent_id=f'reuse_{recipe_id}_assistant',
        track_rewards=True
    )
```

## Data Flow

1. **Agent Execution**
   - Agent receives task
   - Wrapper intercepts execution
   - Span created with task context

2. **Event Emission**
   - Prompt sent → emit_prompt()
   - Tool called → emit_tool_call()
   - Task complete → emit_reward()
   - All events stored in LightningStore

3. **Training Cycle**
   - TrainingOrchestrator queries spans
   - Algorithm processes traces
   - Generates improved prompts/policies
   - Updates agent resources

4. **Agent Improvement**
   - New prompts loaded
   - Policy weights updated
   - Performance metrics tracked
   - A/B testing enabled

## Features

### Minimal Integration
- **Auto-tracing**: No code changes needed
- **Wrapper pattern**: Drop-in replacement for existing agents
- **Backwards compatible**: Works with or without Lightning

### Training Algorithms
- **Reinforcement Learning**: Learn from rewards
- **Prompt Optimization**: Auto-generate better prompts
- **Supervised Fine-tuning**: Learn from examples
- **Multi-agent coordination**: Optimize team performance

### Monitoring & Analytics
- **Performance tracking**: Success rates, latency, cost
- **A/B testing**: Compare agent versions
- **Drift detection**: Identify performance degradation
- **Explainability**: Understand agent decisions

## Configuration

```python
# config/agent_lightning_config.py

AGENT_LIGHTNING_CONFIG = {
    'enabled': True,  # Enable/disable Lightning
    'auto_trace': True,  # Automatic tracing
    'store_backend': 'redis',  # redis, json, or memory
    'training': {
        'algorithm': 'ppo',  # ppo, prompt_opt, sft
        'batch_size': 32,
        'learning_rate': 1e-4,
        'update_frequency': '1 hour'
    },
    'rewards': {
        'task_completion': 1.0,
        'task_failure': -0.5,
        'tool_use_efficiency': 0.1,
        'user_feedback': 0.5
    },
    'agents': {
        'create_recipe_assistant': {
            'optimize_prompts': True,
            'optimize_tools': False
        },
        'reuse_recipe_assistant': {
            'optimize_prompts': True,
            'optimize_tools': True
        }
    }
}
```

## Implementation Files

```
integrations/agent_lightning/
├── __init__.py                    # Module exports
├── wrapper.py                     # AgentLightningWrapper
├── tracer.py                      # Automatic tracing
├── rewards.py                     # Reward calculation
├── trainer.py                     # Training orchestration
├── store.py                       # LightningStore backend
├── config.py                      # Configuration
├── algorithms/                    # Training algorithms
│   ├── __init__.py
│   ├── ppo.py                     # Proximal Policy Optimization
│   ├── prompt_opt.py              # Prompt optimization
│   └── sft.py                     # Supervised fine-tuning
├── examples/                      # Integration examples
│   ├── basic_example.py
│   └── multi_agent_example.py
└── tests/                         # Test suite
    ├── test_wrapper.py
    ├── test_tracer.py
    └── test_trainer.py
```

## Benefits

### For Agents
- ✅ Continuous improvement from real usage
- ✅ Automatic prompt refinement
- ✅ Better tool selection
- ✅ Improved multi-agent coordination

### For System
- ✅ Performance monitoring
- ✅ Cost optimization
- ✅ Quality assurance
- ✅ Explainability and debugging

### For Users
- ✅ Better agent responses over time
- ✅ Faster task completion
- ✅ More reliable outcomes
- ✅ Personalized agent behavior

## Compatibility

- ✅ Works with AutoGen agents (our current system)
- ✅ Compatible with Task Ledger
- ✅ Integrates with A2A delegation
- ✅ Preserves AP2 payment workflows
- ✅ No changes to existing agent logic

## Roadmap

### Phase 1: Basic Integration (Current)
- [x] Research Agent Lightning
- [ ] Install agentlightning package
- [ ] Create wrapper module
- [ ] Add auto-tracing support
- [ ] Integrate with create_recipe.py

### Phase 2: Training Pipeline
- [ ] Set up LightningStore
- [ ] Implement reward calculation
- [ ] Configure training algorithm
- [ ] Add training orchestrator

### Phase 3: Advanced Features
- [ ] Multi-agent optimization
- [ ] A/B testing framework
- [ ] Performance analytics dashboard
- [ ] Custom reward functions

### Phase 4: Production Deployment
- [ ] Production monitoring
- [ ] Automated retraining
- [ ] Model versioning
- [ ] Rollback capabilities

## Next Steps

1. Install Agent Lightning: `pip install agentlightning`
2. Create wrapper module: `integrations/agent_lightning/wrapper.py`
3. Add configuration: `integrations/agent_lightning/config.py`
4. Integrate into create_recipe.py
5. Test with sample agents
6. Set up training pipeline

---

**References:**
- [Agent Lightning GitHub](https://github.com/microsoft/agent-lightning)
- [Agent Lightning Docs](https://microsoft.github.io/agent-lightning/)
- [arXiv Paper](https://arxiv.org/abs/2508.03680)
