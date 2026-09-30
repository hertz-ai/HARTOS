"""The bloom-order guard must still catch a real reorder.

test_native_shell_bloom_wiring.py::test_bloom_is_the_bottom_element reads the push
order out of comp_core.rs's build_frame_elements. It went red on a correct order once
(release run 35917397640) because it anchored on `bloom_mut()`, which the builder now
also calls at its top to age the frosted-crop pool. The anchor moved to the real push
(`bloom_mut().get(`), and this is the other half of that change: the guard against a
guard that reads nothing. The bloom block is moved above the window loop in a COPY of
the source, and the guard must fail on it. It never touches the repo file.

Run:
  pytest tests/unit/test_bloom_guard_mutation.py -v
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.unit import test_native_shell_bloom_wiring as guard  # noqa: E402


def _mutated_comp_core():
    src = guard._read("comp_core.rs")
    body_start = src.index("pub fn build_frame_elements")
    # The bloom push block: from its heading comment to the publish that follows it.
    bloom_start = src.index("    // ── 4. BLOOM BACKDROP", body_start)
    bloom_end = src.index("    NATIVE_CHROME_EMITTED.store(", bloom_start)
    block = src[bloom_start:bloom_end]
    assert "bloom_mut().get(" in block, "the mutation must move the real push"
    without = src[:bloom_start] + src[bloom_end:]
    # Above the window loop: the toplevels' Surface pushes now come AFTER it, which
    # would paint every window underneath the backdrop.
    windows_at = without.index("    let windows: Vec<Window> = state.space().elements()", body_start)
    return without[:windows_at] + block + without[windows_at:]


def test_the_guard_fails_when_the_bloom_push_moves_above_the_windows(monkeypatch):
    mutated = _mutated_comp_core()
    monkeypatch.setattr(guard, "_read", lambda name: mutated if name == "comp_core.rs" else guard._read(name))
    with pytest.raises(AssertionError, match="pushed after the bloom backdrop"):
        guard.test_bloom_is_the_bottom_element()


def test_the_guard_passes_on_the_real_order():
    # The other direction, in the same file, so a broken anchor cannot pass by
    # raising on everything.
    guard.test_bloom_is_the_bottom_element()
