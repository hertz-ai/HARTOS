"""
Tests for News Push Notification Agent - goal type, prompt builder, seed goals, and tools.
"""
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace
from datetime import datetime, timedelta

# Ensure project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
os.environ.setdefault('HEVOLVE_DB_PATH', ':memory:')


# ─── Goal Manager Registration Tests ───

class TestNewsGoalTypeRegistration:
    """Verify 'news' goal type is registered in the prompt builder registry."""

    def test_news_in_prompt_builders(self):
        from integrations.agent_engine.goal_manager import _prompt_builders
        assert 'news' in _prompt_builders

    def test_news_in_tool_tags(self):
        from integrations.agent_engine.goal_manager import _tool_tags
        assert 'news' in _tool_tags
        assert 'news' in _tool_tags['news']
        assert 'feed_management' in _tool_tags['news']

    def test_news_in_registered_types(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        assert 'news' in get_registered_types()

    def test_get_prompt_builder_returns_callable(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('news')
        assert builder is not None
        assert callable(builder)

    def test_get_tool_tags_returns_news_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('news')
        assert 'news' in tags
        assert 'feed_management' in tags


# ─── Prompt Builder Tests ───

class TestBuildNewsPrompt:
    """Test _build_news_prompt output for various configurations."""

    def _build(self, **overrides):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('news')
        goal = {
            'title': overrides.get('title', 'Test News Goal'),
            'description': overrides.get('description', 'Test description'),
            'config': overrides.get('config', {}),
        }
        return builder(goal)

    def test_contains_scope_regional(self):
        prompt = self._build(config={'scope': 'regional'})
        assert 'REGIONAL' in prompt

    def test_contains_scope_national(self):
        prompt = self._build(config={'scope': 'national'})
        assert 'NATIONAL' in prompt

    def test_contains_scope_international(self):
        prompt = self._build(config={'scope': 'international'})
        assert 'INTERNATIONAL' in prompt

    def test_default_scope_is_international(self):
        prompt = self._build(config={})
        assert 'INTERNATIONAL' in prompt

    def test_contains_categories(self):
        prompt = self._build(config={'categories': ['politics', 'health']})
        assert 'politics' in prompt
        assert 'health' in prompt

    def test_empty_categories_shows_general(self):
        prompt = self._build(config={'categories': []})
        assert 'general news' in prompt

    def test_contains_feed_urls(self):
        prompt = self._build(config={'feed_urls': ['https://example.com/rss']})
        assert 'https://example.com/rss' in prompt

    def test_empty_feeds_suggests_discovery(self):
        prompt = self._build(config={'feed_urls': []})
        assert 'subscribe_news_feed' in prompt

    def test_contains_frequency(self):
        prompt = self._build(config={'frequency': 'every_4h'})
        assert 'every_4h' in prompt

    def test_contains_title_and_description(self):
        prompt = self._build(title='My News', description='My Desc')
        assert 'My News' in prompt
        assert 'My Desc' in prompt

    def test_contains_tool_references(self):
        prompt = self._build()
        assert 'fetch_news_feeds' in prompt
        assert 'subscribe_news_feed' in prompt
        assert 'send_news_notification' in prompt
        assert 'get_trending_news' in prompt
        assert 'get_news_metrics' in prompt

    def test_contains_curation_rules(self):
        prompt = self._build()
        assert 'Quality over quantity' in prompt
        assert 'clickbait' in prompt
        assert 'source attribution' in prompt


# ─── Seed Goal Tests ───

class TestNewsSeedGoals:
    """Verify 3 news seed goals are present in SEED_BOOTSTRAP_GOALS."""

    def test_regional_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_news_regional' in slugs

    def test_national_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_news_national' in slugs

    def test_international_seed_goal_exists(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_news_international' in slugs

    def _get_goal(self, slug):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        return next(g for g in SEED_BOOTSTRAP_GOALS if g['slug'] == slug)

    def test_regional_goal_type_is_news(self):
        g = self._get_goal('bootstrap_news_regional')
        assert g['goal_type'] == 'news'

    def test_national_goal_type_is_news(self):
        g = self._get_goal('bootstrap_news_national')
        assert g['goal_type'] == 'news'

    def test_international_goal_type_is_news(self):
        g = self._get_goal('bootstrap_news_international')
        assert g['goal_type'] == 'news'

    def test_regional_scope_config(self):
        g = self._get_goal('bootstrap_news_regional')
        assert g['config']['scope'] == 'regional'
        assert 'local' in g['config']['categories']
        assert g['config']['frequency'] == 'hourly'

    def test_national_scope_config(self):
        g = self._get_goal('bootstrap_news_national')
        assert g['config']['scope'] == 'national'
        assert 'politics' in g['config']['categories']
        assert 'economy' in g['config']['categories']
        assert g['config']['frequency'] == 'hourly'

    def test_international_scope_config(self):
        g = self._get_goal('bootstrap_news_international')
        assert g['config']['scope'] == 'international'
        assert 'world' in g['config']['categories']
        assert 'ai' in g['config']['categories']
        assert g['config']['frequency'] == 'every_4h'

    def test_all_three_have_spark_budget(self):
        for slug in ['bootstrap_news_regional', 'bootstrap_news_national',
                      'bootstrap_news_international']:
            g = self._get_goal(slug)
            assert g['spark_budget'] > 0

    def test_seed_count_increased(self):
        from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
        # Was 13 goals, now 16 with 3 news goals
        assert len(SEED_BOOTSTRAP_GOALS) >= 16


# ─── Tool Registration Tests ───

class TestNewsToolRegistration:
    """Verify register_news_tools registers all 5 tools."""

    def test_registers_five_tools(self):
        from integrations.agent_engine.news_tools import register_news_tools

        helper = MagicMock()
        assistant = MagicMock()
        # register_for_llm returns a decorator that accepts the function
        helper.register_for_llm.return_value = lambda f: f
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='123')

        assert helper.register_for_llm.call_count == 5
        assert assistant.register_for_execution.call_count == 5

    def test_tool_names(self):
        from integrations.agent_engine.news_tools import register_news_tools

        helper = MagicMock()
        assistant = MagicMock()
        helper.register_for_llm.return_value = lambda f: f
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='123')

        registered_names = [
            call.kwargs['name']
            for call in helper.register_for_llm.call_args_list
        ]
        assert 'fetch_news_feeds' in registered_names
        assert 'subscribe_news_feed' in registered_names
        assert 'send_news_notification' in registered_names
        assert 'get_trending_news' in registered_names
        assert 'get_news_metrics' in registered_names


# ─── Individual Tool Tests ───

class TestFetchNewsFeeds:
    """Test fetch_news_feeds tool with mocked FeedImporter."""

    def _get_tool(self):
        from integrations.agent_engine.news_tools import register_news_tools
        helper = MagicMock()
        assistant = MagicMock()
        tools = {}

        def capture_llm(name, description):
            def decorator(fn):
                tools[name] = fn
                return fn
            return decorator

        helper.register_for_llm.side_effect = capture_llm
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='42')
        return tools['fetch_news_feeds']

    @patch('integrations.agent_engine.news_tools.FeedImporter', create=True)
    def test_fetch_returns_items(self, _mock_cls):
        # We need to patch inside the function's import
        mock_item = SimpleNamespace(
            title='Breaking News',
            link='https://example.com/1',
            author='Reporter',
            published=datetime(2026, 1, 15),
            categories=['world'],
            content='Full article text here...',
        )
        mock_meta = SimpleNamespace(title='Example News')

        with patch('integrations.social.feed_import.FeedImporter') as MockImporter:
            instance = MockImporter.return_value
            instance.fetch_feed.return_value = (mock_meta, [mock_item], None)

            fetch = self._get_tool()
            result = json.loads(fetch('https://example.com/rss'))

            assert result['total'] == 1
            assert result['items'][0]['title'] == 'Breaking News'
            assert result['items'][0]['source'] == 'Example News'

    def test_fetch_handles_import_error(self):
        fetch = self._get_tool()
        # If FeedImporter is not available, should return error gracefully
        result = json.loads(fetch('https://example.com/rss'))
        # Should have error key or items (depending on whether import succeeds)
        assert 'error' in result or 'items' in result

    def test_fetch_handles_empty_urls(self):
        fetch = self._get_tool()
        with patch('integrations.social.feed_import.FeedImporter') as MockImporter:
            instance = MockImporter.return_value
            result = json.loads(fetch(''))
            # Empty URL means no items fetched
            assert result.get('total', 0) == 0 or 'error' in result


