"""
HevolveSocial - Extended Game Type Strategies
New engine classes that power 100+ games via config-driven variants.
Each follows the BaseGameType interface from game_types.py.
"""
import logging
import random
import string
import json
from datetime import datetime
from typing import Dict, List, Tuple, Optional

from core.http_pool import pooled_get

from .game_types import BaseGameType

logger = logging.getLogger('hevolve_social')


# ─── OpenTDB Trivia Engine ─────────────────────────────────────────
# 24 categories x 3 modes = 72+ trivia variants from 1 class

class OpenTDBTriviaGame(BaseGameType):
    """Trivia powered by Open Trivia Database API.

    engine_config keys:
        opentdb_category_id: int (9-32, see opentdb.com/api_config.php)
        mode: 'classic' | 'speed' | 'survival'
        time_per_question: int (seconds)
        difficulty: 'easy' | 'medium' | 'hard' | None (mixed)
        question_count: int (override total_rounds)
    """

    OPENTDB_URL = 'https://opentdb.com/api.php'

    # Fallback questions if API is unreachable
    FALLBACK_QUESTIONS = [
        {'q': 'What is the chemical symbol for water?', 'a': 'H2O',
         'options': ['H2O', 'CO2', 'NaCl', 'O2']},
        {'q': 'Which planet is closest to the Sun?', 'a': 'Mercury',
         'options': ['Venus', 'Mercury', 'Mars', 'Earth']},
        {'q': 'What year did World War II end?', 'a': '1945',
         'options': ['1943', '1945', '1947', '1944']},
        {'q': 'Who painted the Mona Lisa?', 'a': 'Leonardo da Vinci',
         'options': ['Michelangelo', 'Leonardo da Vinci', 'Raphael', 'Donatello']},
        {'q': 'What is the largest mammal?', 'a': 'Blue whale',
         'options': ['Elephant', 'Blue whale', 'Giraffe', 'Hippopotamus']},
        {'q': 'How many continents are there?', 'a': '7',
         'options': ['5', '6', '7', '8']},
        {'q': 'What gas do humans exhale?', 'a': 'Carbon dioxide',
         'options': ['Oxygen', 'Nitrogen', 'Carbon dioxide', 'Helium']},
        {'q': 'Which element has the atomic number 1?', 'a': 'Hydrogen',
         'options': ['Helium', 'Hydrogen', 'Lithium', 'Carbon']},
        {'q': 'What is the capital of Japan?', 'a': 'Tokyo',
         'options': ['Osaka', 'Kyoto', 'Tokyo', 'Hiroshima']},
        {'q': 'How many strings does a standard guitar have?', 'a': '6',
         'options': ['4', '5', '6', '8']},
    ]

    def _fetch_questions(self, config, count):
        """Fetch questions from OpenTDB API, fallback to built-in."""
        cat_id = config.get('opentdb_category_id')
        difficulty = config.get('difficulty')

        params = {'amount': min(count, 50), 'type': 'multiple'}
        if cat_id:
            params['category'] = cat_id
        if difficulty and difficulty in ('easy', 'medium', 'hard'):
            params['difficulty'] = difficulty

        try:
            resp = pooled_get(self.OPENTDB_URL, params=params, timeout=5)
            data = resp.json()
            if data.get('response_code') == 0 and data.get('results'):
                questions = []
                for r in data['results']:
                    import html
                    q_text = html.unescape(r['question'])
                    correct = html.unescape(r['correct_answer'])
                    incorrect = [html.unescape(a) for a in r['incorrect_answers']]
                    options = incorrect + [correct]
                    random.shuffle(options)
                    questions.append({
                        'q': q_text,
                        'a': correct,
                        'options': options,
                        'difficulty': r.get('difficulty', 'medium'),
                    })
                return questions
        except Exception as e:
            logger.warning("OpenTDB fetch failed: %s — using fallback", e)

        # Fallback
        fallback = list(self.FALLBACK_QUESTIONS)
        random.shuffle(fallback)
        return fallback[:count]

    def initialize(self, config, total_rounds, player_ids):
        mode = config.get('mode', 'classic')
        time_per_q = config.get('time_per_question', 15 if mode == 'classic' else 10)
        q_count = config.get('question_count', total_rounds)

        questions = self._fetch_questions(config, q_count)

        state = {
            'questions': questions,
            'current_question_idx': 0,
            'answers': {},
            'round_scores': {uid: [] for uid in player_ids},
            'round_answered': False,
            'players': player_ids,
            'mode': mode,
            'time_per_question': time_per_q,
            'eliminated': [],  # for survival mode
            'first_correct': {},  # {round_idx: user_id} for speed mode
        }
        return state

    def validate_move(self, game_state, user_id, move_data):
        if 'answer' not in move_data:
            return False, "Missing 'answer'"
        if user_id in game_state.get('eliminated', []):
            return False, "You have been eliminated"
        idx = game_state.get('current_question_idx', 0)
        round_answers = game_state.get('answers', {}).get(str(idx), {})
        if user_id in round_answers:
            return False, "Already answered this round"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        idx = game_state.get('current_question_idx', 0)
        questions = game_state.get('questions', [])
        if idx >= len(questions):
            return game_state, 0

        answer = str(move_data['answer']).strip()
        correct_answer = questions[idx]['a'].strip()
        is_correct = answer.lower() == correct_answer.lower()

        answers = game_state.get('answers', {})
        round_answers = answers.get(str(idx), {})
        round_answers[user_id] = {'answer': answer, 'correct': is_correct,
                                   'time_ms': move_data.get('time_ms', 0)}
        answers[str(idx)] = round_answers
        game_state['answers'] = answers

        mode = game_state.get('mode', 'classic')
        score_delta = 0

        if mode == 'speed':
            # First correct answer gets max points, subsequent get less
            if is_correct:
                first_correct = game_state.get('first_correct', {})
                if str(idx) not in first_correct:
                    first_correct[str(idx)] = user_id
                    game_state['first_correct'] = first_correct
                    score_delta = 15  # first correct = bonus
                else:
                    score_delta = 8  # also correct but not first
            else:
                score_delta = 0

        elif mode == 'survival':
            if is_correct:
                score_delta = 10
            else:
                # Wrong answer = eliminated
                eliminated = game_state.get('eliminated', [])
                eliminated.append(user_id)
                game_state['eliminated'] = eliminated
                score_delta = 0

        else:  # classic
            if is_correct:
                # Time bonus: faster = more points
                time_ms = move_data.get('time_ms', 15000)
                time_per_q = game_state.get('time_per_question', 15) * 1000
                time_bonus = max(0, int(5 * (1 - time_ms / time_per_q)))
                score_delta = 10 + time_bonus

        return game_state, score_delta

    def check_round_end(self, game_state):
        idx = game_state.get('current_question_idx', 0)
        answers = game_state.get('answers', {})
        round_answers = answers.get(str(idx), {})
        players = game_state.get('players', [])
        eliminated = game_state.get('eliminated', [])
        active_players = [p for p in players if p not in eliminated]

        if len(round_answers) >= len(active_players):
            game_state['current_question_idx'] = idx + 1
            game_state['round_answered'] = False
            return True
        return False

    def check_game_end(self, game_state, current_round, total_rounds):
        idx = game_state.get('current_question_idx', 0)
        questions = game_state.get('questions', [])
        mode = game_state.get('mode', 'classic')

        if idx >= len(questions):
            return True
        if current_round > total_rounds:
            return True

        # Survival: end when 0 or 1 player left
        if mode == 'survival':
            eliminated = game_state.get('eliminated', [])
            players = game_state.get('players', [])
            alive = [p for p in players if p not in eliminated]
            if len(alive) <= 1:
                return True

        return False

    def calculate_results(self, game_state, participants):
        scores = {p.user_id: p.score for p in participants}
        mode = game_state.get('mode', 'classic')
        eliminated = game_state.get('eliminated', [])

        if mode == 'survival':
            players = game_state.get('players', [])
            alive = [p for p in players if p not in eliminated]
            results = {}
            for p in participants:
                if p.user_id in alive and len(alive) <= 1:
                    results[p.user_id] = {'result': 'win', 'score': p.score,
                                          'survived': True}
                elif p.user_id in alive:
                    results[p.user_id] = {'result': 'draw', 'score': p.score,
                                          'survived': True}
                else:
                    results[p.user_id] = {'result': 'loss', 'score': p.score,
                                          'survived': False}
            return results

        # Classic / Speed: highest score wins
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


