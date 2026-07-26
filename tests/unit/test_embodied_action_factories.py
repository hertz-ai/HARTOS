"""Embodied / VLA action factories on RobotAction (Qwen-RobotSuite-class verbs).

Behavioural: build via the factory, assert action_type + params, and that the
to_dict() output is the plain dict WorldModelBridge.send_action() forwards to
HevolveAI. Callers must construct these ONE way (the factory), never inline dicts.

    python -m pytest tests/unit/test_embodied_action_factories.py --noconftest -q
"""
from integrations.robotics.action_model import RobotAction


def test_vla_instruct():
    a = RobotAction.vla_instruct('pick up the red cube',
                                 observation={'rgb': 'b64'}, horizon=4)
    assert a.action_type == 'vla_instruct'
    assert a.params['instruction'] == 'pick up the red cube'
    assert a.params['horizon'] == 4
    assert a.params['observation'] == {'rgb': 'b64'}
    # round-trips through the transport contract
    assert RobotAction.from_dict(a.to_dict()).action_type == 'vla_instruct'


def test_vla_instruct_defaults_empty_observation():
    a = RobotAction.vla_instruct('go home')
    assert a.params['observation'] == {}
    assert a.params['horizon'] == 8


def test_action_chunk():
    a = RobotAction.action_chunk([{'dx': 0.1}, {'dx': 0.2}], control_hz=20)
    assert a.action_type == 'action_chunk'
    assert a.params['control_hz'] == 20.0
    assert a.params['chunk'] == [{'dx': 0.1}, {'dx': 0.2}]


def test_end_effector_delta_gripper_omitted_when_none():
    a = RobotAction.end_effector_delta(dx=0.05, dz=-0.02, gripper=0.8)
    assert a.action_type == 'end_effector_delta'
    assert a.params['dx'] == 0.05 and a.params['dz'] == -0.02
    assert a.params['gripper'] == 0.8
    b = RobotAction.end_effector_delta(dyaw=0.1)
    assert 'gripper' not in b.params  # omitted, not None


def test_world_model_rollout_is_language_conditioned():
    # RobotWorld: language instruction (+ observation) → predicted future
    a = RobotAction.world_model_rollout('imagine picking up the cube',
                                        observation={'rgb': 'x'}, horizon=12)
    assert a.action_type == 'world_model_rollout'
    assert a.params['instruction'] == 'imagine picking up the cube'
    assert a.params['horizon'] == 12
    assert a.params['observation'] == {'rgb': 'x'}


def test_manip_action_80d_masked():
    # RobotManip canonical 80-D masked state-action
    a = RobotAction.manip_action([0.0] * 80, mask=[1] * 29 + [0] * 51)
    assert a.action_type == 'manip_action'
    assert len(a.params['state_action']) == 80
    assert len(a.params['mask']) == 80
    b = RobotAction.manip_action([0.0] * 80)
    assert 'mask' not in b.params  # omitted when None


def test_navigate_defaults_to_eight_waypoints():
    # RobotNav outputs 8 (x, y, theta) waypoints
    a = RobotAction.navigate('go to the kitchen')
    assert a.action_type == 'navigate'
    assert a.params['goal'] == 'go to the kitchen'
    assert a.params['num_waypoints'] == 8


def test_all_factories_are_bridge_serializable():
    for a in (RobotAction.vla_instruct('go'),
              RobotAction.action_chunk([]),
              RobotAction.end_effector_delta(dx=0.1),
              RobotAction.world_model_rollout('imagine'),
              RobotAction.manip_action([0.0] * 80),
              RobotAction.navigate('here')):
        d = a.to_dict()
        assert set(d) >= {'type', 'target', 'params', 'source'}
        assert isinstance(d['params'], dict)