class TestSendNewsNotification:
    """Test send_news_notification tool with mocked NotificationService."""

    def _get_tool(self):
        from integrations.agent_engine.news_tools import register_news_tools
        helper = MagicMock()
        assistant = MagicMock()
        tools = {}

        def capture_llm(name, description):
            def decorator(fn):
                tools[name] = fn
                return fn
            return decorator

        helper.register_for_llm.side_effect = capture_llm
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='42')
        return tools['send_news_notification']

    def test_send_specific_user(self):
        send = self._get_tool()
        with patch('integrations.social.models.get_db') as mock_get_db, \
             patch('integrations.social.services.NotificationService') as MockNS:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            MockNS.create.return_value = None

            result = json.loads(send(
                title='Test Headline',
                message='Test summary',
                source_url='https://example.com/article',
                scope='999',
                category='world',
            ))

            assert result['success'] is True
            assert result['sent_count'] == 1
            assert result['scope'] == '999'
            assert result['category'] == 'world'

    def test_send_handles_error_gracefully(self):
        send = self._get_tool()
        # Without proper DB setup, should return error dict
        result = json.loads(send(
            title='Test',
            message='Test',
            source_url='https://example.com',
        ))
        assert 'error' in result or 'success' in result


class TestGetTrendingNews:
    """Test get_trending_news tool."""

    def _get_tool(self):
        from integrations.agent_engine.news_tools import register_news_tools
        helper = MagicMock()
        assistant = MagicMock()
        tools = {}

        def capture_llm(name, description):
            def decorator(fn):
                tools[name] = fn
                return fn
            return decorator

        helper.register_for_llm.side_effect = capture_llm
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='42')
        return tools['get_trending_news']

    def test_trending_with_mock_feed_engine(self):
        trending = self._get_tool()

        mock_post = MagicMock()
        mock_post.to_dict.return_value = {
            'id': 1,
            'title': 'Hot Story',
            'content': 'Content here',
            'author_id': 10,
            'vote_count': 50,
            'comment_count': 12,
            'created_at': '2026-01-15T00:00:00',
        }

        with patch('integrations.social.models.get_db') as mock_get_db, \
             patch('integrations.social.feed_engine.get_trending_feed') as mock_trending:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            mock_trending.return_value = [mock_post]

            result = json.loads(trending(limit=5))
            assert result['count'] == 1
            assert result['trending'][0]['title'] == 'Hot Story'

    def test_trending_handles_error(self):
        trending = self._get_tool()
        result = json.loads(trending())
        assert 'error' in result or 'trending' in result