# ─── Board Game Engine ──────────────────────────────────────────────
# Frontend (boardgame.io or custom) manages board state.
# Backend validates turns and tracks scores.

class BoardGameType(BaseGameType):
    """Generic turn-based board game engine.

    Frontend manages the visual board state and sends validated moves.
    Backend tracks turns, scores, and game completion.

    engine_config keys:
        board_type: 'tictactoe' | 'connect4' | 'checkers' | 'reversi' |
                    'mancala' | 'dots_and_boxes' | 'battleship' | 'nim'
        board_size: int (e.g., 3 for 3x3 tic-tac-toe)
        time_limit_per_turn: int (seconds, 0 = no limit)
    """

    def initialize(self, config, total_rounds, player_ids):
        board_type = config.get('board_type', 'tictactoe')
        board_size = config.get('board_size', 3)

        return {
            'board_type': board_type,
            'board_size': board_size,
            'board_state': {},  # frontend-managed state synced via moves
            'turn_order': player_ids,
            'current_turn_idx': 0,
            'moves_history': [],
            'players': player_ids,
            'winner': None,
            'draw': False,
        }

    def validate_move(self, game_state, user_id, move_data):
        turn_order = game_state.get('turn_order', [])
        current_idx = game_state.get('current_turn_idx', 0)
        if not turn_order:
            return False, "No players"
        expected = turn_order[current_idx % len(turn_order)]
        if user_id != expected:
            return False, "Not your turn"
        if 'action' not in move_data:
            return False, "Missing 'action'"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        moves = game_state.get('moves_history', [])
        moves.append({
            'user_id': user_id,
            'action': move_data['action'],
            'position': move_data.get('position'),
            'timestamp': datetime.utcnow().isoformat(),
        })
        game_state['moves_history'] = moves
        game_state['current_turn_idx'] = game_state.get('current_turn_idx', 0) + 1

        # Frontend sends board_state and game result with each move
        if 'board_state' in move_data:
            game_state['board_state'] = move_data['board_state']

        score_delta = move_data.get('score_delta', 0)

        if move_data.get('game_won'):
            game_state['winner'] = user_id
        elif move_data.get('game_draw'):
            game_state['draw'] = True

        return game_state, score_delta

    def check_round_end(self, game_state):
        return game_state.get('winner') is not None or game_state.get('draw', False)

    def check_game_end(self, game_state, current_round, total_rounds):
        return game_state.get('winner') is not None or game_state.get('draw', False)

    def calculate_results(self, game_state, participants):
        winner = game_state.get('winner')
        is_draw = game_state.get('draw', False)
        results = {}
        for p in participants:
            if is_draw:
                results[p.user_id] = {'result': 'draw', 'score': p.score}
            elif p.user_id == winner:
                results[p.user_id] = {'result': 'win', 'score': p.score}
            else:
                results[p.user_id] = {'result': 'loss', 'score': p.score}
        return results


