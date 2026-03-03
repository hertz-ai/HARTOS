"""
HevolveSocial - Game Catalog
Centralized registry of 100+ games powered by 6 engine classes.
Config-driven variants: 24 OpenTDB categories x 3 modes = 72 trivia alone.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger('hevolve_social')


# ─── OpenTDB Category IDs ──────────────────────────────────────────
# https://opentdb.com/api_config.php
OPENTDB_CATEGORIES = {
    9: 'General Knowledge', 10: 'Books', 11: 'Film', 12: 'Music',
    13: 'Musicals & Theatre', 14: 'Television', 15: 'Video Games',
    16: 'Board Games', 17: 'Science & Nature', 18: 'Computers',
    19: 'Mathematics', 20: 'Mythology', 21: 'Sports', 22: 'Geography',
    23: 'History', 24: 'Politics', 25: 'Art', 26: 'Celebrities',
    27: 'Animals', 28: 'Vehicles', 29: 'Comics', 30: 'Gadgets',
    31: 'Anime & Manga', 32: 'Cartoons',
}

TRIVIA_MODES = {
    'classic': {'label': 'Classic', 'time_per_question': 15, 'default_rounds': 10},
    'speed': {'label': 'Speed Round', 'time_per_question': 10, 'default_rounds': 20},
    'survival': {'label': 'Survival', 'time_per_question': 12, 'default_rounds': 30},
}

# ─── Catalog Data ──────────────────────────────────────────────────

def _build_trivia_entries():
    """Generate 72 trivia variants from 24 categories x 3 modes."""
    entries = []
    sort_base = 100

    for cat_id, cat_name in OPENTDB_CATEGORIES.items():
        for mode_key, mode_info in TRIVIA_MODES.items():
            slug = f"trivia-{cat_name.lower().replace(' & ', '-').replace(' ', '-')}-{mode_key}"
            title = f"{cat_name} — {mode_info['label']}"

            entries.append({
                'id': slug,
                'engine': 'opentdb_trivia',
                'title': title,
                'category': 'trivia',
                'audience': 'adult',
                'thumbnail': f"trivia_{cat_id}.webp",
                'multiplayer': True,
                'min_players': 1,
                'max_players': 8,
                'solo_allowed': True,
                'difficulty_levels': ['easy', 'medium', 'hard'],
                'default_rounds': mode_info['default_rounds'],
                'engine_config': {
                    'opentdb_category_id': cat_id,
                    'mode': mode_key,
                    'time_per_question': mode_info['time_per_question'],
                },
                'tags': ['trivia', cat_name.lower(), mode_key],
                'featured': cat_id in (9, 11, 17, 18, 22, 23) and mode_key == 'classic',
                'sort_order': sort_base,
            })
            sort_base += 1

    return entries


BOARD_GAMES = [
    {
        'id': 'board-tictactoe',
        'engine': 'boardgame',
        'title': 'Tic-Tac-Toe',
        'category': 'board',
        'audience': 'all',
        'thumbnail': 'board_tictactoe.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'tictactoe', 'board_size': 3},
        'tags': ['board', 'classic', 'quick'],
        'featured': True,
        'sort_order': 200,
    },
    {
        'id': 'board-connect4',
        'engine': 'boardgame',
        'title': 'Connect Four',
        'category': 'board',
        'audience': 'all',
        'thumbnail': 'board_connect4.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'connect4', 'board_size': 7},
        'tags': ['board', 'classic', 'strategy'],
        'featured': True,
        'sort_order': 201,
    },
    {
        'id': 'board-checkers',
        'engine': 'boardgame',
        'title': 'Checkers',
        'category': 'board',
        'audience': 'all',
        'thumbnail': 'board_checkers.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'checkers', 'board_size': 8},
        'tags': ['board', 'classic', 'strategy'],
        'featured': False,
        'sort_order': 202,
    },
    {
        'id': 'board-reversi',
        'engine': 'boardgame',
        'title': 'Reversi (Othello)',
        'category': 'board',
        'audience': 'adult',
        'thumbnail': 'board_reversi.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'reversi', 'board_size': 8},
        'tags': ['board', 'strategy'],
        'featured': False,
        'sort_order': 203,
    },
    {
        'id': 'board-mancala',
        'engine': 'boardgame',
        'title': 'Mancala',
        'category': 'board',
        'audience': 'all',
        'thumbnail': 'board_mancala.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'mancala', 'board_size': 6},
        'tags': ['board', 'classic', 'strategy'],
        'featured': False,
        'sort_order': 204,
    },
    {
        'id': 'board-dots-and-boxes',
        'engine': 'boardgame',
        'title': 'Dots & Boxes',
        'category': 'board',
        'audience': 'all',
        'thumbnail': 'board_dots.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 4,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'board_type': 'dots_and_boxes', 'board_size': 5},
        'tags': ['board', 'party', 'quick'],
        'featured': False,
        'sort_order': 205,
    },
    {
        'id': 'board-battleship',
        'engine': 'boardgame',
        'title': 'Battleship',
        'category': 'board',
        'audience': 'adult',
        'thumbnail': 'board_battleship.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'battleship', 'board_size': 10},
        'tags': ['board', 'strategy', 'classic'],
        'featured': True,
        'sort_order': 206,
    },
    {
        'id': 'board-nim',
        'engine': 'boardgame',
        'title': 'Nim',
        'category': 'board',
        'audience': 'adult',
        'thumbnail': 'board_nim.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'board_type': 'nim', 'board_size': 4},
        'tags': ['board', 'math', 'strategy'],
        'featured': False,
        'sort_order': 207,
    },
]


ARCADE_GAMES = [
    {
        'id': 'arcade-snake',
        'engine': 'phaser',
        'title': 'Snake',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_snake.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'snake', 'starting_lives': 1,
                          'target_score': 0},
        'tags': ['arcade', 'classic', 'quick'],
        'featured': True,
        'sort_order': 300,
    },
    {
        'id': 'arcade-breakout',
        'engine': 'phaser',
        'title': 'Breakout',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_breakout.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'breakout', 'starting_lives': 3},
        'tags': ['arcade', 'classic'],
        'featured': True,
        'sort_order': 301,
    },
    {
        'id': 'arcade-bubble-shooter',
        'engine': 'phaser',
        'title': 'Bubble Shooter',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_bubble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'bubble_shooter', 'starting_lives': 5},
        'tags': ['arcade', 'puzzle', 'casual'],
        'featured': False,
        'sort_order': 302,
    },
    {
        'id': 'arcade-pong',
        'engine': 'phaser',
        'title': 'Pong',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_pong.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'pong', 'target_score': 11},
        'tags': ['arcade', 'classic', 'competitive'],
        'featured': True,
        'sort_order': 303,
    },
    {
        'id': 'arcade-runner',
        'engine': 'phaser',
        'title': 'Endless Runner',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_runner.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'scene_id': 'runner', 'starting_lives': 1},
        'tags': ['arcade', 'endless', 'casual'],
        'featured': False,
        'sort_order': 304,
    },
    {
        'id': 'arcade-flappy',
        'engine': 'phaser',
        'title': 'Flappy Bird',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_flappy.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'scene_id': 'flappy', 'starting_lives': 1},
        'tags': ['arcade', 'casual'],
        'featured': False,
        'sort_order': 305,
    },
    {
        'id': 'arcade-match3',
        'engine': 'phaser',
        'title': 'Gem Match',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_match3.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'match3', 'target_score': 1000,
                          'max_duration_seconds': 120},
        'tags': ['arcade', 'puzzle', 'casual'],
        'featured': True,
        'sort_order': 306,
    },
    {
        'id': 'arcade-block-stack',
        'engine': 'phaser',
        'title': 'Block Stacker',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_blocks.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'block_stack', 'starting_lives': 1},
        'tags': ['arcade', 'puzzle', 'classic'],
        'featured': False,
        'sort_order': 307,
    },
    {
        'id': 'arcade-space-invaders',
        'engine': 'phaser',
        'title': 'Space Invaders',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_space.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': ['easy', 'medium', 'hard'],
        'default_rounds': 1,
        'engine_config': {'scene_id': 'space_invaders', 'starting_lives': 3},
        'tags': ['arcade', 'classic', 'shooter'],
        'featured': False,
        'sort_order': 308,
    },
    {
        'id': 'arcade-2048',
        'engine': 'phaser',
        'title': '2048',
        'category': 'arcade',
        'audience': 'all',
        'thumbnail': 'arcade_2048.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 2,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'scene_id': '2048', 'target_score': 2048},
        'tags': ['arcade', 'puzzle', 'math'],
        'featured': False,
        'sort_order': 309,
    },
]


WORD_GAMES = [
    {
        'id': 'word-scramble-4',
        'engine': 'word_scramble',
        'title': 'Word Scramble (Easy)',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_scramble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 6,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 10,
        'engine_config': {'word_length': 4, 'round_time': 30},
        'tags': ['word', 'easy', 'quick'],
        'featured': False,
        'sort_order': 400,
    },
    {
        'id': 'word-scramble-5',
        'engine': 'word_scramble',
        'title': 'Word Scramble',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_scramble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 6,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 10,
        'engine_config': {'word_length': 5, 'round_time': 30},
        'tags': ['word', 'classic'],
        'featured': True,
        'sort_order': 401,
    },
    {
        'id': 'word-scramble-6',
        'engine': 'word_scramble',
        'title': 'Word Scramble (Medium)',
        'category': 'word',
        'audience': 'adult',
        'thumbnail': 'word_scramble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 6,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 10,
        'engine_config': {'word_length': 6, 'round_time': 45},
        'tags': ['word', 'medium'],
        'featured': False,
        'sort_order': 402,
    },
    {
        'id': 'word-scramble-7',
        'engine': 'word_scramble',
        'title': 'Word Scramble (Hard)',
        'category': 'word',
        'audience': 'adult',
        'thumbnail': 'word_scramble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 6,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 8,
        'engine_config': {'word_length': 7, 'round_time': 60},
        'tags': ['word', 'hard', 'challenge'],
        'featured': False,
        'sort_order': 403,
    },
    {
        'id': 'word-scramble-8',
        'engine': 'word_scramble',
        'title': 'Word Scramble (Expert)',
        'category': 'word',
        'audience': 'adult',
        'thumbnail': 'word_scramble.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 6,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 6,
        'engine_config': {'word_length': 8, 'round_time': 90},
        'tags': ['word', 'expert', 'challenge'],
        'featured': False,
        'sort_order': 404,
    },
    {
        'id': 'word-search-animals',
        'engine': 'word_search',
        'title': 'Word Search: Animals',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_search.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'grid_size': 10, 'word_count': 8, 'theme': 'animals'},
        'tags': ['word', 'search', 'casual'],
        'featured': False,
        'sort_order': 410,
    },
    {
        'id': 'word-search-space',
        'engine': 'word_search',
        'title': 'Word Search: Space',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_search.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'grid_size': 12, 'word_count': 10, 'theme': 'space'},
        'tags': ['word', 'search', 'science'],
        'featured': False,
        'sort_order': 411,
    },
    {
        'id': 'word-search-food',
        'engine': 'word_search',
        'title': 'Word Search: Food',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_search.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'grid_size': 10, 'word_count': 8, 'theme': 'food'},
        'tags': ['word', 'search', 'casual'],
        'featured': False,
        'sort_order': 412,
    },
    {
        'id': 'word-search-tech',
        'engine': 'word_search',
        'title': 'Word Search: Tech',
        'category': 'word',
        'audience': 'adult',
        'thumbnail': 'word_search.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'grid_size': 12, 'word_count': 10, 'theme': 'tech'},
        'tags': ['word', 'search', 'tech'],
        'featured': False,
        'sort_order': 413,
    },
    # Original word_chain from existing game_types.py
    {
        'id': 'word-chain',
        'engine': 'word_chain',
        'title': 'Word Chain',
        'category': 'word',
        'audience': 'all',
        'thumbnail': 'word_chain.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 6,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 10,
        'engine_config': {},
        'tags': ['word', 'classic', 'turn-based'],
        'featured': False,
        'sort_order': 420,
    },
]


PUZZLE_GAMES = [
    {
        'id': 'puzzle-sudoku-easy',
        'engine': 'sudoku',
        'title': 'Sudoku (Easy)',
        'category': 'puzzle',
        'audience': 'all',
        'thumbnail': 'puzzle_sudoku.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'difficulty': 'easy'},
        'tags': ['puzzle', 'logic', 'relaxing'],
        'featured': False,
        'sort_order': 500,
    },
    {
        'id': 'puzzle-sudoku-medium',
        'engine': 'sudoku',
        'title': 'Sudoku',
        'category': 'puzzle',
        'audience': 'adult',
        'thumbnail': 'puzzle_sudoku.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'difficulty': 'medium'},
        'tags': ['puzzle', 'logic', 'challenge'],
        'featured': True,
        'sort_order': 501,
    },
    {
        'id': 'puzzle-sudoku-hard',
        'engine': 'sudoku',
        'title': 'Sudoku (Hard)',
        'category': 'puzzle',
        'audience': 'adult',
        'thumbnail': 'puzzle_sudoku.webp',
        'multiplayer': True,
        'min_players': 1, 'max_players': 4,
        'solo_allowed': True,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {'difficulty': 'hard'},
        'tags': ['puzzle', 'logic', 'expert'],
        'featured': False,
        'sort_order': 502,
    },
    # Original collab_puzzle from existing game_types.py
    {
        'id': 'puzzle-thought-experiment',
        'engine': 'collab_puzzle',
        'title': 'Thought Experiment',
        'category': 'puzzle',
        'audience': 'adult',
        'thumbnail': 'puzzle_thought.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 6,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 1,
        'engine_config': {},
        'tags': ['puzzle', 'cooperative', 'creative'],
        'featured': False,
        'sort_order': 510,
    },
]


PARTY_GAMES = [
    # Original trivia (uses built-in questions, not OpenTDB)
    {
        'id': 'party-quick-trivia',
        'engine': 'trivia',
        'title': 'Quick Trivia',
        'category': 'party',
        'audience': 'all',
        'thumbnail': 'party_trivia.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 8,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 5,
        'engine_config': {'category': 'general'},
        'tags': ['party', 'trivia', 'quick'],
        'featured': False,
        'sort_order': 600,
    },
    {
        'id': 'party-tech-trivia',
        'engine': 'trivia',
        'title': 'Tech Trivia',
        'category': 'party',
        'audience': 'adult',
        'thumbnail': 'party_tech.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 8,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 5,
        'engine_config': {'category': 'tech'},
        'tags': ['party', 'trivia', 'tech'],
        'featured': False,
        'sort_order': 601,
    },
    {
        'id': 'party-science-trivia',
        'engine': 'trivia',
        'title': 'Science Trivia',
        'category': 'party',
        'audience': 'all',
        'thumbnail': 'party_science.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 8,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 5,
        'engine_config': {'category': 'science'},
        'tags': ['party', 'trivia', 'science'],
        'featured': False,
        'sort_order': 602,
    },
    # Compute challenge (from existing game_types.py)
    {
        'id': 'party-compute-race',
        'engine': 'compute_challenge',
        'title': 'Compute Race',
        'category': 'party',
        'audience': 'adult',
        'thumbnail': 'party_compute.webp',
        'multiplayer': True,
        'min_players': 2, 'max_players': 4,
        'solo_allowed': False,
        'difficulty_levels': None,
        'default_rounds': 5,
        'engine_config': {'target_tasks': 5},
        'tags': ['party', 'compute', 'race'],
        'featured': False,
        'sort_order': 610,
    },
]


# ─── Full Catalog ──────────────────────────────────────────────────

def _build_full_catalog() -> List[Dict]:
    """Build complete catalog: trivia variants + static entries."""
    catalog = []
    catalog.extend(_build_trivia_entries())
    catalog.extend(BOARD_GAMES)
    catalog.extend(ARCADE_GAMES)
    catalog.extend(WORD_GAMES)
    catalog.extend(PUZZLE_GAMES)
    catalog.extend(PARTY_GAMES)
    return catalog


# Cached in-memory catalog
_CATALOG: Optional[List[Dict]] = None
_CATALOG_INDEX: Optional[Dict[str, Dict]] = None


def _ensure_catalog():
    global _CATALOG, _CATALOG_INDEX
    if _CATALOG is None:
        _CATALOG = _build_full_catalog()
        _CATALOG_INDEX = {entry['id']: entry for entry in _CATALOG}
        logger.info("Game catalog built: %d games", len(_CATALOG))


def get_catalog_entry(game_id: str) -> Optional[Dict]:
    """Get a single catalog entry by ID."""
    _ensure_catalog()
    return _CATALOG_INDEX.get(game_id)


def list_catalog(
    audience: str = None,
    category: str = None,
    multiplayer: bool = None,
    featured: bool = None,
    tag: str = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict:
    """List catalog entries with filters. Returns {items, total, categories}."""
    _ensure_catalog()

    filtered = list(_CATALOG)

    if audience:
        filtered = [e for e in filtered
                    if e['audience'] == audience or e['audience'] == 'all']
    if category:
        filtered = [e for e in filtered if e['category'] == category]
    if multiplayer is not None:
        filtered = [e for e in filtered if e['multiplayer'] == multiplayer]
    if featured is not None:
        filtered = [e for e in filtered if e.get('featured') == featured]
    if tag:
        tag_lower = tag.lower()
        filtered = [e for e in filtered if tag_lower in e.get('tags', [])]
    if search:
        search_lower = search.lower()
        filtered = [e for e in filtered
                    if search_lower in e['title'].lower()
                    or search_lower in e.get('category', '')
                    or any(search_lower in t for t in e.get('tags', []))]

    filtered.sort(key=lambda e: e.get('sort_order', 999))
    total = len(filtered)

    # Category counts (from full filtered set before pagination)
    categories = {}
    for e in filtered:
        cat = e.get('category', 'other')
        categories[cat] = categories.get(cat, 0) + 1

    items = filtered[offset:offset + limit]

    return {
        'items': items,
        'total': total,
        'categories': categories,
    }


def get_engine_for_catalog_entry(game_id: str) -> Optional[str]:
    """Resolve catalog game ID to engine name."""
    entry = get_catalog_entry(game_id)
    if entry:
        return entry['engine']
    return None


def get_config_for_catalog_entry(game_id: str, overrides: Dict = None) -> Dict:
    """Get merged engine_config for a catalog entry + user overrides."""
    entry = get_catalog_entry(game_id)
    if not entry:
        return overrides or {}

    config = dict(entry.get('engine_config', {}))
    if overrides:
        config.update(overrides)

    # Apply difficulty override to engine config
    if 'difficulty' in (overrides or {}):
        config['difficulty'] = overrides['difficulty']

    return config