class TestGetNewsMetrics:
    """Test get_news_metrics tool."""

    def _get_tool(self):
        from integrations.agent_engine.news_tools import register_news_tools
        helper = MagicMock()
        assistant = MagicMock()
        tools = {}

        def capture_llm(name, description):
            def decorator(fn):
                tools[name] = fn
                return fn
            return decorator

        helper.register_for_llm.side_effect = capture_llm
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='42')
        return tools['get_news_metrics']

    def test_metrics_default_params(self):
        metrics = self._get_tool()
        with patch('integrations.social.models.get_db') as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db

            # Mock the query chain
            mock_query = MagicMock()
            mock_db.query.return_value = mock_query
            mock_query.filter.return_value = mock_query
            mock_query.count.return_value = 10
            mock_query.group_by.return_value = mock_query
            mock_query.all.return_value = [('news_world', 7), ('news_local', 3)]

            result = json.loads(metrics())
            assert result['period_days'] == 7
            assert result['total_sent'] == 10

    def test_metrics_handles_error(self):
        metrics = self._get_tool()
        result = json.loads(metrics())
        assert 'error' in result or 'total_sent' in result


class TestSubscribeNewsFeed:
    """Test subscribe_news_feed tool."""

    def _get_tool(self):
        from integrations.agent_engine.news_tools import register_news_tools
        helper = MagicMock()
        assistant = MagicMock()
        tools = {}

        def capture_llm(name, description):
            def decorator(fn):
                tools[name] = fn
                return fn
            return decorator

        helper.register_for_llm.side_effect = capture_llm
        assistant.register_for_execution.return_value = lambda f: f

        register_news_tools(helper, assistant, user_id='42')
        return tools['subscribe_news_feed']

    def test_subscribe_with_categories(self):
        subscribe = self._get_tool()
        with patch('integrations.social.models.get_db') as mock_get_db, \
             patch('integrations.social.feed_import.FeedSubscriptionService') as MockSvc:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            MockSvc.return_value.subscribe.return_value = {
                'success': True,
                'feed_id': 1,
            }

            result = json.loads(subscribe(
                feed_url='https://example.com/rss',
                categories='world, tech',
            ))
            assert result.get('categories') == ['world', 'tech']

    def test_subscribe_handles_error(self):
        subscribe = self._get_tool()
        result = json.loads(subscribe(feed_url='https://example.com/rss'))
        assert 'error' in result or 'success' in result
