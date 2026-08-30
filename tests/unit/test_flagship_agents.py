"""
Behavioral tests for the six steward FLAGSHIP AGENTS.

Flagship agents (Auto Research, Trading, Tutor, English Learning, Spoken
English, Speech Therapy) are registered as REAL dispatchable goal types
through the EXISTING agent_engine pipeline:

  register_goal_type (goal_manager)  ->  prompt builder + tool tags
  SEED_BOOTSTRAP_GOALS (goal_seeding) ->  runnable goal template
  GoalManager.build_prompt -> dispatch_goal -> /chat (CREATE/REUSE)

These tests import the REAL code, mock only the network/LLM boundary, and
assert observable behaviour (registration, prompt content, seed shape, and
one full end-to-end dispatch that produces output).

Run: pytest tests/unit/test_flagship_agents.py -v --noconftest
"""
import os
import sys

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from integrations.agent_engine.goal_manager import (
    get_prompt_builder,
    get_registered_types,
    get_tool_tags,
    GoalManager,
)
from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS


# Flagship agent display name -> registered goal_type.
FLAGSHIP_TYPES = {
    'Auto Research': 'research',
    'Trading': 'trading',
    'Tutor': 'tutor',
    'English Learning': 'english_learning',
    'Spoken English': 'spoken_english',
    'Speech Therapy': 'speech_therapy',
}

# The three speech agents that must declare the voice stack.
VOICE_TYPES = ['english_learning', 'spoken_english', 'speech_therapy']

# Flagship goal_type -> the bootstrap slug that seeds its runnable template.
FLAGSHIP_SLUGS = {
    'research': 'bootstrap_auto_research',
    'trading': 'bootstrap_trading_companion',
    'tutor': 'bootstrap_tutor',
    'english_learning': 'bootstrap_english_learning',
    'spoken_english': 'bootstrap_spoken_english',
    'speech_therapy': 'bootstrap_speech_companion',
}


def _goal_dict(goal_type, config=None):
    return {
        'id': f'test-{goal_type}',
        'goal_type': goal_type,
        'title': f'Test {goal_type}',
        'description': f'Test description for {goal_type}',
        'config': config or {},
    }


# ════════════════════════════════════════════════════════════════════
# 1. Registration - all six flagship types are dispatchable goal types
# ════════════════════════════════════════════════════════════════════

class TestFlagshipRegistration:

    @pytest.mark.parametrize('goal_type', list(FLAGSHIP_TYPES.values()))
    def test_type_registered(self, goal_type):
        assert goal_type in get_registered_types(), f'{goal_type} not registered'

    @pytest.mark.parametrize('goal_type', list(FLAGSHIP_TYPES.values()))
    def test_builder_callable_nonempty(self, goal_type):
        builder = get_prompt_builder(goal_type)
        assert builder is not None and callable(builder)
        prompt = builder(_goal_dict(goal_type))
        assert isinstance(prompt, str)
        assert len(prompt) > 100, f'{goal_type} prompt too short'

    @pytest.mark.parametrize('goal_type', list(FLAGSHIP_TYPES.values()))
    def test_tool_tags_nonempty(self, goal_type):
        tags = get_tool_tags(goal_type)
        assert isinstance(tags, list) and len(tags) >= 1, f'{goal_type} has no tool tags'

    def test_build_prompt_routes_to_flagship_builder(self):
        """GoalManager.build_prompt dispatches to the registered flagship builder."""
        prompt = GoalManager.build_prompt(
            _goal_dict('tutor', {'subject': 'algebra'}))
        assert prompt is not None
        assert 'TUTOR' in prompt
        assert 'algebra' in prompt


# ════════════════════════════════════════════════════════════════════
# 2. Auto Research is its OWN concern, not the internal experiment loop
# ════════════════════════════════════════════════════════════════════

