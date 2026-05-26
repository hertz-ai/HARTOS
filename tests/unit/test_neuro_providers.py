"""Invariants for the neuro / biometric provider registry + adapter +
discovery.

Locks the properties Nunba's UI and any downstream agent rely on:
  - Yneuro present and contact-gated (not silently integrated)
  - Registry spans multiple form factors so "auto-integrate for various
    form factors" is meaningful
  - choose_adapter() routes correctly per provider transport
  - ContactGatedAdapter raises PermissionError pointing at the vendor
  - Stub adapters raise NotImplementedError with install-hint text so
    a dev knows exactly which pip package to reach for
  - scan() is fail-soft — no optional dep, no crash
"""

import unittest

from integrations.providers.neuro_providers import (
    NEURO_REGISTRY,
    FormFactor,
    NeuroProvider,
    ProviderStatus,
    SignalType,
    Transport,
    get_provider,
    list_providers,
)
from integrations.providers.neuro_adapter import (
    BLEAdapter,
    ContactGatedAdapter,
    LSLAdapter,
    NeuroAdapter,
    RESTAdapter,
    USBHIDAdapter,
    USBSerialAdapter,
    WebSocketAdapter,
    _SDKUnavailable,
    choose_adapter,
    register_adapter,
)
from integrations.providers import neuro_discovery


# ─── Registry invariants ──────────────────────────────────────────────

class TestYneuroEntry(unittest.TestCase):
    """The headline provider — confirm it's honest about what we don't
    know and points the developer at the right email."""

    def test_yneuro_present(self):
        self.assertIn('yneuro', NEURO_REGISTRY)

    def test_yneuro_contact_gated(self):
        p = NEURO_REGISTRY['yneuro']
        self.assertEqual(p.status, ProviderStatus.CONTACT_REQUIRED)

    def test_yneuro_contact_email_set(self):
        p = NEURO_REGISTRY['yneuro']
        self.assertEqual(p.contact_email, 'hello@yneuro.com')

    def test_yneuro_website(self):
        p = NEURO_REGISTRY['yneuro']
        self.assertEqual(p.website, 'https://www.yneuro.com/')

    def test_yneuro_signals_brainwave_fingerprint(self):
        p = NEURO_REGISTRY['yneuro']
        self.assertIn(SignalType.BRAINWAVE_FINGERPRINT, p.signals)

    def test_yneuro_form_factor_unknown(self):
        # We don't know their hardware form factor — don't pretend
        p = NEURO_REGISTRY['yneuro']
        self.assertEqual(p.form_factor, FormFactor.UNKNOWN)

    def test_yneuro_notes_explain_status(self):
        p = NEURO_REGISTRY['yneuro']
        self.assertIn('SDK', p.notes)


class TestRegistryBreadth(unittest.TestCase):
    """The registry must span multiple form factors and transports so
    'auto-integrate for various form factors' has real content."""

    def test_at_least_four_form_factors(self):
        factors = {p.form_factor for p in NEURO_REGISTRY.values()}
        self.assertGreaterEqual(len(factors), 4)

    def test_at_least_four_transports_present(self):
        transports = set()
        for p in NEURO_REGISTRY.values():
            transports.update(p.transport)
        self.assertGreaterEqual(len(transports), 4)

    def test_registry_key_matches_id(self):
        for key, p in NEURO_REGISTRY.items():
            self.assertEqual(key, p.id, f'key/id mismatch on {key}')

    def test_get_provider_unknown_returns_none(self):
        self.assertIsNone(get_provider('nonexistent'))

    def test_get_provider_yneuro_returns_entry(self):
        p = get_provider('yneuro')
        self.assertIsNotNone(p)
        self.assertEqual(p.id, 'yneuro')

    def test_at_least_one_public_sdk_provider(self):
        public = [p for p in NEURO_REGISTRY.values()
                  if p.status == ProviderStatus.PUBLIC_SDK]
        self.assertGreaterEqual(len(public), 3)


class TestRegistryFilters(unittest.TestCase):
    def test_filter_by_form_factor_headset(self):
        headsets = list_providers(form_factor=FormFactor.HEADSET_EEG)
        self.assertTrue(
            all(p.form_factor == FormFactor.HEADSET_EEG for p in headsets)
        )
        # Muse, Neurosity, Emotiv at minimum
        self.assertGreaterEqual(len(headsets), 3)

    def test_filter_by_signal_hrv(self):
        hrv = list_providers(signal=SignalType.HRV)
        self.assertTrue(all(SignalType.HRV in p.signals for p in hrv))
        ids = {p.id for p in hrv}
        self.assertIn('whoop', ids)
        self.assertIn('oura', ids)

    def test_filter_by_status_contact_required_includes_yneuro(self):
        gated = list_providers(status=ProviderStatus.CONTACT_REQUIRED)
        self.assertIn('yneuro', {p.id for p in gated})

    def test_filter_by_transport_ble_includes_muse(self):
        ble = list_providers(transport=Transport.BLE)
        self.assertIn('muse', {p.id for p in ble})


