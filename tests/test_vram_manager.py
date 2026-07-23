"""NFT — VRAMManager allocation refusal on oversize claim.

Asserts the contract fixed in 3a40a65:

- allocate(tool) returns False when free VRAM < tool's min_vram budget
- can_fit(tool) returns False in the same case
- allocate() does NOT mutate _allocations on refusal
- detect_gpu() mock with 8GB total/free refuses a 10GB claim

These tests guard Stage A symptom #3 from the master orchestrator run.
Root-cause class: shallow-signal VRAM accounting — the allocate() path
must honor the free-vram bound, not just book-keep.
"""
import unittest
from unittest.mock import patch

from integrations.service_tools.vram_manager import (
    VRAMManager,
    VRAM_BUDGETS,
)


class VRAMAllocationRefusalTests(unittest.TestCase):
    def setUp(self):
        # Fresh manager per test — the module-level singleton has
        # test-order-dependent state we don't want polluting here.
        self.mgr = VRAMManager()

    def _mock_gpu(self, total_gb: float, free_gb: float):
        """Return a detect_gpu patch context for a given GPU shape."""
        fake_info = {
            "name": "MockGPU",
            "total_gb": total_gb,
            "free_gb": free_gb,
            "cuda_available": True,
        }
        return patch.object(self.mgr, "detect_gpu", return_value=fake_info)

    def test_can_fit_refuses_oversize_claim_on_8gb_gpu(self):
        """10GB-budget tool on 8GB GPU: can_fit must be False."""
        # Register a synthetic 10GB tool so we don't depend on live catalog.
        VRAM_BUDGETS["_test_10gb_tool"] = (10.0, 9.0)  # min_vram=10, size=9
        try:
            with self._mock_gpu(total_gb=8.0, free_gb=8.0):
                self.assertFalse(
                    self.mgr.can_fit("_test_10gb_tool"),
                    "can_fit must return False when min_vram > free_vram",
                )
        finally:
            VRAM_BUDGETS.pop("_test_10gb_tool", None)

    def test_allocate_returns_false_on_oversize_claim(self):
        """allocate() must return False when can_fit would be False."""
        VRAM_BUDGETS["_test_10gb_tool"] = (10.0, 9.0)
        try:
            with self._mock_gpu(total_gb=8.0, free_gb=8.0):
                ok = self.mgr.allocate("_test_10gb_tool")
                self.assertFalse(ok, "allocate must return False on OOM")
                self.assertNotIn(
                    "_test_10gb_tool",
                    self.mgr.get_allocations(),
                    "Refused allocation must not mutate the ledger",
                )
        finally:
            VRAM_BUDGETS.pop("_test_10gb_tool", None)

    def test_allocate_returns_true_when_it_fits(self):
        """Sanity: 2GB tool on 8GB GPU fits."""
        VRAM_BUDGETS["_test_2gb_tool"] = (2.0, 1.5)
        try:
            with self._mock_gpu(total_gb=8.0, free_gb=8.0):
                ok = self.mgr.allocate("_test_2gb_tool")
                self.assertTrue(ok)
                self.assertIn("_test_2gb_tool", self.mgr.get_allocations())
                self.assertAlmostEqual(
                    self.mgr.get_allocations()["_test_2gb_tool"],
                    1.5,
                    places=2,
                )
        finally:
            VRAM_BUDGETS.pop("_test_2gb_tool", None)

    def test_allocate_idempotent_on_repeat(self):
        """Re-allocating an already-allocated tool returns True without drift."""
        VRAM_BUDGETS["_test_2gb_tool"] = (2.0, 1.5)
        try:
            with self._mock_gpu(total_gb=8.0, free_gb=8.0):
                self.assertTrue(self.mgr.allocate("_test_2gb_tool"))
                # Second call must be True (already-allocated shortcut)
                self.assertTrue(self.mgr.allocate("_test_2gb_tool"))
                self.assertEqual(len(self.mgr.get_allocations()), 1)
        finally:
            VRAM_BUDGETS.pop("_test_2gb_tool", None)

    def test_allocate_refuses_when_no_gpu(self):
        """No GPU → allocate must refuse any tool that has a budget."""
        VRAM_BUDGETS["_test_any_tool"] = (2.0, 1.5)
        try:
            nogpu_info = {
                "name": None,
                "total_gb": 0.0,
                "free_gb": 0.0,
                "cuda_available": False,
            }
            with patch.object(self.mgr, "detect_gpu", return_value=nogpu_info):
                ok = self.mgr.allocate("_test_any_tool")
                self.assertFalse(ok)
                self.assertNotIn("_test_any_tool", self.mgr.get_allocations())
        finally:
            VRAM_BUDGETS.pop("_test_any_tool", None)

    def test_release_after_refusal_is_safe(self):
        """Releasing a tool that was refused is a no-op, not a KeyError."""
        VRAM_BUDGETS["_test_10gb_tool"] = (10.0, 9.0)
        try:
            with self._mock_gpu(total_gb=8.0, free_gb=8.0):
                self.assertFalse(self.mgr.allocate("_test_10gb_tool"))
                # Must not raise
                self.mgr.release("_test_10gb_tool")
        finally:
            VRAM_BUDGETS.pop("_test_10gb_tool", None)

    def test_unknown_tool_fits_by_default(self):
        """Unknown tool (not in VRAM_BUDGETS) is assumed to fit — matches
        the existing default-allow behavior."""
        with self._mock_gpu(total_gb=8.0, free_gb=8.0):
            self.assertTrue(self.mgr.can_fit("_not_in_budget_table"))
            ok = self.mgr.allocate("_not_in_budget_table")
            self.assertTrue(ok)
            # Unknown tool allocates 0 GB (no budget to subtract)
            self.assertEqual(
                self.mgr.get_allocations().get("_not_in_budget_table"), 0.0
            )

    def _mock_metal_gpu(self, total_gb: float, free_gb: float):
        """Return a detect_gpu patch shaped like macOS Metal detection
        (cuda_available=False, metal_available=True) — the real return
        shape from detect_gpu() on a Mac, distinct from _mock_gpu's
        NVIDIA-shaped fake."""
        fake_info = {
            "name": "Apple Metal (Apple Silicon)",
            "total_gb": total_gb,
            "free_gb": free_gb,
            "cuda_available": False,
            "metal_available": True,
        }
        return patch.object(self.mgr, "detect_gpu", return_value=fake_info)

    def test_metal_with_free_memory_can_fit(self):
        """Regression: macOS Metal previously reported free_gb=0.0
        unconditionally, so can_fit()/get_free_vram() rejected every
        GPU-gated model on every Mac regardless of real available memory.
        A Metal GPU with real free memory must fit a budget within it."""
        VRAM_BUDGETS["_test_1gb_tool"] = (1.0, 0.5)
        try:
            with self._mock_metal_gpu(total_gb=16.0, free_gb=8.0):
                self.assertEqual(self.mgr.get_free_vram(), 8.0)
                self.assertTrue(self.mgr.can_fit("_test_1gb_tool"))
                self.assertTrue(self.mgr.allocate("_test_1gb_tool"))
        finally:
            VRAM_BUDGETS.pop("_test_1gb_tool", None)

    def test_metal_oversize_claim_still_refused(self):
        """Metal GPU with genuinely insufficient free memory must still
        refuse — the fix isn't a blanket allow, it's a real measurement."""
        VRAM_BUDGETS["_test_10gb_tool"] = (10.0, 9.0)
        try:
            with self._mock_metal_gpu(total_gb=8.0, free_gb=1.0):
                self.assertFalse(self.mgr.can_fit("_test_10gb_tool"))
                self.assertFalse(self.mgr.allocate("_test_10gb_tool"))
        finally:
            VRAM_BUDGETS.pop("_test_10gb_tool", None)

    def test_metal_offload_mode_not_forced_cpu_only_when_it_fits(self):
        """suggest_offload_mode() had the same cuda_available-only gate —
        must not force cpu_only on Metal when the model genuinely fits."""
        with self._mock_metal_gpu(total_gb=16.0, free_gb=8.0):
            self.assertNotEqual(
                self.mgr.suggest_offload_mode("_not_in_budget_table"),
                "cpu_only",
            )


if __name__ == "__main__":
    unittest.main()
