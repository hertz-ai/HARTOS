"""Multimodal user input (video + audio) rides the EXISTING SensorReading schema.

REUSE, not a parallel path: video (camera / desktop / other-user stream) and
audio flow through the SAME ingest_sensor_batch / submit_sensor_frame →
HevolveAI /v1/sensor/ingest. The schema now carries `audio` + a `stream_source`
classifier so HevolveAI applies the proper learning signal per origin.

    python -m pytest tests/unit/test_multimodal_sensor_ingest.py --noconftest -q
"""
from integrations.robotics.sensor_model import (
    SensorReading, validate_reading, SENSOR_SCHEMAS, DEFAULT_TTL)


def test_audio_is_a_first_class_sensor_type():
    assert 'audio' in SENSOR_SCHEMAS
    assert 'audio' in DEFAULT_TTL
    assert 'stream_source' in SENSOR_SCHEMAS['audio']['optional']
    r = SensorReading(sensor_id='mic_0', sensor_type='audio',
                      data={'pcm_base64': 'AAA=', 'sample_rate': 16000,
                            'channels': 1, 'stream_source': 'mic'})
    assert validate_reading(r) is True
    assert r.to_dict()['data']['stream_source'] == 'mic'


def test_video_stream_source_classification():
    # SAME camera sensor_type, classified by origin for proper learning signals
    for origin in ('camera', 'desktop', 'peer_user', 'meeting'):
        r = SensorReading(sensor_id='cam_0', sensor_type='camera',
                          data={'frame_base64': 'AAA=', 'width': 640,
                                'height': 480, 'stream_source': origin})
        assert validate_reading(r) is True
        assert r.to_dict()['data']['stream_source'] == origin
    assert 'stream_source' in SENSOR_SCHEMAS['camera']['optional']


def test_multimodal_readings_share_the_one_transport():
    # both serialize to the dict ingest_sensor_batch forwards — ONE path
    readings = [
        SensorReading(sensor_id='cam_0', sensor_type='camera',
                      data={'frame_base64': 'x', 'stream_source': 'desktop'}),
        SensorReading(sensor_id='mic_0', sensor_type='audio',
                      data={'wav_base64': 'y', 'stream_source': 'mic'}),
    ]
    batch = [r.to_dict() for r in readings]
    assert all(set(d) >= {'sensor_id', 'sensor_type', 'data', 'source'} for d in batch)
    assert {d['sensor_type'] for d in batch} == {'camera', 'audio'}
