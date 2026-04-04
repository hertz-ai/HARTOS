"""
Creative Content Agent Tools — chain providers for movies, games, stories.

These tools compose the provider gateway's atomic operations (text gen,
image gen, video gen, audio gen) into creative pipelines:

  story_director    — Generate a full story with scenes, dialogue, visuals
  movie_maker       — Text → storyboard → images → video → music → movie
  game_asset_creator — Generate game assets (sprites, backgrounds, items, audio)
  personalized_content — Understand user preferences → custom content

Each tool uses the gateway's smart routing, so it automatically picks
the cheapest/fastest provider for each step in the pipeline.

Revenue tracking: every gateway call records cost. The revenue analytics
dashboard shows cost-per-creation vs value generated.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _gateway():
    """Lazy import to avoid circular deps."""
    from integrations.providers.gateway import get_gateway
    return get_gateway()


def _generate(prompt, model_type='llm', **kwargs):
    """Shorthand for gateway.generate with error handling."""
    result = _gateway().generate(prompt, model_type=model_type, **kwargs)
    if result.success:
        return result.content
    return f'[Generation failed: {result.error}]'


# ═══════════════════════════════════════════════════════════════════════
# Story Director
# ═══════════════════════════════════════════════════════════════════════

def story_director(query: str) -> str:
    """Create a complete story with scenes, characters, dialogue, and visual descriptions.

    Pipeline: user concept → plot outline → scene breakdown → dialogue + visual prompts.
    Input: story concept/idea (e.g. "A detective cat solving mysteries in Tokyo").
    Returns: structured story with scenes ready for movie_maker or game_asset_creator.
    """
    # Step 1: Generate plot outline
    outline = _generate(
        f"""You are a professional screenwriter. Create a compelling story outline from this concept:

CONCEPT: {query}

Return a JSON object with:
{{
  "title": "Story title",
  "genre": "genre",
  "logline": "One sentence pitch",
  "characters": [
    {{"name": "...", "role": "protagonist/antagonist/supporting", "visual": "physical description for image generation"}}
  ],
  "scenes": [
    {{
      "number": 1,
      "location": "setting description",
      "action": "what happens",
      "dialogue": "key dialogue lines",
      "visual_prompt": "detailed prompt for generating this scene as an image",
      "mood": "emotional tone",
      "duration_seconds": 10
    }}
  ]
}}

Create 4-6 scenes. Make visual_prompts detailed enough for AI image generation.""",
        system_prompt="You are a screenwriter who outputs valid JSON only.",
        max_tokens=2000,
        temperature=0.8,
    )

    # Step 2: Parse and enrich
    try:
        story = json.loads(outline)
        # Add metadata
        story['created_at'] = time.time()
        story['concept'] = query
        story['total_scenes'] = len(story.get('scenes', []))
        story['estimated_duration'] = sum(
            s.get('duration_seconds', 10) for s in story.get('scenes', []))

        return json.dumps(story, indent=2)
    except json.JSONDecodeError:
        # LLM didn't return valid JSON — return raw text
        return f"Story outline (raw):\n\n{outline}"


# ═══════════════════════════════════════════════════════════════════════
# Movie Maker
# ═══════════════════════════════════════════════════════════════════════

def movie_maker(query: str) -> str:
    """Create a short movie from a concept: script → images → video → music.

    Pipeline:
    1. story_director generates the script with visual prompts
    2. Generate key frame images for each scene
    3. Generate video clips from images (img2vid) or text (txt2vid)
    4. Generate background music
    5. Return all assets with assembly instructions

    Input: movie concept (e.g. "30-second ad for a space tourism company").
    Returns: JSON with all generated asset URLs and timeline.
    """
    # Step 1: Generate script
    script_json = story_director(query)
    try:
        script = json.loads(script_json)
    except json.JSONDecodeError:
        return f"Failed to generate script. Raw output:\n{script_json[:500]}"

    scenes = script.get('scenes', [])
    if not scenes:
        return "No scenes generated. Try a more specific concept."

    assets = {
        'title': script.get('title', 'Untitled'),
        'script': script,
        'images': [],
        'videos': [],
        'music': None,
        'timeline': [],
        'total_cost_usd': 0.0,
    }

    # Step 2: Generate key frame images for each scene
    for i, scene in enumerate(scenes[:6]):  # Max 6 scenes
        prompt = scene.get('visual_prompt', scene.get('action', ''))
        if not prompt:
            continue

        image_url = _generate(
            f"Cinematic, high quality, film still: {prompt}. "
            f"Mood: {scene.get('mood', 'dramatic')}. "
            f"Style: photorealistic, 16:9 aspect ratio, movie lighting.",
            model_type='image_gen',
            strategy='balanced',
        )
        assets['images'].append({
            'scene': i + 1,
            'url': image_url,
            'prompt': prompt,
        })

    # Step 3: Generate video for key scenes (first and climax)
    key_scenes = [scenes[0]]
    if len(scenes) > 2:
        key_scenes.append(scenes[len(scenes) // 2])  # Mid-point

    for scene in key_scenes:
        prompt = scene.get('visual_prompt', '')
        if not prompt:
            continue
        video_url = _generate(
            f"Cinematic video: {prompt}. Smooth camera movement, "
            f"mood: {scene.get('mood', 'dramatic')}",
            model_type='video_gen',
            strategy='balanced',
        )
        assets['videos'].append({
            'scene': scene.get('number', 0),
            'url': video_url,
            'duration': scene.get('duration_seconds', 10),
        })

    # Step 4: Generate background music
    genre = script.get('genre', 'cinematic')
    music_url = _generate(
        f"Background music for a {genre} short film. "
        f"Title: {script.get('title', '')}. Mood: atmospheric, emotional. "
        f"Duration: 30 seconds. Instrumental only.",
        model_type='audio_gen',
        strategy='balanced',
    )
    assets['music'] = {'url': music_url, 'genre': genre}

    # Step 5: Build timeline
    offset = 0
    for i, scene in enumerate(scenes):
        dur = scene.get('duration_seconds', 10)
        assets['timeline'].append({
            'scene': i + 1,
            'start_s': offset,
            'end_s': offset + dur,
            'image': assets['images'][i]['url'] if i < len(assets['images']) else None,
            'video': next((v['url'] for v in assets['videos']
                           if v['scene'] == i + 1), None),
            'dialogue': scene.get('dialogue', ''),
        })
        offset += dur

    # Cost tracking
    stats = _gateway().get_stats()
    assets['total_cost_usd'] = stats.get('total_cost_usd', 0)

    return json.dumps(assets, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# Game Asset Creator
# ═══════════════════════════════════════════════════════════════════════

def game_asset_creator(query: str) -> str:
    """Generate game assets from a concept: characters, backgrounds, items, audio.

    Input: game concept (e.g. "fantasy RPG with dragon riders").
    Returns: JSON with generated asset URLs organized by category.
    """
    # Step 1: Generate asset list
    asset_plan = _generate(
        f"""You are a game artist director. For this game concept, list the assets needed:

CONCEPT: {query}

Return JSON:
{{
  "game_title": "...",
  "style": "pixel art / 2D cartoon / 3D realistic / anime",
  "characters": [
    {{"name": "...", "role": "player/enemy/npc", "visual_prompt": "detailed visual description for AI image gen"}}
  ],
  "backgrounds": [
    {{"name": "...", "visual_prompt": "detailed scene description"}}
  ],
  "items": [
    {{"name": "...", "type": "weapon/potion/key/treasure", "visual_prompt": "..."}}
  ],
  "audio_needs": ["background music genre", "sound effect descriptions"]
}}

Keep it to 3-4 items per category. Make visual_prompts specific enough for AI generation.""",
        system_prompt="Output valid JSON only.",
        max_tokens=1500,
        temperature=0.7,
    )

    try:
        plan = json.loads(asset_plan)
    except json.JSONDecodeError:
        return f"Failed to plan assets. Raw:\n{asset_plan[:500]}"

    style = plan.get('style', '2D cartoon')
    result = {
        'game_title': plan.get('game_title', 'Untitled'),
        'style': style,
        'characters': [],
        'backgrounds': [],
        'items': [],
        'audio': [],
    }

    # Step 2: Generate character sprites
    for char in plan.get('characters', [])[:4]:
        url = _generate(
            f"Game character sprite, {style} style: {char['visual_prompt']}. "
            f"Full body, transparent background, game-ready.",
            model_type='image_gen',
        )
        result['characters'].append({
            'name': char['name'], 'role': char.get('role', ''),
            'url': url,
        })

    # Step 3: Generate backgrounds
    for bg in plan.get('backgrounds', [])[:3]:
        url = _generate(
            f"Game background, {style} style: {bg['visual_prompt']}. "
            f"Wide shot, 16:9, detailed environment.",
            model_type='image_gen',
        )
        result['backgrounds'].append({
            'name': bg['name'], 'url': url,
        })

    # Step 4: Generate items
    for item in plan.get('items', [])[:4]:
        url = _generate(
            f"Game item icon, {style} style: {item['visual_prompt']}. "
            f"Centered, transparent background, detailed.",
            model_type='image_gen',
        )
        result['items'].append({
            'name': item['name'], 'type': item.get('type', ''),
            'url': url,
        })

    # Step 5: Generate audio
    for audio_desc in plan.get('audio_needs', [])[:2]:
        url = _generate(
            f"Game audio: {audio_desc}. High quality, loopable.",
            model_type='audio_gen',
        )
        result['audio'].append({
            'description': audio_desc, 'url': url,
        })

    return json.dumps(result, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# LangChain Tool Registration
# ═══════════════════════════════════════════════════════════════════════

def get_creative_tools():
    """Return LangChain tools for creative content generation."""
    tools = []
    try:
        from langchain.tools import Tool
    except ImportError:
        try:
            from langchain_core.tools import Tool
        except ImportError:
            return []

    tools.extend([
        Tool(
            name='Story_Director',
            func=story_director,
            description=(
                'Create a complete story with scenes, characters, dialogue, and visual descriptions. '
                'Input: story concept. Output: structured JSON story ready for movie or game production.'
            ),
        ),
        Tool(
            name='Movie_Maker',
            func=movie_maker,
            description=(
                'Create a short movie from a concept: generates script, images, video clips, '
                'and music. Returns all asset URLs with a timeline for assembly. '
                'Input: movie concept (e.g. "30-second ad for a coffee brand").'
            ),
        ),
        Tool(
            name='Game_Asset_Creator',
            func=game_asset_creator,
            description=(
                'Generate game assets from a concept: character sprites, backgrounds, items, '
                'and audio. Returns organized asset URLs. '
                'Input: game concept (e.g. "fantasy RPG with dragon riders").'
            ),
        ),
    ])
    return tools