# ─── Phaser Arcade Engine ───────────────────────────────────────────
# Frontend runs Phaser scenes. Backend tracks high scores and race results.

class PhaserGameType(BaseGameType):
    """Score-based arcade games rendered by Phaser on frontend.

    The actual game logic runs in the Phaser scene. Backend tracks:
    - Score submissions (periodic + final)
    - Lives/elimination for multiplayer
    - Game completion

    engine_config keys:
        scene_id: 'snake' | 'breakout' | 'bubble_shooter' | 'pong' |
                  'runner' | 'match3' | 'flappy' | 'block_stack' |
                  'space_invaders' | '2048'
        target_score: int (0 = play until lives exhausted)
        max_duration_seconds: int (time limit, 0 = unlimited)
    """

    def initialize(self, config, total_rounds, player_ids):
        return {
            'scene_id': config.get('scene_id', 'snake'),
            'scores': {uid: 0 for uid in player_ids},
            'lives': {uid: config.get('starting_lives', 3) for uid in player_ids},
            'finished': {uid: False for uid in player_ids},
            'target_score': config.get('target_score', 0),
            'max_duration': config.get('max_duration_seconds', 0),
            'players': player_ids,
            'start_time': datetime.utcnow().isoformat(),
        }

    def validate_move(self, game_state, user_id, move_data):
        if user_id not in game_state.get('players', []):
            return False, "Not in this game"
        if game_state.get('finished', {}).get(user_id, False):
            return False, "Already finished"
        action = move_data.get('action')
        if action not in ('score_update', 'life_lost', 'game_over', 'game_complete'):
            return False, "Invalid action type"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        action = move_data['action']
        score_delta = 0

        if action == 'score_update':
            new_score = move_data.get('score', 0)
            old_score = game_state['scores'].get(user_id, 0)
            game_state['scores'][user_id] = new_score
            score_delta = new_score - old_score

        elif action == 'life_lost':
            lives = game_state.get('lives', {})
            lives[user_id] = max(0, lives.get(user_id, 0) - 1)
            game_state['lives'] = lives
            if lives[user_id] <= 0:
                game_state['finished'][user_id] = True

        elif action in ('game_over', 'game_complete'):
            final_score = move_data.get('score', game_state['scores'].get(user_id, 0))
            old_score = game_state['scores'].get(user_id, 0)
            game_state['scores'][user_id] = final_score
            game_state['finished'][user_id] = True
            score_delta = final_score - old_score

        return game_state, score_delta

    def check_round_end(self, game_state):
        # Arcade games have no rounds — continuous play
        finished = game_state.get('finished', {})
        return all(finished.values())

    def check_game_end(self, game_state, current_round, total_rounds):
        finished = game_state.get('finished', {})
        if all(finished.values()):
            return True
        # Check target score
        target = game_state.get('target_score', 0)
        if target > 0:
            for uid, score in game_state.get('scores', {}).items():
                if score >= target:
                    return True
        return False

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


