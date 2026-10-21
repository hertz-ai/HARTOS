"""core.platform_paths.free_bytes: the one free-disk reading at a path (D27).

Three copies with three rules existed (duplicates inventory D27):
user_file_store._free_bytes made its root first, reel_studio's
pipeline.free_bytes walked up to the nearest existing folder and made
nothing (amendment A11b), trends.daily_job._free_bytes read venv_root.  The
canonical rule is pipeline's, now in core so model_storage (an npm setup's
disk check, amendment A14) and the reel pipeline read the same answer.
shutil.disk_usage is the boundary.
"""
import os
import shutil
from collections import namedtuple

import pytest

Usage = namedtuple('Usage', 'total used free')


@pytest.fixture
def measured(monkeypatch):
    asked = []

    def disk_usage(path):
        asked.append(path)
        return Usage(100, 40, 60)
    monkeypatch.setattr(shutil, 'disk_usage', disk_usage)
    return asked


def test_a_folder_that_does_not_exist_yet_is_measured_at_its_nearest_existing_parent(
        tmp_path, measured):
    from core.platform_paths import free_bytes
    target = tmp_path / 'models' / 'reel_motion' / 'node_modules'
    assert free_bytes(str(target)) == 60
    assert measured == [str(tmp_path)]
    assert not (tmp_path / 'models').exists(), 'measuring creates nothing (A11b)'


def test_an_existing_folder_is_measured_itself(tmp_path, measured):
    from core.platform_paths import free_bytes
    assert free_bytes(str(tmp_path)) == 60
    assert measured == [str(tmp_path)]


def test_a_file_path_is_measured_at_its_folder(tmp_path, measured):
    from core.platform_paths import free_bytes
    f = tmp_path / 'x.bin'
    f.write_bytes(b'x')
    assert free_bytes(str(f)) == 60
    assert measured == [str(tmp_path)]


def test_a_relative_path_is_measured_from_the_working_folder(tmp_path, monkeypatch, measured):
    from core.platform_paths import free_bytes
    monkeypatch.chdir(tmp_path)
    assert free_bytes(os.path.join('not', 'here')) == 60
    assert measured == [str(tmp_path)]


def test_the_reel_pipeline_reads_the_core_rule():
    """One rule: the pipeline's name is the core function, not a copy."""
    from core import platform_paths
    from integrations.reel_studio import pipeline
    assert pipeline.free_bytes is platform_paths.free_bytes
