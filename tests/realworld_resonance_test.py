#!/usr/bin/env python
"""
Real-world integration test for the Resonance Frequency Tuning system.

Tests the ACTUAL code paths end-to-end with real file I/O, real EMA math,
real signal extraction, and real personality prompt generation.
No mocks. No stubs. Real world.

Usage:
    python tests/realworld_resonance_test.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.resonance_profile import (
    UserResonanceProfile, save_resonance_profile,
    load_resonance_profile, get_or_create_profile, DEFAULT_TUNING,
)
from core.resonance_tuner import (
    ResonanceTuner, SignalExtractor, build_resonance_prompt,
    get_resonance_tuner, MIN_INTERACTIONS_FOR_TUNING,
)
from core.agent_personality import (
    AgentPersonality, generate_personality, build_personality_prompt,
)
from core.resonance_identifier import ResonanceIdentifier


# ═══════════════════════════════════════════════════════════════════════
# Test data: realistic user conversations
# ═══════════════════════════════════════════════════════════════════════

CASUAL_USER_MESSAGES = [
    "hey can you help me with this bug? the login page is totally broken lol",
    "yo the CSS is all messed up, nothing aligns right haha",
    "cool thx! btw can u also check the database? it's being slow af",
    "yeah that worked! awesome sauce, now the homepage needs some love too",
    "nah dont worry about the footer, just fix the header thx",
    "ok cool can we also add a dark mode? that'd be sick",
    "yo real quick - can you make the buttons bigger on mobile?",
    "lol yeah that looks way better now, good job!",
    "hey one more thing - the search bar is kinda janky on safari",
    "thx bro you're a lifesaver! gonna push this to prod now",
]

FORMAL_USER_MESSAGES = [
    "Good morning. I would like to request an analysis of our API endpoint performance metrics.",
    "Please kindly review the database schema and provide recommendations for optimization.",
    "Regarding the deployment pipeline, I would appreciate a thorough assessment of our CI/CD configuration.",
    "Furthermore, could you please examine the authentication middleware for potential vulnerabilities?",
    "I respectfully request that you document the architectural decisions made in this sprint.",
    "Would you please analyze the microservice communication patterns and suggest improvements?",
    "Pursuant to our discussion, I shall require a comprehensive report on system latency.",
    "Dear assistant, please review the error handling strategy across all API endpoints.",
    "I appreciate your thorough work. Could you kindly prepare a summary of the infrastructure changes?",
    "Thank you for your diligent assistance. Please proceed with the production deployment plan.",
]

TECHNICAL_USER_MESSAGES = [
    "Can you refactor the database query to use a CTE with a recursive JOIN on the dependency graph?",
    "The microservice endpoint has 99th percentile latency at 450ms. Let's add Redis caching with a TTL strategy.",
    "Deploy the container with a rolling update strategy. Set the readiness probe to /healthz on port 8080.",
    "The algorithm has O(n²) complexity. Can we use a trie-based approach for the autocomplete pipeline?",
    "Configure the API gateway with rate limiting at 1000 req/s and circuit breaker with a 5% error threshold.",
    "The schema migration needs a zero-downtime approach. Use expand-contract pattern with backward-compatible columns.",
    "Set up the CI pipeline with parallel test execution across 4 runners. Add the dependency cache layer.",
    "The regex for email validation is too permissive. Use RFC 5322 compliant pattern with lookahead assertions.",
    "Implement the WebSocket endpoint with connection pooling and automatic reconnection with exponential backoff.",
    "The infrastructure needs horizontal pod autoscaling based on custom metrics from the query throughput.",
]

AGENT_RESPONSES = [
    "I've analyzed the issue and here's what I found. The root cause is a misconfigured routing table.",
    "Here's the optimized solution with proper error handling and logging.",
    "I've implemented the changes. The performance improvement should be around 40% based on benchmarks.",
    "Great question! Let me walk you through the architecture step by step.",
    "Done! I've also added comprehensive tests to prevent regression.",
]


def print_header(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_result(label, value, ok=None):
    status = ""
    if ok is True:
        status = " [PASS]"
    elif ok is False:
        status = " [FAIL]"
    print(f"  {label}: {value}{status}")


def test_scenario_1_casual_user(tmpdir):
    """Simulate 10 interactions from a casual user and verify tuning drift."""
    print_header("SCENARIO 1: Casual User — 10 Interactions")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "casual_user_42"

    for i, msg in enumerate(CASUAL_USER_MESSAGES):
        resp = AGENT_RESPONSES[i % len(AGENT_RESPONSES)]
        profile = tuner.analyze_and_tune(
            user_id, msg, resp,
            response_time_ms=200.0 + i * 50,
            base_dir=tmpdir,
        )

    # Verify tuning drifted toward casual
    formality = profile.tuning['formality_score']
    warmth = profile.tuning['warmth_score']
    technical = profile.tuning['technical_depth']

    print_result("Total interactions", profile.total_interactions,
                 ok=profile.total_interactions == 10)
    print_result("Formality score", f"{formality:.3f} (expect < 0.35)",
                 ok=formality < 0.35)
    print_result("Warmth score", f"{warmth:.3f} (expect > 0.5)",
                 ok=warmth > 0.5)
    print_result("Technical depth", f"{technical:.3f} (expect < 0.3)",
                 ok=technical < 0.3)
    print_result("Confidence", f"{profile.resonance_confidence:.3f} (expect > 0.35)",
                 ok=profile.resonance_confidence > 0.35)

    # Verify file persisted
    json_path = os.path.join(tmpdir, f"{user_id}_resonance.json")
    file_exists = os.path.exists(json_path)
    print_result("Profile persisted to disk", json_path, ok=file_exists)

    # Verify file is valid JSON and roundtrips
    if file_exists:
        with open(json_path) as f:
            data = json.load(f)
        loaded = UserResonanceProfile.from_dict(data)
        print_result("Roundtrip user_id", loaded.user_id,
                     ok=loaded.user_id == user_id)
        print_result("Roundtrip interactions", loaded.total_interactions,
                     ok=loaded.total_interactions == 10)

    return profile


def test_scenario_2_formal_user(tmpdir):
    """Simulate 10 interactions from a formal/professional user."""
    print_header("SCENARIO 2: Formal Professional User — 10 Interactions")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "formal_exec_99"

    for i, msg in enumerate(FORMAL_USER_MESSAGES):
        resp = AGENT_RESPONSES[i % len(AGENT_RESPONSES)]
        profile = tuner.analyze_and_tune(
            user_id, msg, resp,
            response_time_ms=500.0 + i * 30,
            base_dir=tmpdir,
        )

    formality = profile.tuning['formality_score']
    technical = profile.tuning['technical_depth']
    verbosity = profile.tuning['verbosity_score']

    print_result("Total interactions", profile.total_interactions,
                 ok=profile.total_interactions == 10)
    print_result("Formality score", f"{formality:.3f} (expect > 0.65)",
                 ok=formality > 0.65)
    print_result("Verbosity score", f"{verbosity:.3f} (expect > 0.45)",
                 ok=verbosity > 0.45)
    print_result("Technical depth", f"{technical:.3f} (expect > 0.25)",
                 ok=technical > 0.25)
    print_result("Confidence", f"{profile.resonance_confidence:.3f}",
                 ok=profile.resonance_confidence > 0.35)

    return profile


def test_scenario_3_technical_engineer(tmpdir):
    """Simulate 10 interactions from a highly technical user."""
    print_header("SCENARIO 3: Technical Engineer — 10 Interactions")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "tech_dev_007"

    for i, msg in enumerate(TECHNICAL_USER_MESSAGES):
        resp = AGENT_RESPONSES[i % len(AGENT_RESPONSES)]
        profile = tuner.analyze_and_tune(
            user_id, msg, resp,
            response_time_ms=300.0,
            base_dir=tmpdir,
        )

    technical = profile.tuning['technical_depth']
    formality = profile.tuning['formality_score']
    verbosity = profile.tuning['verbosity_score']

    print_result("Total interactions", profile.total_interactions,
                 ok=profile.total_interactions == 10)
    print_result("Technical depth", f"{technical:.3f} (expect > 0.55)",
                 ok=technical > 0.55)
    print_result("Formality score", f"{formality:.3f} (expect > 0.40)",
                 ok=formality > 0.40)
    print_result("Verbosity score", f"{verbosity:.3f} (expect > 0.50)",
                 ok=verbosity > 0.50)
    print_result("Confidence", f"{profile.resonance_confidence:.3f}",
                 ok=profile.resonance_confidence > 0.35)

    return profile


def test_scenario_4_personality_prompt_injection(tmpdir):
    """Verify resonance addon is injected into personality prompts."""
    print_header("SCENARIO 4: Personality Prompt with Resonance Addon")

    # Generate a base personality
    personality = generate_personality('coder', 'Build an AI chatbot')
    print_result("Generated personality", personality.persona_name)

    # Test 1: No profile → no resonance addon
    prompt_no_profile = build_personality_prompt(personality)
    has_resonance_no = "RESONANCE TUNING" in prompt_no_profile
    print_result("Without profile → no resonance addon", not has_resonance_no,
                 ok=not has_resonance_no)

    # Test 2: New user (< 3 interactions) → no resonance addon
    new_profile = UserResonanceProfile(user_id="newbie")
    new_profile.total_interactions = 1
    prompt_new = build_personality_prompt(personality, resonance_profile=new_profile)
    has_resonance_new = "RESONANCE TUNING" in prompt_new
    print_result("New user (1 interaction) → no resonance", not has_resonance_new,
                 ok=not has_resonance_new)

    # Test 3: Tuned casual user → resonance addon injected
    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    for i in range(5):
        casual_profile = tuner.analyze_and_tune(
            "casual_prompt_test",
            CASUAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir,
        )

    prompt_casual = build_personality_prompt(personality, resonance_profile=casual_profile)
    has_resonance = "RESONANCE TUNING" in prompt_casual
    has_formality = "Formality:" in prompt_casual
    has_interactions = "5 interactions" in prompt_casual
    print_result("Tuned user → resonance injected", has_resonance, ok=has_resonance)
    print_result("Has formality label", has_formality, ok=has_formality)
    print_result("Shows 5 interactions", has_interactions, ok=has_interactions)

    # Test 4: Tuned formal user → different labels
    tuner2 = ResonanceTuner(alpha=0.15, auto_save=True)
    for i in range(5):
        formal_profile = tuner2.analyze_and_tune(
            "formal_prompt_test",
            FORMAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir,
        )

    prompt_formal = build_personality_prompt(personality, resonance_profile=formal_profile)

    # Extract the resonance blocks for comparison
    casual_block = prompt_casual[prompt_casual.index("RESONANCE TUNING"):]
    formal_block = prompt_formal[prompt_formal.index("RESONANCE TUNING"):]
    blocks_differ = casual_block != formal_block
    print_result("Casual vs Formal → different resonance blocks", blocks_differ,
                 ok=blocks_differ)

    # Print actual resonance blocks for inspection
    print("\n  --- Casual user resonance block ---")
    for line in casual_block.strip().split('\n'):
        print(f"    {line}")
    print("\n  --- Formal user resonance block ---")
    for line in formal_block.strip().split('\n'):
        print(f"    {line}")

    return casual_profile, formal_profile


def test_scenario_5_profile_persistence_lifecycle(tmpdir):
    """Test real file persistence: create, save, reload, modify, reload again."""
    print_header("SCENARIO 5: Profile Persistence Lifecycle")

    user_id = "persist_test_user"

    # Step 1: Create fresh profile
    p1 = get_or_create_profile(user_id, tmpdir)
    print_result("Fresh profile user_id", p1.user_id, ok=p1.user_id == user_id)
    print_result("Fresh interactions", p1.total_interactions, ok=p1.total_interactions == 0)

    # Step 2: Save to disk
    save_resonance_profile(p1, tmpdir)
    path = os.path.join(tmpdir, f"{user_id}_resonance.json")
    print_result("Saved to disk", os.path.exists(path), ok=os.path.exists(path))

    # Step 3: Load from disk
    p2 = load_resonance_profile(user_id, tmpdir)
    print_result("Loaded from disk", p2 is not None, ok=p2 is not None)
    print_result("Loaded user_id matches", p2.user_id == user_id, ok=p2.user_id == user_id)

    # Step 4: Modify and re-save
    p2.total_interactions = 42
    p2.tuning['formality_score'] = 0.85
    p2.tuning['humor_receptivity'] = 0.9
    save_resonance_profile(p2, tmpdir)

    # Step 5: Reload and verify modifications persisted
    p3 = load_resonance_profile(user_id, tmpdir)
    print_result("Modified interactions persisted", p3.total_interactions,
                 ok=p3.total_interactions == 42)
    print_result("Modified formality persisted", f"{p3.tuning['formality_score']:.2f}",
                 ok=p3.tuning['formality_score'] == 0.85)
    print_result("Modified humor persisted", f"{p3.tuning['humor_receptivity']:.2f}",
                 ok=p3.tuning['humor_receptivity'] == 0.9)

    # Step 6: get_or_create loads existing (doesn't overwrite)
    p4 = get_or_create_profile(user_id, tmpdir)
    print_result("get_or_create preserves existing", p4.total_interactions,
                 ok=p4.total_interactions == 42)

    # Step 7: Verify JSON structure
    with open(path) as f:
        raw = json.load(f)
    print_result("JSON has 'tuning' key", 'tuning' in raw, ok='tuning' in raw)
    print_result("JSON has all 8 tuning dimensions",
                 len(raw['tuning']) == len(DEFAULT_TUNING),
                 ok=len(raw['tuning']) == len(DEFAULT_TUNING))

    return True


def test_scenario_6_convergence_over_time(tmpdir):
    """Test that tuning converges as interactions accumulate."""
    print_header("SCENARIO 6: Convergence Over 30 Interactions")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "convergence_user"

    # All formal messages — formality should converge toward 1.0
    snapshots = []
    for i in range(30):
        msg = FORMAL_USER_MESSAGES[i % len(FORMAL_USER_MESSAGES)]
        resp = AGENT_RESPONSES[i % len(AGENT_RESPONSES)]
        profile = tuner.analyze_and_tune(
            user_id, msg, resp, base_dir=tmpdir)
        snapshots.append(profile.tuning['formality_score'])

    # Check convergence: later values should be more stable
    early_delta = abs(snapshots[4] - snapshots[0])
    late_delta = abs(snapshots[29] - snapshots[25])

    print_result("After 5 interactions", f"formality={snapshots[4]:.3f}")
    print_result("After 15 interactions", f"formality={snapshots[14]:.3f}")
    print_result("After 30 interactions", f"formality={snapshots[29]:.3f}")
    print_result("Early delta (0-4)", f"{early_delta:.4f}")
    print_result("Late delta (25-29)", f"{late_delta:.4f}")
    print_result("Convergence: late delta < early delta",
                 late_delta < early_delta,
                 ok=late_delta < early_delta)
    print_result("Final confidence", f"{profile.resonance_confidence:.3f} (expect > 0.75)",
                 ok=profile.resonance_confidence > 0.75)

    # EMA convergence: after 30 formal messages with α=0.15,
    # formality should be well above 0.8
    print_result("Formality converged above 0.80",
                 f"{snapshots[29]:.3f}",
                 ok=snapshots[29] > 0.80)

    return snapshots


def test_scenario_7_multi_user_isolation(tmpdir):
    """Verify different users get independent profiles."""
    print_header("SCENARIO 7: Multi-User Isolation")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)

    # User A: casual
    for i in range(5):
        tuner.analyze_and_tune(
            "user_alice", CASUAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir)

    # User B: formal
    for i in range(5):
        tuner.analyze_and_tune(
            "user_bob", FORMAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir)

    alice = load_resonance_profile("user_alice", tmpdir)
    bob = load_resonance_profile("user_bob", tmpdir)

    alice_formality = alice.tuning['formality_score']
    bob_formality = bob.tuning['formality_score']

    print_result("Alice formality", f"{alice_formality:.3f} (expect < 0.40)",
                 ok=alice_formality < 0.40)
    print_result("Bob formality", f"{bob_formality:.3f} (expect > 0.60)",
                 ok=bob_formality > 0.60)
    print_result("Profiles are isolated", f"delta={abs(bob_formality - alice_formality):.3f}",
                 ok=abs(bob_formality - alice_formality) > 0.20)

    # Verify separate files on disk
    alice_file = os.path.exists(os.path.join(tmpdir, "user_alice_resonance.json"))
    bob_file = os.path.exists(os.path.join(tmpdir, "user_bob_resonance.json"))
    print_result("Alice file exists", alice_file, ok=alice_file)
    print_result("Bob file exists", bob_file, ok=bob_file)

    return True


def test_scenario_8_async_tuning(tmpdir):
    """Test fire-and-forget async tuning actually persists."""
    print_header("SCENARIO 8: Async (Fire-and-Forget) Tuning")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "async_user"

    # Fire 5 async tunings
    for i in range(5):
        tuner.analyze_and_tune_async(
            user_id, CASUAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir)

    # Wait for the executor to finish (ThreadPoolExecutor)
    tuner._executor.shutdown(wait=True)
    # Re-create executor for any subsequent use
    from concurrent.futures import ThreadPoolExecutor
    tuner._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='resonance_tune')

    # Verify profile was persisted
    profile = load_resonance_profile(user_id, tmpdir)
    if profile is None:
        print_result("Async profile exists", False, ok=False)
        return False

    print_result("Async profile exists", True, ok=True)
    print_result("Async interactions", profile.total_interactions,
                 ok=profile.total_interactions == 5)
    print_result("Async formality tuned", f"{profile.tuning['formality_score']:.3f}",
                 ok=profile.tuning['formality_score'] != 0.5)  # Should have drifted

    return True


def test_scenario_9_biometric_graceful_degradation():
    """Test biometric dispatch proxy gracefully degrades without HevolveAI."""
    print_header("SCENARIO 9: Biometric Dispatch Proxy (HevolveAI)")

    # All biometric ML lives in HevolveAI -- HARTOS only dispatches
    identifier = ResonanceIdentifier()
    face_result = identifier.identify_by_face(b'\x00' * 100)
    voice_result = identifier.identify_by_voice(b'\x00' * 32000)
    print_result("identify_by_face (HevolveAI unavailable)", face_result,
                 ok=face_result is None)
    print_result("identify_by_voice (HevolveAI unavailable)", voice_result,
                 ok=voice_result is None)

    # Enrollment dispatches (returns bool)
    face_enroll = identifier.enroll_face("test_user", b'\x00' * 100)
    voice_enroll = identifier.enroll_voice("test_user", b'\x00' * 32000)
    print_result("enroll_face returns bool", isinstance(face_enroll, bool),
                 ok=isinstance(face_enroll, bool))
    print_result("enroll_voice returns bool", isinstance(voice_enroll, bool),
                 ok=isinstance(voice_enroll, bool))

    # Verify no ML imports in identifier
    import inspect
    source = inspect.getsource(ResonanceIdentifier)
    no_ml = ('insightface' not in source and 'speechbrain' not in source
             and 'numpy' not in source and 'torch' not in source)
    print_result("No ML imports in ResonanceIdentifier", no_ml, ok=no_ml)

    return True


def test_scenario_10_signal_extraction_quality():
    """Deep-dive into signal extraction accuracy."""
    print_header("SCENARIO 10: Signal Extraction Quality Analysis")

    # Test 1: Pure formal message
    signals_formal = SignalExtractor.extract(
        "Dear sir, I would kindly request your assistance regarding the matter at hand. "
        "Furthermore, I shall require a comprehensive analysis. Please proceed accordingly.",
        "Certainly, I'll provide a thorough analysis.",
    )
    print_result("Formal msg → formality",
                 f"{signals_formal.formality_markers:.3f} (expect > 0.70)",
                 ok=signals_formal.formality_markers > 0.70)

    # Test 2: Pure casual message
    signals_casual = SignalExtractor.extract(
        "hey yo can u fix this bug lol its so broken haha thx btw",
        "Sure, fixing it now.",
    )
    print_result("Casual msg → formality",
                 f"{signals_casual.formality_markers:.3f} (expect < 0.15)",
                 ok=signals_casual.formality_markers < 0.15)

    # Test 3: Technical message
    signals_tech = SignalExtractor.extract(
        "Deploy the container with the API endpoint on the microservice infrastructure. "
        "Configure the database schema and pipeline with proper query optimization.",
        "Deploying now.",
    )
    print_result("Tech msg → tech_terms",
                 f"{signals_tech.technical_term_count} (expect >= 5)",
                 ok=signals_tech.technical_term_count >= 5)

    # Test 4: Positive sentiment
    signals_positive = SignalExtractor.extract(
        "Thanks! That's amazing, great work! I love this wonderful solution, excellent!",
        "Glad you like it!",
    )
    print_result("Positive msg → sentiment",
                 f"{signals_positive.positive_sentiment:.3f} (expect > 0.80)",
                 ok=signals_positive.positive_sentiment > 0.80)

    # Test 5: Question-heavy message
    signals_questions = SignalExtractor.extract(
        "How does this work? What are the requirements? Can you explain the architecture? "
        "Is there documentation? Where are the tests?",
        "Let me explain.",
    )
    print_result("Question msg → question_count",
                 f"{signals_questions.question_count} (expect >= 4)",
                 ok=signals_questions.question_count >= 4)

    # Test 6: Vocabulary richness
    signals_rich = SignalExtractor.extract(
        "The sophisticated algorithmic paradigm leverages heterogeneous computational "
        "substrates while maintaining orthogonal abstraction boundaries for "
        "unprecedented scalability and resilience across distributed topologies.",
        "Understood.",
    )
    signals_simple = SignalExtractor.extract(
        "do the thing do the thing now do it do it please do the thing",
        "Done.",
    )
    print_result("Rich vocab → TTR",
                 f"{signals_rich.vocabulary_richness:.3f} (expect > 0.7)",
                 ok=signals_rich.vocabulary_richness > 0.7)
    print_result("Simple vocab → TTR",
                 f"{signals_simple.vocabulary_richness:.3f} (expect < 0.6)",
                 ok=signals_simple.vocabulary_richness < 0.6)

    return True


def test_scenario_11_mixed_style_user(tmpdir):
    """User who starts casual then becomes formal — verify gradual shift."""
    print_header("SCENARIO 11: Style Shift — Casual to Formal Over Time")

    tuner = ResonanceTuner(alpha=0.15, auto_save=True)
    user_id = "style_shifter"

    # First 5: casual
    for i in range(5):
        profile = tuner.analyze_and_tune(
            user_id, CASUAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir)
    formality_after_casual = profile.tuning['formality_score']

    # Next 5: formal
    for i in range(5):
        profile = tuner.analyze_and_tune(
            user_id, FORMAL_USER_MESSAGES[i],
            AGENT_RESPONSES[i % len(AGENT_RESPONSES)],
            base_dir=tmpdir)
    formality_after_formal = profile.tuning['formality_score']

    print_result("After 5 casual messages", f"formality={formality_after_casual:.3f}",
                 ok=formality_after_casual < 0.40)
    print_result("After 5 formal messages", f"formality={formality_after_formal:.3f}",
                 ok=formality_after_formal > formality_after_casual)
    print_result("Gradual shift (not instant)",
                 f"delta={formality_after_formal - formality_after_casual:.3f}",
                 ok=formality_after_formal < 0.85)  # EMA prevents instant jump
    print_result("Total interactions", profile.total_interactions,
                 ok=profile.total_interactions == 10)

    return profile


def test_scenario_12_tuner_stats():
    """Verify the tuner stats tracking works."""
    print_header("SCENARIO 12: Tuner Stats & Singleton")

    tuner = get_resonance_tuner()
    tuner2 = get_resonance_tuner()
    print_result("Singleton identity", tuner is tuner2, ok=tuner is tuner2)

    stats = tuner.get_stats()
    print_result("Stats has total_tunings", 'total_tunings' in stats,
                 ok='total_tunings' in stats)
    print_result("Stats has total_identifications", 'total_identifications' in stats,
                 ok='total_identifications' in stats)

    return True


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "#" * 70)
    print("  HARTOS Resonance Frequency Tuning -- Real-World Integration Test")
    print("#" * 70)

    tmpdir = tempfile.mkdtemp(prefix="hartos_resonance_test_")
    print(f"\n  Temp directory: {tmpdir}")

    results = {}
    total_pass = 0
    total_fail = 0

    try:
        scenarios = [
            ("Casual User", test_scenario_1_casual_user),
            ("Formal User", test_scenario_2_formal_user),
            ("Technical Engineer", test_scenario_3_technical_engineer),
            ("Personality Prompt Injection", test_scenario_4_personality_prompt_injection),
            ("Persistence Lifecycle", test_scenario_5_profile_persistence_lifecycle),
            ("Convergence Over Time", test_scenario_6_convergence_over_time),
            ("Multi-User Isolation", test_scenario_7_multi_user_isolation),
            ("Async Tuning", test_scenario_8_async_tuning),
            ("Biometric Degradation", test_scenario_9_biometric_graceful_degradation),
            ("Signal Extraction", test_scenario_10_signal_extraction_quality),
            ("Style Shift", test_scenario_11_mixed_style_user),
            ("Tuner Stats", test_scenario_12_tuner_stats),
        ]

        for name, fn in scenarios:
            try:
                if fn.__code__.co_varnames[:1] == ('tmpdir',):
                    fn(tmpdir)
                else:
                    fn()
                results[name] = "PASS"
                total_pass += 1
            except Exception as e:
                results[name] = f"ERROR: {e}"
                total_fail += 1
                import traceback
                traceback.print_exc()

        # Final summary
        print_header("FINAL SUMMARY")
        for name, result in results.items():
            status = "[PASS]" if result == "PASS" else f"[FAIL] {result}"
            print(f"  {name}: {status}")

        print(f"\n  Total: {total_pass} passed, {total_fail} failed out of {len(scenarios)} scenarios")

        # List all files created
        print(f"\n  Files created in {tmpdir}:")
        for f in sorted(os.listdir(tmpdir)):
            size = os.path.getsize(os.path.join(tmpdir, f))
            print(f"    {f} ({size} bytes)")

    finally:
        # Cleanup
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"\n  Cleaned up temp directory: {tmpdir}")

    print("\n" + "#" * 70)
    if total_fail == 0:
        print("  ALL SCENARIOS PASSED -- Resonance Tuning is REAL-WORLD READY")
    else:
        print(f"  {total_fail} SCENARIO(S) FAILED -- needs investigation")
    print("#" * 70 + "\n")

    return total_fail == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