# ─── Word Scramble Engine ───────────────────────────────────────────

class WordScrambleGame(BaseGameType):
    """Scrambled letters — players race to form valid words.

    engine_config keys:
        word_length: int (4-8)
        round_time: int (seconds per round)
        language: 'en'
    """

    WORD_LISTS = {
        4: ['time', 'work', 'life', 'game', 'hand', 'play', 'look', 'help',
            'love', 'book', 'star', 'fire', 'wind', 'rain', 'moon', 'fish',
            'tree', 'bird', 'cake', 'ship'],
        5: ['water', 'power', 'light', 'world', 'sound', 'place', 'house',
            'music', 'earth', 'brain', 'dream', 'heart', 'stone', 'cloud',
            'flame', 'ocean', 'storm', 'peace', 'dance', 'magic'],
        6: ['nature', 'garden', 'island', 'bridge', 'castle', 'forest',
            'planet', 'spirit', 'temple', 'galaxy', 'rhythm', 'stream',
            'dragon', 'sunset', 'breeze', 'winter', 'summer', 'autumn',
            'spring', 'shadow'],
        7: ['journey', 'freedom', 'mystery', 'silence', 'balance', 'crystal',
            'kingdom', 'harmony', 'phoenix', 'thunder', 'whisper', 'blossom',
            'sunrise', 'horizon', 'rainbow', 'destiny', 'courage', 'miracle',
            'library', 'compass'],
        8: ['treasure', 'mountain', 'starship', 'firework', 'notebook',
            'learning', 'wildfire', 'discover', 'champion', 'laughter',
            'universe', 'keyboard', 'sandwich', 'engineer', 'painting',
            'dinosaur', 'calendar', 'spectrum', 'midnight', 'wireless'],
    }

    def _scramble(self, word):
        letters = list(word)
        for _ in range(10):
            random.shuffle(letters)
            if ''.join(letters) != word:
                break
        return ''.join(letters)

    def initialize(self, config, total_rounds, player_ids):
        word_length = config.get('word_length', 5)
        words = list(self.WORD_LISTS.get(word_length, self.WORD_LISTS[5]))
        random.shuffle(words)
        selected = words[:total_rounds]

        rounds = []
        for w in selected:
            rounds.append({
                'word': w,
                'scrambled': self._scramble(w),
                'solved_by': None,
            })

        return {
            'rounds': rounds,
            'current_round_idx': 0,
            'round_time': config.get('round_time', 30),
            'word_length': word_length,
            'players': player_ids,
            'scores_breakdown': {uid: [] for uid in player_ids},
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'word' not in move_data:
            return False, "Missing 'word'"
        idx = game_state.get('current_round_idx', 0)
        rounds = game_state.get('rounds', [])
        if idx >= len(rounds):
            return False, "No more rounds"
        if rounds[idx].get('solved_by'):
            return False, "Round already solved"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        idx = game_state.get('current_round_idx', 0)
        rounds = game_state.get('rounds', [])
        if idx >= len(rounds):
            return game_state, 0

        guess = move_data['word'].lower().strip()
        correct_word = rounds[idx]['word'].lower()

        if guess == correct_word:
            rounds[idx]['solved_by'] = user_id
            game_state['rounds'] = rounds
            time_ms = move_data.get('time_ms', 30000)
            time_bonus = max(0, int(10 * (1 - time_ms / 30000)))
            score_delta = 15 + time_bonus
            return game_state, score_delta
        else:
            return game_state, -2  # wrong guess penalty

    def check_round_end(self, game_state):
        idx = game_state.get('current_round_idx', 0)
        rounds = game_state.get('rounds', [])
        if idx < len(rounds) and rounds[idx].get('solved_by'):
            game_state['current_round_idx'] = idx + 1
            return True
        return False

    def check_game_end(self, game_state, current_round, total_rounds):
        idx = game_state.get('current_round_idx', 0)
        rounds = game_state.get('rounds', [])
        return idx >= len(rounds) or current_round > total_rounds

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


# ─── Word Search Engine ─────────────────────────────────────────────

class WordSearchGame(BaseGameType):
    """Grid with hidden words. Players find words by selecting start/end cells.

    engine_config keys:
        grid_size: int (10-15)
        word_count: int (5-15)
        theme: str (category for words)
    """

    THEMED_WORDS = {
        'animals': ['tiger', 'eagle', 'shark', 'whale', 'snake', 'horse',
                     'zebra', 'panda', 'koala', 'otter', 'raven', 'crane',
                     'mouse', 'bison', 'camel'],
        'space': ['stars', 'orbit', 'comet', 'venus', 'lunar', 'solar',
                  'pluto', 'earth', 'rover', 'mars', 'rings', 'dwarf',
                  'nova', 'black', 'light'],
        'food': ['pizza', 'pasta', 'sushi', 'bread', 'salad', 'curry',
                 'steak', 'cream', 'fruit', 'grape', 'mango', 'lemon',
                 'olive', 'spice', 'sauce'],
        'tech': ['cloud', 'pixel', 'debug', 'cache', 'query', 'stack',
                 'array', 'loops', 'parse', 'token', 'nodes', 'linux',
                 'https', 'swift', 'react'],
    }

    def _generate_grid(self, grid_size, words):
        """Generate a word search grid with hidden words."""
        grid = [[' ' for _ in range(grid_size)] for _ in range(grid_size)]
        placed_words = []
        directions = [(0, 1), (1, 0), (1, 1), (0, -1), (-1, 0), (-1, -1), (1, -1), (-1, 1)]

        for word in words:
            word_upper = word.upper()
            placed = False
            for _ in range(100):  # max attempts
                d = random.choice(directions)
                r = random.randint(0, grid_size - 1)
                c = random.randint(0, grid_size - 1)

                # Check if word fits
                end_r = r + d[0] * (len(word_upper) - 1)
                end_c = c + d[1] * (len(word_upper) - 1)
                if end_r < 0 or end_r >= grid_size or end_c < 0 or end_c >= grid_size:
                    continue

                # Check for conflicts
                conflict = False
                for i, ch in enumerate(word_upper):
                    gr = r + d[0] * i
                    gc = c + d[1] * i
                    if grid[gr][gc] != ' ' and grid[gr][gc] != ch:
                        conflict = True
                        break
                if conflict:
                    continue

                # Place the word
                for i, ch in enumerate(word_upper):
                    gr = r + d[0] * i
                    gc = c + d[1] * i
                    grid[gr][gc] = ch
                placed_words.append({
                    'word': word,
                    'start': [r, c],
                    'end': [end_r, end_c],
                    'direction': list(d),
                })
                placed = True
                break

            if not placed:
                logger.debug("Could not place word: %s", word)

        # Fill remaining spaces with random letters
        for r in range(grid_size):
            for c in range(grid_size):
                if grid[r][c] == ' ':
                    grid[r][c] = random.choice(string.ascii_uppercase)

        return grid, placed_words

    def initialize(self, config, total_rounds, player_ids):
        grid_size = config.get('grid_size', 10)
        word_count = config.get('word_count', 8)
        theme = config.get('theme', 'animals')

        word_pool = list(self.THEMED_WORDS.get(theme, self.THEMED_WORDS['animals']))
        random.shuffle(word_pool)
        selected_words = word_pool[:word_count]

        grid, placed_words = self._generate_grid(grid_size, selected_words)

        return {
            'grid': grid,
            'grid_size': grid_size,
            'words_to_find': [w['word'] for w in placed_words],
            'word_positions': placed_words,  # hidden from client until found
            'found_words': {},  # {word: user_id}
            'players': player_ids,
            'total_words': len(placed_words),
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'word' not in move_data:
            return False, "Missing 'word'"
        word = move_data['word'].lower()
        if word in game_state.get('found_words', {}):
            return False, "Word already found"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        word = move_data['word'].lower()
        words_to_find = [w.lower() for w in game_state.get('words_to_find', [])]

        if word in words_to_find and word not in game_state.get('found_words', {}):
            found = game_state.get('found_words', {})
            found[word] = user_id
            game_state['found_words'] = found
            return game_state, 10
        return game_state, 0

    def check_round_end(self, game_state):
        found = game_state.get('found_words', {})
        total = game_state.get('total_words', 0)
        return len(found) >= total

    def check_game_end(self, game_state, current_round, total_rounds):
        return self.check_round_end(game_state)

    def calculate_results(self, game_state, participants):
        scores = {p.user_id: p.score for p in participants}
        max_score = max(scores.values()) if scores else 0
        results = {}
        for uid, score in scores.items():
            found_count = sum(1 for w, finder in game_state.get('found_words', {}).items()
                              if finder == uid)
            if score == max_score and max_score > 0:
                results[uid] = {'result': 'win', 'score': score, 'words_found': found_count}
            elif score == max_score:
                results[uid] = {'result': 'draw', 'score': score, 'words_found': found_count}
            else:
                results[uid] = {'result': 'loss', 'score': score, 'words_found': found_count}
        return results


# ─── Sudoku Engine ──────────────────────────────────────────────────

class SudokuGame(BaseGameType):
    """Sudoku puzzle — cooperative fill mode for multiplayer.

    engine_config keys:
        difficulty: 'easy' | 'medium' | 'hard'
    """

    def _generate_puzzle(self, difficulty):
        """Generate a valid sudoku puzzle with solution."""
        # Start with a known valid board and remove cells
        base = [
            [5, 3, 4, 6, 7, 8, 9, 1, 2],
            [6, 7, 2, 1, 9, 5, 3, 4, 8],
            [1, 9, 8, 3, 4, 2, 5, 6, 7],
            [8, 5, 9, 7, 6, 1, 4, 2, 3],
            [4, 2, 6, 8, 5, 3, 7, 9, 1],
            [7, 1, 3, 9, 2, 4, 8, 5, 6],
            [9, 6, 1, 5, 3, 7, 2, 8, 4],
            [2, 8, 7, 4, 1, 9, 6, 3, 5],
            [3, 4, 5, 2, 8, 6, 1, 7, 9],
        ]

        # Shuffle to create variety (row/col swaps within blocks)
        for _ in range(20):
            block = random.randint(0, 2)
            r1 = block * 3 + random.randint(0, 2)
            r2 = block * 3 + random.randint(0, 2)
            base[r1], base[r2] = base[r2], base[r1]

        for _ in range(20):
            block = random.randint(0, 2)
            c1 = block * 3 + random.randint(0, 2)
            c2 = block * 3 + random.randint(0, 2)
            for row in base:
                row[c1], row[c2] = row[c2], row[c1]

        solution = [row[:] for row in base]

        # Remove cells based on difficulty
        remove_count = {'easy': 30, 'medium': 40, 'hard': 50}.get(difficulty, 35)
        cells = [(r, c) for r in range(9) for c in range(9)]
        random.shuffle(cells)
        puzzle = [row[:] for row in base]
        for r, c in cells[:remove_count]:
            puzzle[r][c] = 0

        return puzzle, solution

    def initialize(self, config, total_rounds, player_ids):
        difficulty = config.get('difficulty', 'medium')
        puzzle, solution = self._generate_puzzle(difficulty)

        return {
            'puzzle': puzzle,       # current state (0 = empty)
            'solution': solution,   # hidden from clients
            'original': [row[:] for row in puzzle],  # to know which cells were given
            'difficulty': difficulty,
            'players': player_ids,
            'cell_owners': {},  # {"r,c": user_id}
            'mistakes': {uid: 0 for uid in player_ids},
            'max_mistakes': 3,
        }

    def validate_move(self, game_state, user_id, move_data):
        if 'row' not in move_data or 'col' not in move_data or 'value' not in move_data:
            return False, "Need 'row', 'col', 'value'"
        r, c = move_data['row'], move_data['col']
        if not (0 <= r < 9 and 0 <= c < 9):
            return False, "Invalid position"
        if game_state['original'][r][c] != 0:
            return False, "Cannot change given cell"
        v = move_data['value']
        if not (1 <= v <= 9):
            return False, "Value must be 1-9"
        return True, ""

    def apply_move(self, game_state, user_id, move_data):
        r, c, v = move_data['row'], move_data['col'], move_data['value']
        correct = game_state['solution'][r][c]

        if v == correct:
            game_state['puzzle'][r][c] = v
            cell_key = f"{r},{c}"
            owners = game_state.get('cell_owners', {})
            owners[cell_key] = user_id
            game_state['cell_owners'] = owners
            return game_state, 5
        else:
            mistakes = game_state.get('mistakes', {})
            mistakes[user_id] = mistakes.get(user_id, 0) + 1
            game_state['mistakes'] = mistakes
            return game_state, -2

    def check_round_end(self, game_state):
        # Check if puzzle is complete
        for row in game_state.get('puzzle', []):
            if 0 in row:
                return False
        return True

    def check_game_end(self, game_state, current_round, total_rounds):
        if self.check_round_end(game_state):
            return True
        # End if all players exceeded max mistakes
        mistakes = game_state.get('mistakes', {})
        max_m = game_state.get('max_mistakes', 3)
        players = game_state.get('players', [])
        if all(mistakes.get(uid, 0) >= max_m for uid in players):
            return True
        return False

    def calculate_results(self, game_state, participants):
        # Cooperative: everyone wins if puzzle solved
        complete = all(0 not in row for row in game_state.get('puzzle', []))
        results = {}
        for p in participants:
            cells_filled = sum(1 for v in game_state.get('cell_owners', {}).values()
                               if v == p.user_id)
            if complete:
                results[p.user_id] = {'result': 'win', 'score': p.score,
                                      'cells_filled': cells_filled}
            else:
                results[p.user_id] = {'result': 'draw', 'score': p.score,
                                      'cells_filled': cells_filled}
        return results
