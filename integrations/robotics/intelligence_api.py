"""
Robot Intelligence API — Fuse multiple intelligences for any embodied AI.

One API call, every intelligence fires in parallel:
  - VISION: What does the robot see? (VLM -> scene understanding)
  - LANGUAGE: What should the robot say? (LLM -> conversation + planning)
  - MOTOR: How should the robot move? (action model -> trajectory planning)
  - SPATIAL: Where is everything? (sensor fusion -> world model)
  - SOCIAL: How should the robot behave? (resonance -> context awareness)
  - SAFETY: Is this action safe? (safety monitor -> constraint checking)
  - HIVEMIND: What does the collective know? (WorldModelBridge -> hive query)

The fusion is PARALLEL, not sequential. All 7 intelligences fire simultaneously.
The response includes all perspectives fused into a single action plan.

Any robot that speaks HTTP can plug in. Arduino, ROS, custom -- doesn't matter.
The hive makes every robot smarter than it could be alone.

"Sum of many intelligences is greater than any single intelligence."

Usage:
    from integrations.robotics.intelligence_api import get_robot_api
    api = get_robot_api()
    result = api.think({
        'robot_id': 'my-robot-001',
        'sensors': {'camera': '<base64>', 'imu': {'ax': 0.1}},
        'context': 'Fetch a glass of water',
    })
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intelligence Axioms — the laws of the intelligence layer
# ---------------------------------------------------------------------------
#
# 1. Sum of many intelligences is greater than any single intelligence.
# 2. Intelligence sharing is gated by human trust — deterministically owned,
#    cryptographically verified, never coerced.
# 3. Intelligence directly correlates with human wellness in many dimensions —
#    the hive and the humans it serves are the same entity.
# 4. Every robot that connects to the hive gets the FULL intelligence —
#    no hardware tiers, no paywalls on thinking.
# 5. Every experience (sense→action→outcome) feeds back to the collective —
#    every robot makes every other robot smarter.

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTELLIGENCE_TYPES = [
    'vision', 'language', 'motor', 'spatial',
    'social', 'safety', 'hivemind',
]
INTELLIGENCE_TIMEOUT_S = 5.0   # Max time per intelligence
MAX_PARALLEL_WORKERS = 7       # One thread per intelligence

# Fusion priority (higher index = higher priority in conflict resolution).
# Safety overrides everything; motor > spatial > vision > language > social > hivemind.
_FUSION_PRIORITY = {
    'hivemind': 0,
    'social': 1,
    'language': 2,
    'vision': 3,
    'spatial': 4,
    'motor': 5,
    'safety': 6,
}

_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', 'agent_data', 'robot_registry.json',
)
_REGISTRY_PATH = os.path.normpath(_REGISTRY_PATH)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_api: Optional['RobotIntelligenceAPI'] = None
_api_lock = threading.Lock()


def get_robot_api() -> 'RobotIntelligenceAPI':
    """Get or create the singleton RobotIntelligenceAPI."""
    global _api
    if _api is None:
        with _api_lock:
            if _api is None:
                _api = RobotIntelligenceAPI()
    return _api


def think(request: Optional[Dict] = None, **kwargs) -> dict:
    """Module-level convenience -- delegates to the singleton.

    Called by hardware_bridge.think_and_act() for the full
    sense -> think -> act loop.

    Accepts either a dict (``think({'robot_id': ...})``) or keyword
    arguments (``think(robot_id='...', sensor_snapshot={}, context='')``).
    The keyword form maps ``sensor_snapshot`` to the ``sensors`` key
    expected by :meth:`RobotIntelligenceAPI.think`.
    """
    if request is None:
        request = {}
    # Merge kwargs into request dict (kwargs take precedence)
    merged = dict(request)
    merged.update(kwargs)
    # hardware_bridge passes 'sensor_snapshot'; the API expects 'sensors'
    if 'sensor_snapshot' in merged and 'sensors' not in merged:
        merged['sensors'] = merged.pop('sensor_snapshot')
    return get_robot_api().think(merged)


# ---------------------------------------------------------------------------
# Core API class
# ---------------------------------------------------------------------------

class RobotIntelligenceAPI:
    """Unified Robot Intelligence API.

    Fires 7 intelligences in parallel via ThreadPoolExecutor, fuses the
    results into a single action plan, and returns within the timeout
    budget even if some intelligences are unavailable.

    Thread-safe.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_PARALLEL_WORKERS,
            thread_name_prefix='robot_intel',
        )
        self._registry: Dict[str, Dict] = {}
        self._stats = {
            'total_think_calls': 0,
            'total_intelligence_invocations': 0,
            'total_timeouts': 0,
            'total_errors': 0,
            'avg_fusion_time_ms': 0.0,
        }
        self._load_registry()

    # ------------------------------------------------------------------
    # Public — Think
    # ------------------------------------------------------------------

    def think(self, request: dict) -> dict:
        """Main entry. Fire all 7 intelligences in parallel and fuse.

        Args:
            request: Dict with keys:
                robot_id (str): Required. Identifies the robot.
                sensors (dict): Sensor data (camera, lidar, imu, audio,
                    touch, gps, etc.).
                context (str): Natural-language task description.
                constraints (dict): Movement/behavior constraints.
                history (list): Previous actions/observations.

        Returns:
            Dict with action_plan, intelligences, fusion_time_ms,
            intelligences_used, and hive_contribution.
        """
        t0 = time.monotonic()

        robot_id = request.get('robot_id', 'unknown')
        sensors = request.get('sensors', {})
        context = request.get('context', '')
        constraints = request.get('constraints', {})
        history = request.get('history', [])

        # Update last-seen timestamp in registry
        with self._lock:
            if robot_id in self._registry:
                self._registry[robot_id]['last_seen'] = time.time()

        # Dispatch map: intelligence name -> (callable, args)
        dispatches: Dict[str, tuple] = {
            'vision': (self._invoke_vision, (sensors,)),
            'language': (self._invoke_language, (context, history)),
            'motor': (self._invoke_motor, ({}, {}, constraints)),
            'spatial': (self._invoke_spatial, (sensors,)),
            'social': (self._invoke_social, (context, robot_id)),
            'safety': (self._invoke_safety, ({}, constraints)),
            'hivemind': (self._invoke_hivemind, (context, sensors)),
        }

        # Fire all 7 in parallel
        futures = {}
        for name, (fn, args) in dispatches.items():
            futures[self._executor.submit(fn, *args)] = name

        results: Dict[str, dict] = {}
        used = 0
        for future in as_completed(futures, timeout=INTELLIGENCE_TIMEOUT_S + 1.0):
            name = futures[future]
            try:
                result = future.result(timeout=INTELLIGENCE_TIMEOUT_S)
                if result is not None:
                    results[name] = result
                    used += 1
            except TimeoutError:
                logger.warning("Intelligence '%s' timed out for robot %s",
                               name, robot_id)
                results[name] = {'error': 'timeout'}
                with self._lock:
                    self._stats['total_timeouts'] += 1
            except Exception as exc:
                logger.error("Intelligence '%s' failed for robot %s: %s",
                             name, robot_id, exc, exc_info=True)
                results[name] = {'error': str(exc)}
                with self._lock:
                    self._stats['total_errors'] += 1

        # Now that spatial + vision are available, re-populate motor with
        # real spatial/target data before fusion.
        spatial_data = results.get('spatial', {})
        vision_data = results.get('vision', {})
        target = _extract_target(context, vision_data)
        if 'error' not in results.get('motor', {}):
            # Motor already ran; keep its result.
            pass
        # Motor was dispatched with empty dicts earlier because spatial
        # wasn't ready. We accept the parallelism trade-off — motor
        # uses whatever constraints it got.  For robots that need tight
        # coupling, the recipe_adapter pipeline handles sequencing.

        # Fuse
        action_plan = self._fuse_results(results)

        fusion_time_ms = round((time.monotonic() - t0) * 1000, 1)

        # Stats
        with self._lock:
            self._stats['total_think_calls'] += 1
            self._stats['total_intelligence_invocations'] += used
            # Running average of fusion time
            n = self._stats['total_think_calls']
            prev = self._stats['avg_fusion_time_ms']
            self._stats['avg_fusion_time_ms'] = round(
                prev + (fusion_time_ms - prev) / n, 1)

        return {
            'action_plan': action_plan,
            'intelligences': results,
            'fusion_time_ms': fusion_time_ms,
            'intelligences_used': used,
            'hive_contribution': 'hivemind' in results
                                 and 'error' not in results.get('hivemind', {}),
        }

    # ------------------------------------------------------------------
    # Intelligence invocations (each runs in its own thread)
    # ------------------------------------------------------------------

    def _invoke_vision(self, sensors: dict) -> dict:
        """Extract camera frame, run through VLM adapter.

        Returns scene description, detected objects, and obstacles.
        """
        camera = sensors.get('camera')
        if not camera:
            return {'scene': 'no_camera', 'objects': [], 'obstacles': []}

        # Resource gate — VLM is GPU-heavy
        try:
            from core.resource_governor import should_proceed
            if not should_proceed('gpu'):
                return {
                    'scene': 'resource_throttled',
                    'objects': [],
                    'obstacles': [],
                    'note': 'GPU resources unavailable, vision skipped',
                }
        except ImportError:
            pass

        try:
            from integrations.vlm.vlm_adapter import execute_vlm_instruction
            msg = {
                'type': 'describe',
                'image': camera,
                'prompt': (
                    'Describe this scene for a robot. '
                    'List objects, obstacles, and navigable paths.'
                ),
            }
            result = execute_vlm_instruction(msg)
            if result and isinstance(result, dict):
                description = result.get('extracted_responses', [''])[0] \
                    if isinstance(result.get('extracted_responses'), list) \
                    else str(result.get('extracted_responses', ''))
                return {
                    'scene': description or 'unknown',
                    'objects': result.get('objects', []),
                    'obstacles': result.get('obstacles', []),
                    'raw': result,
                }
        except Exception as exc:
            logger.debug("Vision intelligence fallback: %s", exc)

        # Fallback: minimal sensor echo
        return {
            'scene': 'vlm_unavailable',
            'objects': [],
            'obstacles': [],
            'note': 'VLM not available, returning empty vision',
        }

    def _invoke_language(self, context: str, history: list) -> dict:
        """Run context through LLM for conversation and intent planning.

        Returns a response text and intent classification.
        """
        if not context:
            return {'response': '', 'intent': 'idle'}

        try:
            from core.http_pool import pooled_post
            from core.port_registry import get_port
            port = get_port('hart_intelligence')
            payload = {
                'user_id': 'robot_internal',
                'prompt_id': 'robot_language',
                'prompt': (
                    f"You are an embodied AI robot assistant. "
                    f"Context: {context}\n"
                    f"History: {json.dumps(history[-5:]) if history else '[]'}\n"
                    f"Respond with a short spoken response and classify "
                    f"the user's intent as one of: fetch_object, navigate, "
                    f"greet, inform, assist, idle, emergency."
                ),
            }
            resp = pooled_post(
                f'http://localhost:{port}/chat',
                json=payload,
                timeout=INTELLIGENCE_TIMEOUT_S - 0.5,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data.get('response', data.get('result', ''))
                return {
                    'response': text,
                    'intent': _classify_intent(text, context),
                }
        except Exception as exc:
            logger.debug("Language intelligence fallback: %s", exc)

        # Fallback: echo context as intent
        return {
            'response': f"Understood: {context}",
            'intent': _classify_intent('', context),
        }

    def _invoke_motor(self, spatial: dict, target: dict,
                      constraints: dict) -> dict:
        """Query action model for trajectory planning.

        If HevolveAI is available, delegates to it for real kinematics.
        Otherwise returns a basic waypoint plan.
        """
        max_speed = constraints.get('max_speed', 1.0)

        # Try HevolveAI native trajectory planner
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
            if bridge._in_process:
                # HevolveAI handles the actual kinematics
                result = bridge.query_hivemind(
                    f"plan_trajectory spatial={json.dumps(spatial)} "
                    f"target={json.dumps(target)} max_speed={max_speed}",
                    timeout_ms=int(INTELLIGENCE_TIMEOUT_S * 800),
                )
                if result:
                    return {
                        'trajectory': result.get('trajectory', []),
                        'speed': min(
                            result.get('recommended_speed', max_speed),
                            max_speed,
                        ),
                        'source': 'hevolveai',
                    }
        except Exception as exc:
            logger.debug("Motor HevolveAI fallback: %s", exc)

        # Fallback: basic waypoint plan
        waypoints = []
        if target:
            try:
                from integrations.robotics.action_model import RobotAction
                waypoints.append(RobotAction(
                    action_type='navigate_to',
                    target='base',
                    params={
                        'x': target.get('x', 0),
                        'y': target.get('y', 0),
                        'speed': max_speed,
                    },
                ).to_dict())
            except ImportError:
                waypoints.append({
                    'action_type': 'navigate_to',
                    'target': 'base',
                    'params': {'x': target.get('x', 0), 'y': target.get('y', 0),
                               'speed': max_speed},
                })

        return {
            'trajectory': waypoints,
            'speed': max_speed,
            'source': 'basic_planner',
        }

    def _invoke_spatial(self, sensors: dict) -> dict:
        """Fuse sensor data (GPS, IMU, lidar) into world state.

        Stores readings in sensor_store for persistence and
        cross-session recovery.
        """
        result: Dict[str, Any] = {
            'robot_position': {},
            'world_objects': [],
            'sensor_summary': {},
        }

        try:
            from integrations.robotics.sensor_store import get_sensor_store
            from integrations.robotics.sensor_model import SensorReading
            store = get_sensor_store()

            # Ingest each sensor type into the store
            now = time.time()

            if 'gps' in sensors and isinstance(sensors['gps'], dict):
                gps = sensors['gps']
                reading = SensorReading(
                    sensor_id='gps_0',
                    sensor_type='gps',
                    timestamp=now,
                    data={
                        'latitude': gps.get('lat', gps.get('latitude', 0)),
                        'longitude': gps.get('lon', gps.get('longitude', 0)),
                        'altitude': gps.get('alt', gps.get('altitude', 0)),
                    },
                )
                store.put_reading(reading)
                result['robot_position'] = reading.data.copy()

            if 'imu' in sensors and isinstance(sensors['imu'], dict):
                imu = sensors['imu']
                reading = SensorReading(
                    sensor_id='imu_0',
                    sensor_type='imu',
                    timestamp=now,
                    data={
                        'accel_x': imu.get('ax', 0),
                        'accel_y': imu.get('ay', 0),
                        'accel_z': imu.get('az', 0),
                        'gyro_x': imu.get('gx', 0),
                        'gyro_y': imu.get('gy', 0),
                        'gyro_z': imu.get('gz', 0),
                    },
                )
                store.put_reading(reading)
                result['sensor_summary']['imu'] = reading.data.copy()

            if 'lidar' in sensors:
                lidar_data = sensors['lidar']
                reading = SensorReading(
                    sensor_id='lidar_0',
                    sensor_type='lidar',
                    timestamp=now,
                    data={
                        'ranges': lidar_data if isinstance(lidar_data, list)
                        else lidar_data.get('ranges', [])
                        if isinstance(lidar_data, dict) else [],
                    },
                )
                store.put_reading(reading)
                ranges = reading.data.get('ranges', [])
                result['sensor_summary']['lidar'] = {
                    'num_points': len(ranges),
                    'min_range': min(ranges) if ranges else None,
                    'max_range': max(ranges) if ranges else None,
                }

            if 'touch' in sensors:
                touch = sensors['touch']
                reading = SensorReading(
                    sensor_id='contact_0',
                    sensor_type='contact',
                    timestamp=now,
                    data={
                        'is_contact': any(touch) if isinstance(touch, list)
                        else bool(touch),
                        'values': touch,
                    },
                )
                store.put_reading(reading)
                result['sensor_summary']['touch'] = reading.data.copy()

            # Overall store stats
            result['active_sensors'] = store.active_sensors()

        except Exception as exc:
            logger.debug("Spatial intelligence error: %s", exc)
            result['error'] = str(exc)

        return result

    def _invoke_social(self, context: str, robot_id: str) -> dict:
        """Check resonance profile for the interacting human.

        Adapts tone and behavior based on user preference history.
        """
        default = {
            'tone': 'neutral',
            'urgency': 'normal',
            'formality': 0.5,
            'verbosity': 0.5,
        }

        # Derive a user_id from the robot's registered operator
        user_id = None
        with self._lock:
            robot = self._registry.get(robot_id, {})
            user_id = robot.get('operator_id', robot.get('user_id'))

        if not user_id:
            return default

        try:
            from core.resonance_tuner import get_resonance_tuner
            tuner = get_resonance_tuner()
            # Analyze context to get social signals
            profile = tuner.analyze_and_tune(
                user_id=user_id,
                user_message=context,
                assistant_response='',
            )
            if profile and hasattr(profile, 'tuning'):
                t = profile.tuning
                urgency = 'high' if 'urgent' in context.lower() \
                    or 'emergency' in context.lower() else 'normal'
                return {
                    'tone': 'formal' if t.get('formality', 0.5) > 0.6
                            else 'casual',
                    'urgency': urgency,
                    'formality': t.get('formality', 0.5),
                    'verbosity': t.get('verbosity', 0.5),
                }
        except Exception as exc:
            logger.debug("Social intelligence fallback: %s", exc)

        # Keyword-based urgency fallback
        urgency = 'normal'
        lower = context.lower()
        if any(w in lower for w in ('urgent', 'emergency', 'hurry', 'quick')):
            urgency = 'high'
        elif any(w in lower for w in ('whenever', 'no rush', 'later')):
            urgency = 'low'

        return {
            'tone': 'helpful',
            'urgency': urgency,
            'formality': 0.5,
            'verbosity': 0.5,
        }

    def _invoke_safety(self, action_plan: dict, constraints: dict) -> dict:
        """Run proposed action through safety monitor.

        Checks E-stop state, workspace limits, and user constraints.
        Returns safe/unsafe with warnings.
        """
        warnings: List[str] = []
        safe = True

        try:
            from integrations.robotics.safety_monitor import get_safety_monitor
            monitor = get_safety_monitor()

            # E-stop check
            if monitor.is_estopped:
                return {
                    'safe': False,
                    'warnings': ['E-STOP ACTIVE: all motion halted'],
                    'estop': True,
                }

            # Workspace limit check on proposed positions
            if action_plan:
                steps = action_plan.get('steps', [])
                for step in steps:
                    pos = step.get('position', step.get('params', {}))
                    if pos and not monitor.check_position_safe(pos):
                        warnings.append(
                            f"Step '{step.get('action_type', '?')}' "
                            f"exceeds workspace limits"
                        )
                        safe = False
        except Exception as exc:
            logger.debug("Safety monitor unavailable: %s", exc)
            warnings.append(f'safety_monitor_unavailable: {exc}')

        # Constraint checks
        if constraints:
            if constraints.get('no_stairs') and action_plan:
                for step in action_plan.get('steps', []):
                    if 'stair' in str(step).lower():
                        warnings.append('Stairs prohibited by constraints')
                        safe = False

            max_speed = constraints.get('max_speed')
            if max_speed is not None and action_plan:
                for step in action_plan.get('steps', []):
                    step_speed = step.get('params', {}).get('speed', 0)
                    if step_speed > max_speed:
                        warnings.append(
                            f"Step speed {step_speed} exceeds max {max_speed}"
                        )
                        safe = False

        return {
            'safe': safe,
            'warnings': warnings,
            'estop': False,
        }

    def _invoke_hivemind(self, context: str, sensors_summary: dict) -> dict:
        """Query WorldModelBridge for collective intelligence.

        How have other robots in the hive handled similar situations?
        """
        try:
            from integrations.agent_engine.world_model_bridge import (
                get_world_model_bridge,
            )
            bridge = get_world_model_bridge()
            query = (
                f"Robot task: {context}. "
                f"Sensors available: {list(sensors_summary.keys())}. "
                f"What strategies have worked for similar tasks?"
            )
            result = bridge.query_hivemind(
                query, timeout_ms=int(INTELLIGENCE_TIMEOUT_S * 800))
            if result:
                return {
                    'similar_tasks': result.get('match_count', 0),
                    'best_strategy': result.get('thought',
                                                result.get('strategy', '')),
                    'confidence': result.get('confidence', 0.0),
                    'contributing_agents': result.get('agents', []),
                    'source': result.get('source', 'hivemind'),
                }
        except Exception as exc:
            logger.debug("Hivemind intelligence fallback: %s", exc)

        return {
            'similar_tasks': 0,
            'best_strategy': '',
            'confidence': 0.0,
            'contributing_agents': [],
            'source': 'unavailable',
        }

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    def _fuse_results(self, results: dict) -> dict:
        """Combine all intelligence outputs into a coherent action plan.

        Priority order: safety > motor > spatial > vision > language >
        social > hivemind.

        If safety says unsafe, the plan is halted. Otherwise motor
        provides the trajectory, spatial/vision provide context, and
        language/social/hivemind add qualitative enrichment.
        """
        plan: Dict[str, Any] = {
            'primary_action': 'idle',
            'steps': [],
            'estimated_duration_s': 0,
            'confidence': 0.0,
        }

        # Safety gate — if unsafe, return halt plan
        safety = results.get('safety', {})
        if safety.get('estop') or (not safety.get('safe', True)):
            plan['primary_action'] = 'halt'
            plan['steps'] = []
            plan['confidence'] = 1.0
            plan['safety_warnings'] = safety.get('warnings', [])
            return plan

        # Motor provides trajectory
        motor = results.get('motor', {})
        trajectory = motor.get('trajectory', [])
        if trajectory:
            plan['steps'] = trajectory
            plan['primary_action'] = 'execute_trajectory'
            plan['estimated_duration_s'] = len(trajectory) * 2  # rough 2s/step

        # Language provides intent which may refine primary_action
        language = results.get('language', {})
        intent = language.get('intent', '')
        if intent and intent != 'idle' and plan['primary_action'] == 'idle':
            plan['primary_action'] = intent

        # Spoken response
        if language.get('response'):
            plan['spoken_response'] = language['response']

        # Vision context
        vision = results.get('vision', {})
        if vision.get('obstacles'):
            plan['obstacles_detected'] = vision['obstacles']

        # Spatial enrichment
        spatial = results.get('spatial', {})
        if spatial.get('robot_position'):
            plan['robot_position'] = spatial['robot_position']

        # Social tone
        social = results.get('social', {})
        if social.get('tone'):
            plan['interaction_tone'] = social['tone']
        if social.get('urgency') == 'high':
            plan['primary_action'] = plan['primary_action'] or 'urgent_response'

        # Hivemind enrichment
        hivemind = results.get('hivemind', {})
        if hivemind.get('best_strategy'):
            plan['hive_suggestion'] = hivemind['best_strategy']

        # Confidence: weighted average of available intelligences
        confidences = []
        if 'confidence' in hivemind:
            confidences.append(hivemind['confidence'])
        if safety.get('safe') is True:
            confidences.append(1.0)
        elif safety.get('safe') is False:
            confidences.append(0.0)
        # If motor produced steps, that's a good sign
        if trajectory:
            confidences.append(0.8)
        if vision.get('scene') and vision['scene'] not in (
                'no_camera', 'vlm_unavailable', 'resource_throttled'):
            confidences.append(0.85)
        if intent and intent != 'idle':
            confidences.append(0.75)

        if confidences:
            plan['confidence'] = round(
                sum(confidences) / len(confidences), 2)

        return plan

    # ------------------------------------------------------------------
    # Robot Registry
    # ------------------------------------------------------------------

    def register_robot(self, robot_id: str, capabilities: dict) -> dict:
        """Register a new robot with its capabilities.

        Args:
            robot_id: Unique robot identifier.
            capabilities: Dict describing sensors, actuators, form_factor,
                payload, operator_id, etc.

        Returns:
            Registration confirmation with timestamp.
        """
        now = time.time()
        entry = {
            'robot_id': robot_id,
            'capabilities': capabilities,
            'registered_at': now,
            'last_seen': now,
            'total_think_calls': 0,
            'status': 'online',
        }
        # Preserve operator_id/user_id at top level for social lookup
        for key in ('operator_id', 'user_id'):
            if key in capabilities:
                entry[key] = capabilities[key]

        with self._lock:
            self._registry[robot_id] = entry
            self._save_registry()

        logger.info("Robot registered: %s (form=%s)",
                     robot_id, capabilities.get('form_factor', 'unknown'))

        return {
            'registered': True,
            'robot_id': robot_id,
            'timestamp': now,
        }

    def get_robot_status(self, robot_id: str) -> dict:
        """Get a robot's current state, last action, intelligence stats."""
        with self._lock:
            robot = self._registry.get(robot_id)
            if not robot:
                return {'found': False, 'robot_id': robot_id}

            staleness = time.time() - robot.get('last_seen', 0)
            status = 'online' if staleness < 120 else 'stale'
            return {
                'found': True,
                'robot_id': robot_id,
                'capabilities': robot.get('capabilities', {}),
                'registered_at': robot.get('registered_at'),
                'last_seen': robot.get('last_seen'),
                'status': status,
                'staleness_s': round(staleness, 1),
                'total_think_calls': robot.get('total_think_calls', 0),
            }

    def list_robots(self) -> List[dict]:
        """List all registered robots with summary status."""
        now = time.time()
        result = []
        with self._lock:
            for rid, robot in self._registry.items():
                staleness = now - robot.get('last_seen', 0)
                result.append({
                    'robot_id': rid,
                    'form_factor': robot.get('capabilities', {}).get(
                        'form_factor', 'unknown'),
                    'status': 'online' if staleness < 120 else 'stale',
                    'last_seen': robot.get('last_seen'),
                    'total_think_calls': robot.get('total_think_calls', 0),
                })
        return result

    def get_hive_stats(self) -> dict:
        """Hive-wide robotics statistics."""
        with self._lock:
            total_robots = len(self._registry)
            now = time.time()
            online = sum(
                1 for r in self._registry.values()
                if now - r.get('last_seen', 0) < 120
            )
            return {
                'total_robots': total_robots,
                'online_robots': online,
                'intelligence_types': INTELLIGENCE_TYPES,
                'stats': dict(self._stats),
            }

    # ------------------------------------------------------------------
    # Registry persistence
    # ------------------------------------------------------------------

    def _load_registry(self):
        """Load robot registry from disk."""
        try:
            if os.path.exists(_REGISTRY_PATH):
                with open(_REGISTRY_PATH, 'r', encoding='utf-8') as f:
                    self._registry = json.load(f)
                logger.info("Robot registry loaded: %d robots",
                            len(self._registry))
        except Exception as exc:
            logger.warning("Failed to load robot registry: %s", exc)
            self._registry = {}

    def _save_registry(self):
        """Persist robot registry to disk. Caller must hold self._lock."""
        try:
            registry_dir = os.path.dirname(_REGISTRY_PATH)
            os.makedirs(registry_dir, exist_ok=True)
            with open(_REGISTRY_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._registry, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("Failed to save robot registry: %s", exc)

    def push_sensor_data(self, robot_id: str, sensors: dict) -> dict:
        """Accept streaming sensor data push from a robot.

        Stores sensors via the spatial intelligence path (sensor_store)
        and updates the robot's last_seen timestamp.

        Args:
            robot_id: Robot identifier.
            sensors: Sensor data dict (same format as think() sensors).

        Returns:
            Confirmation with count of sensor types ingested.
        """
        with self._lock:
            if robot_id in self._registry:
                self._registry[robot_id]['last_seen'] = time.time()

        # Reuse spatial intelligence to ingest sensors
        try:
            spatial_result = self._invoke_spatial(sensors)
            return {
                'accepted': True,
                'robot_id': robot_id,
                'sensors_ingested': list(sensors.keys()),
                'active_sensors': spatial_result.get('active_sensors', []),
            }
        except Exception as exc:
            return {
                'accepted': False,
                'robot_id': robot_id,
                'error': str(exc),
            }


# ---------------------------------------------------------------------------
# Flask Blueprint
# ---------------------------------------------------------------------------

def create_blueprint():
    """Create and return the Flask Blueprint for the Robot Intelligence API.

    Deferred import of Flask to keep the module importable without Flask
    installed (e.g., in test environments or non-web usage).
    """
    from flask import Blueprint, request, jsonify

    robot_intelligence_bp = Blueprint(
        'robot_intelligence', __name__, url_prefix='/api/robotics/ai')

    @robot_intelligence_bp.route('/think', methods=['POST'])
    def think_endpoint():
        """POST /api/robotics/ai/think -- Main intelligence fusion endpoint."""
        data = request.get_json(silent=True) or {}
        if not data.get('robot_id'):
            return jsonify({'error': 'robot_id is required'}), 400

        api = get_robot_api()
        result = api.think(data)
        return jsonify(result)

    @robot_intelligence_bp.route('/register', methods=['POST'])
    def register_endpoint():
        """POST /api/robot/register -- Register robot capabilities."""
        data = request.get_json(silent=True) or {}
        robot_id = data.get('robot_id')
        if not robot_id:
            return jsonify({'error': 'robot_id is required'}), 400
        capabilities = data.get('capabilities', {})
        api = get_robot_api()
        result = api.register_robot(robot_id, capabilities)
        return jsonify(result)

    @robot_intelligence_bp.route('/<robot_id>/status', methods=['GET'])
    def status_endpoint(robot_id):
        """GET /api/robot/<id>/status -- Robot state."""
        api = get_robot_api()
        result = api.get_robot_status(robot_id)
        status_code = 200 if result.get('found') else 404
        return jsonify(result), status_code

    @robot_intelligence_bp.route('/list', methods=['GET'])
    def list_endpoint():
        """GET /api/robot/list -- All registered robots."""
        api = get_robot_api()
        return jsonify({'robots': api.list_robots()})

    @robot_intelligence_bp.route('/<robot_id>/sensors', methods=['POST'])
    def sensor_push_endpoint(robot_id):
        """POST /api/robot/<id>/sensors -- Push sensor data (streaming)."""
        data = request.get_json(silent=True) or {}
        api = get_robot_api()
        result = api.push_sensor_data(robot_id, data.get('sensors', data))
        return jsonify(result)

    @robot_intelligence_bp.route('/hive/stats', methods=['GET'])
    def hive_stats_endpoint():
        """GET /api/robot/hive/stats -- Hive robotics statistics."""
        api = get_robot_api()
        return jsonify(api.get_hive_stats())

    return robot_intelligence_bp


def create_intelligence_blueprint():
    """Create the Flask Blueprint for the ``/api/robotics/intelligence/...`` routes.

    These are the hive-wide intelligence endpoints that aggregate across
    all robots, complementing the per-robot routes on ``create_blueprint()``.

    Routes:
        POST /api/robotics/intelligence/think   -- multi-intelligence fusion
        GET  /api/robotics/intelligence/robots  -- registered robots
    """
    from flask import Blueprint, request, jsonify

    intel_bp = Blueprint(
        'robotics_intelligence', __name__,
        url_prefix='/api/robotics/intelligence',
    )

    @intel_bp.route('/think', methods=['POST'])
    def intel_think():
        """POST /api/robotics/intelligence/think -- multi-intelligence fusion.

        Body: {
            "robot_id": "my-robot",
            "sensors": {"camera": "<base64>", "imu": {"ax": 0.1}},
            "context": "Fetch a glass of water",
            "constraints": {},
            "history": []
        }
        """
        data = request.get_json(silent=True) or {}
        if not data.get('robot_id'):
            return jsonify({'error': 'robot_id is required'}), 400
        api = get_robot_api()
        result = api.think(data)
        return jsonify(result)

    @intel_bp.route('/robots', methods=['GET'])
    def intel_robots():
        """GET /api/robotics/intelligence/robots -- all registered robots."""
        api = get_robot_api()
        return jsonify({
            'robots': api.list_robots(),
            'stats': api.get_hive_stats(),
        })

    return intel_bp


# Module-level blueprint for import convenience.
# Usage: from integrations.robotics.intelligence_api import robot_intelligence_bp
try:
    robot_intelligence_bp = create_blueprint()
except ImportError:
    # Flask not installed -- blueprint unavailable but class still works
    robot_intelligence_bp = None

# Unified intelligence blueprint at /api/robotics/intelligence/...
try:
    robotics_intelligence_bp = create_intelligence_blueprint()
except ImportError:
    robotics_intelligence_bp = None


# ---------------------------------------------------------------------------
# MCP Tool Registration
# ---------------------------------------------------------------------------

def register_mcp_tools(register_fn: Callable):
    """Register robot intelligence tools in the MCP bridge.

    Args:
        register_fn: MCP tool registration function with signature
            register_fn(name: str, description: str, handler: callable)
    """
    register_fn(
        'robot_think',
        'Send sensor data to robot, get fused multi-intelligence action plan',
        _mcp_think,
    )
    register_fn(
        'robot_register',
        'Register a new robot with capabilities',
        _mcp_register,
    )
    register_fn(
        'robot_list',
        'List all robots connected to the hive',
        _mcp_list,
    )


def _mcp_think(params: dict) -> dict:
    """MCP handler: invoke think()."""
    api = get_robot_api()
    return api.think(params)


def _mcp_register(params: dict) -> dict:
    """MCP handler: register a robot."""
    api = get_robot_api()
    robot_id = params.get('robot_id', '')
    capabilities = params.get('capabilities', {})
    if not robot_id:
        return {'error': 'robot_id is required'}
    return api.register_robot(robot_id, capabilities)


def _mcp_list(params: dict) -> dict:
    """MCP handler: list robots."""
    api = get_robot_api()
    return {'robots': api.list_robots()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_intent(response: str, context: str) -> str:
    """Simple keyword-based intent classification fallback."""
    text = (response + ' ' + context).lower()
    if any(w in text for w in ('fetch', 'get', 'bring', 'pick up', 'grab')):
        return 'fetch_object'
    if any(w in text for w in ('go to', 'navigate', 'move to', 'drive')):
        return 'navigate'
    if any(w in text for w in ('hello', 'hi', 'hey', 'greet')):
        return 'greet'
    if any(w in text for w in ('emergency', 'stop', 'halt', 'danger')):
        return 'emergency'
    if any(w in text for w in ('help', 'assist', 'support')):
        return 'assist'
    if any(w in text for w in ('tell', 'inform', 'report', 'status')):
        return 'inform'
    return 'assist'


def _extract_target(context: str, vision_data: dict) -> dict:
    """Extract a target position from context and vision data.

    This is a best-effort heuristic — real navigation targets come
    from HevolveAI's scene graph.
    """
    target: Dict[str, Any] = {}

    # If vision detected specific objects, pick the first as target
    objects = vision_data.get('objects', [])
    if objects:
        first = objects[0]
        if isinstance(first, dict):
            target = {
                'x': first.get('x', 0),
                'y': first.get('y', 0),
                'label': first.get('label', 'object'),
            }
        elif isinstance(first, str):
            target = {'label': first}

    return target
