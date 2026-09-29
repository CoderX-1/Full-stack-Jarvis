import tempfile
import unittest
from pathlib import Path

from device_hub import DeviceHub, DeviceState, SimulatedDeviceAdapter
from jarvis_mark2 import Mark2Runtime, READ_ONLY_MARK2_TOOLS


class DeviceHubTests(unittest.TestCase):
    def test_unpaired_runtime_reports_no_real_control(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            self.assertIn("Real device control is unavailable", runtime.execute("device_hub_status", {}))
            self.assertIn("device_hub_status", READ_ONLY_MARK2_TOOLS)

    def test_simulated_action_requires_observed_new_state(self):
        hub = DeviceHub()
        adapter = SimulatedDeviceAdapter("test-light", "power")
        hub.pair("test-light", adapter, {"power"})
        self.assertIn("Verified", hub.set_value("test-light", "power", "on"))
        self.assertEqual(adapter.state.value, "on")
        adapter.accept_commands = False
        self.assertIn("unverified", hub.set_value("test-light", "power", "off"))

    def test_missing_or_unapproved_device_never_receives_command(self):
        hub = DeviceHub()
        adapter = SimulatedDeviceAdapter("test-light", "power")
        hub.pair("test-light", adapter, {"power"})
        self.assertIn("not paired", hub.set_value("other", "power", "on"))
        self.assertIn("not allowed", hub.set_value("test-light", "unlock", "yes"))
        self.assertEqual(adapter.state.revision, 0)

    def test_identity_mismatch_blocks_action(self):
        hub = DeviceHub()
        adapter = SimulatedDeviceAdapter("different-device", "power")
        hub.pair("test-light", adapter, {"power"})
        self.assertIn("identity mismatch", hub.set_value("test-light", "power", "on"))
        self.assertEqual(adapter.state.revision, 0)

    def test_post_action_identity_mismatch_is_unknown(self):
        class SwappingAdapter(SimulatedDeviceAdapter):
            def set_value(self, identity, capability, value):
                self.state = DeviceState("other", capability, value, self.state.revision + 1)

        hub = DeviceHub()
        hub.pair("test-light", SwappingAdapter("test-light", "power"), {"power"})
        self.assertIn("unknown", hub.set_value("test-light", "power", "on"))


if __name__ == "__main__":
    unittest.main()