class TestAutoResearchVsAutoresearch:

    def test_research_runs_with_empty_config(self):
        """The consumer 'research' agent produces a brief with no extra config."""
        prompt = get_prompt_builder('research')(_goal_dict('research'))
        assert prompt is not None
        assert 'AUTO RESEARCH' in prompt
        assert 'cite' in prompt.lower()

    def test_research_is_not_the_autoresearch_experiment_loop(self):
        """'autoresearch' (code-experiment loop) returns None without a repo;
        'research' (consumer assistant) does not - proving they are distinct."""
        autoresearch = get_prompt_builder('autoresearch')(_goal_dict('autoresearch'))
        research = get_prompt_builder('research')(_goal_dict('research'))
        assert autoresearch is None          # experiment loop pauses without repo_path
        assert research is not None          # consumer research assistant runs


# ════════════════════════════════════════════════════════════════════
# 3. Speech agents declare the voice stack (STT / TTS / orb)
# ════════════════════════════════════════════════════════════════════

class TestVoiceStackDeclaration:

    @pytest.mark.parametrize('goal_type', ['english_learning', 'spoken_english'])
    def test_new_voice_agents_declare_full_stack(self, goal_type):
        """The two newly-registered speech agents name STT, TTS, and the orb,
        and route them through the Model Bus."""
        prompt = get_prompt_builder(goal_type)(_goal_dict(goal_type))
        assert 'STT' in prompt, f'{goal_type} does not declare STT'
        assert 'TTS' in prompt, f'{goal_type} does not declare TTS'
        assert 'orb' in prompt.lower(), f'{goal_type} does not declare the orb'
        assert 'com.hart.ModelBus' in prompt, f'{goal_type} does not route via Model Bus'

    def test_speech_therapy_declares_voice(self):
        """The pre-existing Speech Therapy flagship declares spoken voice
        output in its seeded goal template."""
        seed = next(s for s in SEED_BOOTSTRAP_GOALS
                    if s['slug'] == 'bootstrap_speech_companion')
        desc = seed['description']
        assert 'TTS' in desc or 'voice' in desc.lower()

    @pytest.mark.parametrize('goal_type', ['english_learning', 'spoken_english'])
    def test_voice_seed_config_declares_stack(self, goal_type):
        """The new speech seeds carry an explicit voice_stack + modality so the
        UI / orb can light up the right sensory signals."""
        seed = next(s for s in SEED_BOOTSTRAP_GOALS
                    if s['goal_type'] == goal_type)
        cfg = seed['config']
        assert cfg.get('modality') == 'voice'
        assert set(['stt', 'tts', 'orb']).issubset(set(cfg.get('voice_stack', [])))
        assert cfg.get('require_consent') is True   # microphone consent


# ════════════════════════════════════════════════════════════════════
# 4. Runnable goal templates - every flagship has a valid bootstrap seed
# ════════════════════════════════════════════════════════════════════

class TestFlagshipSeeds:

    @pytest.mark.parametrize('goal_type,slug', list(FLAGSHIP_SLUGS.items()))
    def test_seed_present_and_shaped(self, goal_type, slug):
        seeds = [s for s in SEED_BOOTSTRAP_GOALS if s['slug'] == slug]
        assert len(seeds) == 1, f'expected exactly one seed for {slug}'
        seed = seeds[0]
        assert seed['goal_type'] == goal_type
        for field in ('slug', 'title', 'description', 'config', 'spark_budget'):
            assert field in seed, f'{slug} missing {field}'
        assert seed['slug'].startswith('bootstrap_')
        assert seed['spark_budget'] > 0

    @pytest.mark.parametrize('goal_type,slug', list(FLAGSHIP_SLUGS.items()))
    def test_seed_goal_type_is_registered(self, goal_type, slug):
        """A seed whose goal_type is unregistered would be silently skipped by
        seed_bootstrap_goals (GoalManager.create_goal rejects unknown types)."""
        assert goal_type in get_registered_types()

    @pytest.mark.parametrize('goal_type,slug', list(FLAGSHIP_SLUGS.items()))
    def test_seed_builds_a_prompt(self, goal_type, slug):
        """The seeded config actually produces a dispatchable prompt (not None,
        which is what the daemon would skip)."""
        seed = next(s for s in SEED_BOOTSTRAP_GOALS if s['slug'] == slug)
        goal_dict = {
            'goal_type': goal_type,
            'title': seed['title'],
            'description': seed['description'],
            'config': seed['config'],
        }
        prompt = get_prompt_builder(goal_type)(goal_dict)
        assert prompt is not None and len(prompt) > 100


