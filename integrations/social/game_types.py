"""
HevolveSocial - Game Type Strategies
Each game type implements initialize, validate_move, apply_move,
check_round_end, check_game_end, calculate_results.
"""
import logging
import random
from datetime import datetime
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger('hevolve_social')


class BaseGameType:
    """Base class for game types."""

    def initialize(self, config: Dict, total_rounds: int,
                   player_ids: List[str]) -> Dict:
        """Create initial game state."""
        raise NotImplementedError

    def validate_move(self, game_state: Dict, user_id: str,
                      move_data: Dict) -> Tuple[bool, str]:
        """Check if a move is valid. Returns (valid, reason)."""
        raise NotImplementedError

    def apply_move(self, game_state: Dict, user_id: str,
                   move_data: Dict) -> Tuple[Dict, int]:
        """Apply move. Returns (new_state, score_delta)."""
        raise NotImplementedError

    def check_round_end(self, game_state: Dict) -> bool:
        """Check if current round is over."""
        raise NotImplementedError

    def check_game_end(self, game_state: Dict, current_round: int,
                       total_rounds: int) -> bool:
        """Check if game should end."""
        return current_round > total_rounds

    def calculate_results(self, game_state: Dict,
                          participants) -> Dict[str, Dict]:
        """Calculate final results. Returns {user_id: {result, ...}}."""
        raise NotImplementedError


