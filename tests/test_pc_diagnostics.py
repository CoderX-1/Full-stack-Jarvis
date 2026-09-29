import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis_mark2 import Mark2Runtime, READ_ONLY_MARK2_TOOLS
from pc_diagnostics import snapshot


class SensorStub:
    def cpu_percent(self, interval):
        assert interval >= 0.1
        return 12.5

    def virtual_memory(self):
        return SimpleNamespace(percent=40.0, available=600, total=1000)

    def disk_usage(self, _path):
        return SimpleNamespace(percent=25.0, free=750, total=1000)

    def sensors_battery(self):
        return None


class PcDiagnosticsTests(unittest.TestCase):
    def test_reports_measured_values_and_unavailable_battery(self):
        with tempfile.TemporaryDirectory() as folder:
            report = json.loads(snapshot(Path(folder), SensorStub()))
        self.assertEqual(report["cpu_percent"], 12.5)
        self.assertEqual(report["memory"]["available_bytes"], 600)
        self.assertEqual(report["workspace_drive"]["free_bytes"], 750)
        self.assertEqual(report["battery"]["status"], "unavailable_or_no_battery")
        self.assertIn(report["temperature"]["status"],
                      {"not_available_in_core_adapter", "not_measured"})

    def test_measurement_failure_is_not_an_invented_value(self):
        class Broken(SensorStub):
            def virtual_memory(self):
                raise OSError("sensor unavailable")

        with tempfile.TemporaryDirectory() as folder:
            self.assertIn("error: PC measurement failed", snapshot(Path(folder), Broken()))

    def test_runtime_exposes_read_only_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            self.assertIn("pc_diagnostics", READ_ONLY_MARK2_TOOLS)
            report = json.loads(runtime.execute("pc_diagnostics", {}))
            self.assertIn("memory", report)
            self.assertIn("workspace_drive", report)


if __name__ == "__main__":
    unittest.main()