# ─── Adapter factory ──────────────────────────────────────────────────

class TestAdapterFactory(unittest.TestCase):
    def test_yneuro_returns_contact_gated_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['yneuro'])
        self.assertIsInstance(ad, ContactGatedAdapter)

    def test_yneuro_connect_raises_permission_with_email(self):
        ad = choose_adapter(NEURO_REGISTRY['yneuro'])
        with self.assertRaises(PermissionError) as cm:
            ad.connect()
        self.assertIn('hello@yneuro.com', str(cm.exception))

    def test_yneuro_read_raises_permission(self):
        ad = choose_adapter(NEURO_REGISTRY['yneuro'])
        with self.assertRaises(PermissionError):
            ad.read_signal(SignalType.BRAINWAVE_FINGERPRINT)

    def test_muse_returns_ble_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['muse'])
        self.assertIsInstance(ad, BLEAdapter)

    def test_whoop_returns_rest_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['whoop'])
        self.assertIsInstance(ad, RESTAdapter)

    def test_oura_returns_rest_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['oura'])
        self.assertIsInstance(ad, RESTAdapter)

    def test_neurosity_returns_websocket_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['neurosity'])
        self.assertIsInstance(ad, WebSocketAdapter)

    def test_emotiv_returns_websocket_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['emotiv'])
        self.assertIsInstance(ad, WebSocketAdapter)

    def test_openbci_returns_usb_serial_adapter(self):
        ad = choose_adapter(NEURO_REGISTRY['openbci'])
        self.assertIsInstance(ad, USBSerialAdapter)

    def test_stub_adapter_raises_not_implemented_on_connect(self):
        ad = choose_adapter(NEURO_REGISTRY['muse'])
        with self.assertRaises(NotImplementedError) as cm:
            ad.connect()
        # Install hint must name the pip package
        self.assertIn('bleak', str(cm.exception))

    def test_register_adapter_overrides_default(self):
        class _FakeBLEAdapter(NeuroAdapter):
            def connect(self, **kwargs):
                self._connected = True
                return True
            def read_signal(self, signal, duration_s=1.0):
                return None
            def disconnect(self):
                self._connected = False

        try:
            register_adapter(Transport.BLE, _FakeBLEAdapter)
            ad = choose_adapter(NEURO_REGISTRY['muse'])
            self.assertIsInstance(ad, _FakeBLEAdapter)
            self.assertTrue(ad.connect())
        finally:
            # Restore the real stub so other tests aren't polluted
            register_adapter(Transport.BLE, BLEAdapter)

    def test_register_adapter_rejects_non_subclass(self):
        with self.assertRaises(TypeError):
            register_adapter(Transport.BLE, int)  # not a NeuroAdapter


# ─── Discovery fail-soft ──────────────────────────────────────────────

class TestDiscovery(unittest.TestCase):
    """Discovery must never raise — missing deps degrade to empty list."""

    def test_scan_returns_list_without_hardware(self):
        result = neuro_discovery.scan(ble_timeout_s=0.1)
        self.assertIsInstance(result, list)

    def test_scan_ble_only(self):
        result = neuro_discovery.scan(
            include_ble=True, include_lsl=False, include_usb=False,
            ble_timeout_s=0.1,
        )
        self.assertIsInstance(result, list)

    def test_scan_all_disabled_returns_empty(self):
        result = neuro_discovery.scan(
            include_ble=False, include_lsl=False, include_usb=False,
        )
        self.assertEqual(result, [])

    def test_known_providers_summary_includes_all_entries(self):
        summary = neuro_discovery.known_providers_summary()
        self.assertEqual(len(summary), len(NEURO_REGISTRY))

    def test_summary_entry_shape(self):
        summary = neuro_discovery.known_providers_summary()
        for entry in summary:
            for key in ('id', 'name', 'form_factor', 'status',
                        'signals', 'transports'):
                self.assertIn(key, entry, f'{key} missing from entry {entry}')

    def test_summary_yneuro_carries_contact_email(self):
        summary = neuro_discovery.known_providers_summary()
        yn = next(e for e in summary if e['id'] == 'yneuro')
        self.assertEqual(yn['contact_email'], 'hello@yneuro.com')
        self.assertEqual(yn['status'], ProviderStatus.CONTACT_REQUIRED.value)


if __name__ == '__main__':
    unittest.main()