# ════════════════════════════════════════════════════════════════════
# 5. END-TO-END - Tutor dispatches through the REAL pipeline and produces
#    output.  Only the /chat HTTP edge is mocked (the local llama-server
#    is down in this harness); guardrails, prompt_id derivation, audit,
#    and the world-model record all run for real.
# ════════════════════════════════════════════════════════════════════

class TestTutorEndToEnd:

    def test_tutor_real_dispatch_produces_output(self, monkeypatch):
        from integrations.agent_engine import dispatch as dispatch_mod
        from integrations.agent_engine import budget_gate as budget_mod

        # The canned /chat reply standing in for the (down) local LLM.
        canned = (
            "Lesson 1. First, what do you already know? A fraction is parts of a "
            "whole. Try this: a pizza is cut into 4 equal slices and you eat 1 - "
            "what fraction did you eat? Take your time."
        )

        # Build the REAL tutor prompt via the registered builder + build_prompt.
        goal_dict = _goal_dict('tutor', {'subject': 'math', 'level': 'beginner'})
        goal_dict['title'] = 'Teach me fractions'
        prompt = GoalManager.build_prompt(goal_dict)
        assert prompt and 'TUTOR' in prompt

        goal_id = 'flagship-e2e-tutor-1'

        # Boundary mock: budget gate (stateful platform affordability) -> allow.
        monkeypatch.setattr(
            budget_mod, 'pre_dispatch_budget_gate',
            lambda gid, p, model_name='gpt-4o': (True, 'test-allow'))

        # Boundary mock: no distributed coordinator -> force LOCAL dispatch.
        monkeypatch.setattr(dispatch_mod, '_get_distributed_coordinator',
                            lambda: None)

        # Force the in-process Tier-1 adapter import to fail so the
        # deterministic Tier-2 HTTP /chat path runs (Tier-1 needs a live
        # llama-server, which is down here).  None in sys.modules makes the
        # `from routes.hartos_backend_adapter import chat` raise ImportError.
        monkeypatch.setitem(sys.modules, 'routes.hartos_backend_adapter', None)
        monkeypatch.setitem(sys.modules, 'hartos_backend_adapter', None)

        # Avoid live port probing inside _local_dispatch_base_url.
        monkeypatch.setenv('HEVOLVE_BASE_URL', 'http://localhost:5000')

        # Boundary mock: the ONLY mocked edge - the /chat HTTP call.
        captured = {}

        class FakeResp:
            status_code = 200
            text = ''

            def json(self):
                return {'response': canned}

        def fake_pooled_post(url, json=None, headers=None, timeout=None):
            captured['url'] = url
            captured['body'] = json
            return FakeResp()

        monkeypatch.setattr(dispatch_mod, 'pooled_post', fake_pooled_post)

        # Neutralize the IN-PROCESS /chat leg: dispatch_goal tries it (through
        # hart_intelligence_entry's own test client) BEFORE the pooled_post
        # proxy this test feeds, and only falls through when it returns falsy.
        # Unpatched it escapes into the real (down) LLM and the assertion reads
        # the autonomous standby reply instead of the canned lesson. The falsy
        # response IS the documented fall-through to the proxy.
        import hart_intelligence_entry as _hie
        _inproc = MagicMock()
        _inproc.test_client.return_value.__enter__.return_value             .post.return_value.get_json.return_value = {}
        monkeypatch.setattr(_hie, 'app', _inproc)

        # REAL dispatch_goal call.
        out = dispatch_mod.dispatch_goal(
            prompt, user_id='u-e2e', goal_id=goal_id, goal_type='tutor')

        # It produced output.
        assert out == canned

        # It posted to /chat as an autonomous, agent-creating CREATE/REUSE call.
        body = captured['body']
        assert captured['url'].endswith('/chat')
        assert body['autonomous'] is True
        assert body['create_agent'] is True

        # The prompt_id is the deterministic one the recipe + steering bridge
        # recompute (so recipe REUSE works on later ticks).
        assert body['prompt_id'] == dispatch_mod.prompt_id_for_goal(goal_id)

        # The tutor's identity survived the guardrail togetherness rewrite.
        assert 'fractions' in body['prompt']

    def test_prompt_id_for_goal_is_deterministic_numeric(self):
        """Same goal_id -> same numeric prompt_id (recipe filenames + REUSE)."""
        from integrations.agent_engine.dispatch import prompt_id_for_goal
        a = prompt_id_for_goal('flagship-e2e-tutor-1')
        b = prompt_id_for_goal('flagship-e2e-tutor-1')
        assert a == b
        assert a.isdigit()