class TriviaGame(BaseGameType):
    """Timed trivia rounds. First correct answer scores."""

    # Pre-seeded question categories
    CATEGORIES = {
        'general': [
            {'q': 'What is the largest planet in our solar system?', 'a': 'jupiter', 'options': ['mars', 'jupiter', 'saturn', 'neptune']},
            {'q': 'What language has the most native speakers?', 'a': 'mandarin', 'options': ['english', 'mandarin', 'spanish', 'hindi']},
            {'q': 'What is the chemical symbol for gold?', 'a': 'au', 'options': ['ag', 'au', 'fe', 'cu']},
            {'q': 'Which ocean is the deepest?', 'a': 'pacific', 'options': ['atlantic', 'pacific', 'indian', 'arctic']},
            {'q': 'How many bits in a byte?', 'a': '8', 'options': ['4', '8', '16', '32']},
        ],
        'tech': [
            {'q': 'Who created Linux?', 'a': 'linus torvalds', 'options': ['bill gates', 'linus torvalds', 'steve jobs', 'dennis ritchie']},
            {'q': 'What does HTTP stand for?', 'a': 'hypertext transfer protocol', 'options': ['hypertext transfer protocol', 'high tech transfer protocol', 'hypertext transport protocol', 'high text transfer program']},
            {'q': 'Which company created Python?', 'a': 'none', 'options': ['google', 'microsoft', 'none', 'sun microsystems']},
            {'q': 'What year was the first iPhone released?', 'a': '2007', 'options': ['2005', '2007', '2008', '2010']},
            {'q': 'What does GPU stand for?', 'a': 'graphics processing unit', 'options': ['graphics processing unit', 'general processing unit', 'graphical power unit', 'graphics protocol unit']},
        ],
        'science': [
            {'q': 'What is the speed of light in km/s (approx)?', 'a': '300000', 'options': ['150000', '300000', '500000', '1000000']},
            {'q': 'What is the powerhouse of the cell?', 'a': 'mitochondria', 'options': ['nucleus', 'mitochondria', 'ribosome', 'golgi']},
            {'q': 'What gas do plants absorb?', 'a': 'carbon dioxide', 'options': ['oxygen', 'nitrogen', 'carbon dioxide', 'hydrogen']},
            {'q': 'How many chromosomes do humans have?', 'a': '46', 'options': ['23', '46', '48', '44']},
            {'q': 'What planet is known as the Red Planet?', 'a': 'mars', 'options': ['venus', 'mars', 'jupiter', 'mercury']},
        ],
    }

    def initialize(self, config, total_rounds, player_ids):
        category = config.get('category', 'general')
        questions = list(self.CATEGORIES.get(category, self.CATEGORIES['general']))
        random.shuffle(questions)
        return {
            'questions': questions[:total_rounds],
            'current_question_idx': 0,
            'answers': {},       # {round_idx: {user_id: answer}}
            'round_scores': {},  # {user_id: [round_score, ...]}
            'round_answered': False,
            'players': player_ids,
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'answer' not in move_data:
            return False, "Missing 'answer' in move_data"
        idx = game_state.get('current_question_idx', 0)
        answers = game_state.get('answers', {})
        round_answers = answers.get(str(idx), {})
        if user_id in round_answers:
            return False, "Already answered this round"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        idx = game_state.get('current_question_idx', 0)
        questions = game_state.get('questions', [])
        if idx >= len(questions):
            return game_state, 0

        answer = str(move_data['answer']).lower().strip()
        correct_answer = questions[idx]['a'].lower().strip()

        answers = game_state.get('answers', {})
        round_answers = answers.get(str(idx), {})
        round_answers[user_id] = answer
        answers[str(idx)] = round_answers
        game_state['answers'] = answers

        score_delta = 0
        if answer == correct_answer and not game_state.get('round_answered'):
            score_delta = 10  # First correct answer gets points
            game_state['round_answered'] = True

        return game_state, score_delta

    def check_round_end(self, game_state):
        idx = game_state.get('current_question_idx', 0)
        answers = game_state.get('answers', {})
        round_answers = answers.get(str(idx), {})
        players = game_state.get('players', [])
        if len(round_answers) >= len(players):
            game_state['current_question_idx'] = idx + 1
            game_state['round_answered'] = False
            return True
        return game_state.get('round_answered', False)

    def check_game_end(self, game_state, current_round, total_rounds):
        idx = game_state.get('current_question_idx', 0)
        questions = game_state.get('questions', [])
        return idx >= len(questions) or current_round > total_rounds

    def calculate_results(self, game_state, participants):
        scores = {p.user_id: p.score for p in participants}
        max_score = max(scores.values()) if scores else 0
        results = {}
        for uid, score in scores.items():
            if score == max_score and max_score > 0:
                results[uid] = {'result': 'win', 'score': score}
            elif score == max_score:
                results[uid] = {'result': 'draw', 'score': score}
            else:
                results[uid] = {'result': 'loss', 'score': score}
        return results


class WordChainGame(BaseGameType):
    """Turn-based word chain. Each word starts with last letter of previous."""

    def initialize(self, config, total_rounds, player_ids):
        topic = config.get('topic', '')
        return {
            'words': [],
            'turn_order': player_ids,
            'current_turn_idx': 0,
            'topic': topic,
            'used_words': set(),
            'skips': {uid: 0 for uid in player_ids},
            'max_skips': 2,
            'players': player_ids,
        }

    def validate_move(self, game_state, user_id, move_data):
        turn_order = game_state.get('turn_order', [])
        current_idx = game_state.get('current_turn_idx', 0)
        if not turn_order:
            return False, "No players"
        expected_player = turn_order[current_idx % len(turn_order)]
        if user_id != expected_player:
            return False, "Not your turn"
        if 'word' not in move_data and not move_data.get('skip'):
            return False, "Missing 'word' in move_data"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        words = game_state.get('words', [])
        # Handle serialized set
        used_words = game_state.get('used_words', [])
        if isinstance(used_words, list):
            used_words = set(used_words)

        if move_data.get('skip'):
            skips = game_state.get('skips', {})
            skips[user_id] = skips.get(user_id, 0) + 1
            game_state['skips'] = skips
            game_state['current_turn_idx'] = game_state.get('current_turn_idx', 0) + 1
            return game_state, 0

        word = move_data['word'].lower().strip()

        # Check duplicate
        if word in used_words:
            return game_state, -2  # penalty

        # Check chain rule
        if words:
            last_letter = words[-1][-1]
            if word[0] != last_letter:
                return game_state, -1  # wrong starting letter

        words.append(word)
        used_words.add(word)
        game_state['words'] = words
        game_state['used_words'] = list(used_words)  # JSON-serializable
        game_state['current_turn_idx'] = game_state.get('current_turn_idx', 0) + 1

        score = len(word)  # longer words = more points
        return game_state, score

    def check_round_end(self, game_state):
        turn_idx = game_state.get('current_turn_idx', 0)
        players = game_state.get('players', [])
        # Round ends after each player has had a turn
        return turn_idx > 0 and turn_idx % len(players) == 0

    def check_game_end(self, game_state, current_round, total_rounds):
        # Also end if all players exceeded max skips
        skips = game_state.get('skips', {})
        max_skips = game_state.get('max_skips', 2)
        all_exhausted = all(v >= max_skips for v in skips.values()) if skips else False
        return current_round > total_rounds or all_exhausted

    def calculate_results(self, game_state, participants):
        scores = {p.user_id: p.score for p in participants}
        max_score = max(scores.values()) if scores else 0
        results = {}
        for uid, score in scores.items():
            if score == max_score and max_score > 0:
                results[uid] = {'result': 'win', 'score': score}
            elif score == max_score:
                results[uid] = {'result': 'draw', 'score': score}
            else:
                results[uid] = {'result': 'loss', 'score': score}
        return results


class CollabPuzzleGame(BaseGameType):
    """Cooperative: players collectively arrange ideas into a thought experiment.
    Everyone wins together — no competition."""

    # Pre-seeded puzzle fragments
    PUZZLES = [
        {
            'theme': 'community',
            'fragments': [
                'If every neighborhood had a shared AI assistant',
                'that learned from local traditions and needs',
                'it could help coordinate resources during emergencies',
                'while preserving cultural identity and privacy',
                'leading to stronger bonds between neighbors',
            ],
        },
        {
            'theme': 'environment',
            'fragments': [
                'If idle computing power from millions of devices',
                'was donated to climate modeling simulations',
                'scientists could predict weather patterns more accurately',
                'helping farmers optimize crop yields',
                'reducing food waste by 30% globally',
            ],
        },
        {
            'theme': 'education',
            'fragments': [
                'If students could create AI tutors trained on their own learning style',
                'these personal agents would adapt to individual pace',
                'teachers would be freed to focus on emotional support',
                'every child would have access to world-class instruction',
                'closing the education gap between urban and rural areas',
            ],
        },
    ]

    def initialize(self, config, total_rounds, player_ids):
        puzzle = random.choice(self.PUZZLES)
        fragments = list(puzzle['fragments'])
        random.shuffle(fragments)
        return {
            'theme': puzzle['theme'],
            'fragments': fragments,
            'correct_order': puzzle['fragments'],
            'submitted_order': [],
            'contributions': {},  # {user_id: count}
            'players': player_ids,
            'moves_made': 0,
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'fragment_index' not in move_data or 'position' not in move_data:
            return False, "Need 'fragment_index' and 'position'"
        idx = move_data['fragment_index']
        fragments = game_state.get('fragments', [])
        if idx < 0 or idx >= len(fragments):
            return False, "Invalid fragment index"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        fragments = game_state.get('fragments', [])
        submitted = game_state.get('submitted_order', [])
        idx = move_data['fragment_index']
        position = move_data['position']

        fragment = fragments[idx]
        # Insert at position (or append)
        if position >= len(submitted):
            submitted.append(fragment)
        else:
            submitted.insert(position, fragment)

        game_state['submitted_order'] = submitted
        game_state['moves_made'] = game_state.get('moves_made', 0) + 1

        # Track who contributed
        contributions = game_state.get('contributions', {})
        contributions[user_id] = contributions.get(user_id, 0) + 1
        game_state['contributions'] = contributions

        # Score: +5 for each fragment in correct position
        score_delta = 0
        correct = game_state.get('correct_order', [])
        if position < len(correct) and fragment == correct[position]:
            score_delta = 5

        return game_state, score_delta

    def check_round_end(self, game_state):
        submitted = game_state.get('submitted_order', [])
        correct = game_state.get('correct_order', [])
        return len(submitted) >= len(correct)

    def check_game_end(self, game_state, current_round, total_rounds):
        return self.check_round_end(game_state)

    def calculate_results(self, game_state, participants):
        # Cooperative: everyone wins if puzzle is assembled
        submitted = game_state.get('submitted_order', [])
        correct = game_state.get('correct_order', [])
        # Calculate similarity
        matches = sum(1 for s, c in zip(submitted, correct) if s == c)
        total = len(correct)
        accuracy = matches / total if total > 0 else 0

        result = 'win' if accuracy >= 0.6 else 'draw'
        return {p.user_id: {'result': result, 'accuracy': accuracy}
                for p in participants}


class ComputeChallengeGame(BaseGameType):
    """Race to complete micro-compute tasks from the distributed coordinator.
    Bridge to compute lending — shows users what distributed compute does."""

    def initialize(self, config, total_rounds, player_ids):
        return {
            'task_completions': {uid: 0 for uid in player_ids},
            'tasks_verified': {uid: 0 for uid in player_ids},
            'target_tasks': config.get('target_tasks', 5),
            'players': player_ids,
            'moves_made': 0,
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'task_result' not in move_data:
            return False, "Missing 'task_result'"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        completions = game_state.get('task_completions', {})
        completions[user_id] = completions.get(user_id, 0) + 1
        game_state['task_completions'] = completions
        game_state['moves_made'] = game_state.get('moves_made', 0) + 1

        # If task was verified, extra points
        verified = game_state.get('tasks_verified', {})
        if move_data.get('verified'):
            verified[user_id] = verified.get(user_id, 0) + 1
            game_state['tasks_verified'] = verified
            return game_state, 15  # verified task = more points
        return game_state, 10

    def check_round_end(self, game_state):
        # No distinct rounds — continuous play
        return False

    def check_game_end(self, game_state, current_round, total_rounds):
        target = game_state.get('target_tasks', 5)
        completions = game_state.get('task_completions', {})
        # End when any player hits the target
        return any(v >= target for v in completions.values())

    def calculate_results(self, game_state, participants):
        completions = game_state.get('task_completions', {})
        max_completions = max(completions.values()) if completions else 0
        results = {}
        for p in participants:
            count = completions.get(p.user_id, 0)
            if count == max_completions and max_completions > 0:
                results[p.user_id] = {'result': 'win', 'tasks_completed': count}
            else:
                results[p.user_id] = {'result': 'loss', 'tasks_completed': count}
        return results


# ─── Registry ───

_GAME_TYPES = {
    'trivia': TriviaGame(),
    'word_chain': WordChainGame(),
    'collab_puzzle': CollabPuzzleGame(),
    'compute_challenge': ComputeChallengeGame(),
    'quick_match': TriviaGame(),  # quick_match defaults to trivia
}


def _ensure_extended_types():
    """Lazy-register extended game type engines."""
    if 'opentdb_trivia' in _GAME_TYPES:
        return  # already registered
    try:
        from .game_types_extended import (
            OpenTDBTriviaGame, BoardGameType, PhaserGameType,
            WordScrambleGame, WordSearchGame, SudokuGame,
        )
        _GAME_TYPES.update({
            'opentdb_trivia': OpenTDBTriviaGame(),
            'boardgame': BoardGameType(),
            'phaser': PhaserGameType(),
            'word_scramble': WordScrambleGame(),
            'word_search': WordSearchGame(),
            'sudoku': SudokuGame(),
        })
        logger.info("Extended game types registered: %d total engines",
                     len(_GAME_TYPES))
    except ImportError as e:
        logger.warning("Could not load extended game types: %s", e)


def get_game_type(game_type: str) -> BaseGameType:
    """Get handler for a game type (engine name or catalog ID)."""
    _ensure_extended_types()

    # Direct engine match
    handler = _GAME_TYPES.get(game_type)
    if handler:
        return handler

    # Try resolving via catalog (catalog ID → engine name)
    try:
        from .game_catalog import get_engine_for_catalog_entry
        engine = get_engine_for_catalog_entry(game_type)
        if engine:
            handler = _GAME_TYPES.get(engine)
            if handler:
                return handler
    except ImportError:
        pass

    raise ValueError(f"Unknown game type: {game_type}")


def is_valid_game_type(game_type: str) -> bool:
    """Check if a game type or catalog ID is valid."""
    _ensure_extended_types()
    if game_type in _GAME_TYPES:
        return True
    try:
        from .game_catalog import get_catalog_entry
        return get_catalog_entry(game_type) is not None
    except ImportError:
        return False