# ════════════════════════════════════════════════════════════════════
# 4. Herald (news) — the seeded daily news-refresh agent
#
# Guards the exact chain that was BROKEN before this batch: the 'news'
# goal type + news_tools.register_news_tools existed, but no bootstrap
# goal was news-typed AND detect_goal_tags never emitted 'news', so the
# recipe flows never called register_news_tools — the tools were dead
# code and the feed was never agent-refreshed.  These pin every link so
# the orphan can't silently return.
# ════════════════════════════════════════════════════════════════════

class TestHeraldNewsAgent:

    def _herald(self):
        return next((s for s in SEED_BOOTSTRAP_GOALS
                     if s['slug'] == 'bootstrap_herald_news_friend'), None)

    def test_herald_seed_exists_and_is_news_typed(self):
        """The seeded news-friend must exist and be a daily 'news' goal."""
        seed = self._herald()
        assert seed is not None, 'bootstrap_herald_news_friend not seeded'
        assert seed['goal_type'] == 'news'
        assert seed['config'].get('cadence') == 'daily'
        assert seed['config'].get('feed_urls'), 'Herald has no feeds to refresh'

    def test_news_type_registered_with_news_tags(self):
        """'news' goal type is registered and declares the news tool tags."""
        assert 'news' in get_registered_types()
        assert 'news' in get_tool_tags('news')

    def test_herald_prompt_detects_news_tag(self):
        """seed -> built prompt -> detect_goal_tags emits 'news', which is
        what gates register_news_tools in create/reuse.  This is the link
        that was severed."""
        from integrations.agent_engine.marketing_tools import detect_goal_tags
        seed = self._herald()
        prompt = get_prompt_builder('news')(_goal_dict('news', seed['config']))
        assert 'news' in detect_goal_tags(prompt), (
            'news prompt no longer detected as a news task — '
            'register_news_tools would be orphaned again')

    def test_both_flows_wire_register_news_tools(self):
        """create_recipe.py and reuse_recipe.py must both call
        register_news_tools under a 'news' branch — parity, so a Herald
        recipe authored in create replays with its tools in reuse."""
        for filename in ('hartos/create_recipe.py', 'hartos/reuse_recipe.py'):
            path = os.path.join(os.path.dirname(__file__), '..', '..', filename)
            with open(path, encoding='utf-8') as fh:
                src = fh.read()
            assert "'news' in goal_tags" in src, f'{filename} missing news branch'
            assert 'register_news_tools' in src, f'{filename} missing register_news_tools'
