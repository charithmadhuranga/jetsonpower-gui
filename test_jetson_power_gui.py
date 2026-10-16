#!/usr/bin/env python3
"""
Test suite for Jetson Power Modes GUI
Tests core logic without launching the full tkinter GUI (avoids X11 display issues).
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add project directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jetson_power_gui as j


class TestBoardDetection(unittest.TestCase):
    """Test board detection from device tree."""

    def _make_board_file(self, content):
        fd, path = tempfile.mkstemp(suffix=".model")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_detect_board_orin_nano(self):
        path = self._make_board_file("NVIDIA Jetson Orin Nano Developer Kit\x00")
        try:
            with patch("jetson_power_gui.BOARD_FILE", path):
                result = j.detect_board()
        finally:
            os.unlink(path)
        self.assertEqual(result, "NVIDIA Jetson Orin Nano Developer Kit")

    def test_detect_board_agx_xavier(self):
        path = self._make_board_file("NVIDIA Jetson AGX Xavier Developer Kit\x00")
        try:
            with patch("jetson_power_gui.BOARD_FILE", path):
                result = j.detect_board()
        finally:
            os.unlink(path)
        self.assertEqual(result, "NVIDIA Jetson AGX Xavier Developer Kit")

    def test_detect_board_xavier_nx(self):
        path = self._make_board_file("NVIDIA Jetson Xavier NX Developer Kit\x00")
        try:
            with patch("jetson_power_gui.BOARD_FILE", path):
                result = j.detect_board()
        finally:
            os.unlink(path)
        self.assertEqual(result, "NVIDIA Jetson Xavier NX Developer Kit")

    def test_detect_board_unknown(self):
        with patch("jetson_power_gui.BOARD_FILE", "/nonexistent/path"):
            result = j.detect_board()
        self.assertEqual(result, "Unknown Jetson Board")


class TestPowerModeParsing(unittest.TestCase):
    """Test parsing of /etc/nvpmodel.conf for various board configurations."""

    XAVIER_NX_CONF = """\
< POWER_MODEL ID=0 NAME=MODE_15W_2CORE >
CPU_ONLINE CORE_0 1
< POWER_MODEL ID=1 NAME=MODE_15W_4CORE >
CPU_ONLINE CORE_0 1
< POWER_MODEL ID=2 NAME=MODE_20W_6CORE >
CPU_ONLINE CORE_0 1
"""

    ORIN_CONF = """\
< POWER_MODEL ID=0 NAME=MODE_15W_ALL_CORE >
CPU_ONLINE CORE_0 1
< POWER_MODEL ID=1 NAME=MODE_7W_ALL_CORE >
CPU_ONLINE CORE_0 1
< POWER_MODEL ID=2 NAME=MODE_25W_ALL_CORE >
CPU_ONLINE CORE_0 1
"""

    def _write_temp_conf(self, content):
        fd, path = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_parse_xavier_nx_modes(self):
        path = self._write_temp_conf(self.XAVIER_NX_CONF)
        try:
            modes = j.parse_power_modes(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(modes), 3)
        self.assertEqual(modes[0]["id"], 0)
        self.assertEqual(modes[0]["name"], "MODE_15W_2CORE")
        self.assertEqual(modes[0]["watts"], 15)
        self.assertEqual(modes[0]["cores"], 2)
        self.assertEqual(modes[2]["name"], "MODE_20W_6CORE")
        self.assertEqual(modes[2]["watts"], 20)
        self.assertEqual(modes[2]["cores"], 6)

    def test_parse_orin_modes(self):
        path = self._write_temp_conf(self.ORIN_CONF)
        try:
            modes = j.parse_power_modes(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(modes), 3)
        self.assertEqual(modes[0]["name"], "MODE_15W_ALL_CORE")
        self.assertEqual(modes[0]["watts"], 15)
        self.assertIsNone(modes[0]["cores"])  # "ALL_CORE" has no number
        self.assertEqual(modes[1]["name"], "MODE_7W_ALL_CORE")
        self.assertEqual(modes[1]["watts"], 7)

    @patch("os.path.exists", return_value=False)
    def test_parse_missing_config(self, mock_exists):
        modes = j.parse_power_modes("/nonexistent/path.conf")
        self.assertEqual(modes, [])


class TestCurrentModeQuery(unittest.TestCase):
    """Test parsing of nvpmodel -q output."""

    XAVIER_NX_OUTPUT = """\
NV Power Mode: MODE_20W_6CORE
8
NVPM ERROR: Error opening /sys/kernel/nvpmodel_emc_cap/emc_iso_cap: 13
"""

    ORIN_OUTPUT = """\
NV Power Mode: MODE_25W_ALL_CORE
2
NVPM ERROR: some error
"""

    @patch("jetson_power_gui.run_cmd", return_value=(XAVIER_NX_OUTPUT, "", True))
    def test_get_current_mode_xavier_nx(self, mock_run):
        name, mode_id = j.get_current_mode()
        self.assertEqual(name, "MODE_20W_6CORE")
        self.assertEqual(mode_id, 8)

    @patch("jetson_power_gui.run_cmd", return_value=(ORIN_OUTPUT, "", True))
    def test_get_current_mode_orin(self, mock_run):
        name, mode_id = j.get_current_mode()
        self.assertEqual(name, "MODE_25W_ALL_CORE")
        self.assertEqual(mode_id, 2)

    @patch("jetson_power_gui.run_cmd", return_value=("", "", True))
    def test_get_current_mode_empty(self, mock_run):
        name, mode_id = j.get_current_mode()
        self.assertIsNone(name)
        self.assertIsNone(mode_id)


class TestThermalZones(unittest.TestCase):
    """Test thermal zone reading using temp files."""

    def _make_thermal_zone(self, base_dir, zone_name, zone_type, temp_millideg):
        zone_dir = os.path.join(base_dir, zone_name)
        os.makedirs(zone_dir)
        with open(os.path.join(zone_dir, "type"), "w") as f:
            f.write(zone_type)
        with open(os.path.join(zone_dir, "temp"), "w") as f:
            f.write(str(temp_millideg))

    def test_read_thermal_zones(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            thermal_dir = os.path.join(tmpdir, "thermal")
            os.makedirs(thermal_dir)

            # Patch THERMAL_DIR to point to our temp dir
            with patch("jetson_power_gui.THERMAL_DIR", thermal_dir):
                self._make_thermal_zone(thermal_dir, "thermal_zone0", "CPU-therm", 55000)
                self._make_thermal_zone(thermal_dir, "thermal_zone1", "GPU-therm", 52000)
                self._make_thermal_zone(thermal_dir, "thermal_zone2", "AUX-therm", 50000)

                temps = j.read_thermal_zones()
                self.assertEqual(len(temps), 3)
                self.assertEqual(temps[0], ("thermal_zone0", "CPU-therm", 55.0))
                self.assertEqual(temps[1], ("thermal_zone1", "GPU-therm", 52.0))
                self.assertEqual(temps[2], ("thermal_zone2", "AUX-therm", 50.0))


class TestFrequencyReading(unittest.TestCase):
    """Test CPU/GPU/EMC frequency reading with unit detection."""

    @patch("jetson_power_gui.read_first_available", return_value=1420800)
    def test_read_cpu_freq_hz(self, mock_avail):
        # 1420800 kHz = 1420.8 MHz
        result = j.read_cpu_freq()
        self.assertAlmostEqual(result, 1420.8, places=1)

    @patch("jetson_power_gui.read_first_available", return_value=1109250000)
    def test_read_gpu_freq_hz(self, mock_avail):
        # 1109250000 Hz = 1109.25 MHz
        result = j.read_gpu_freq()
        self.assertAlmostEqual(result, 1109.25, places=1)

    @patch("jetson_power_gui.read_first_available", return_value=2133000)
    def test_read_gpu_freq_khz(self, mock_avail):
        # 2133000 kHz = 2133 MHz
        result = j.read_gpu_freq()
        self.assertAlmostEqual(result, 2133.0, places=1)

    @patch("jetson_power_gui.read_first_available", return_value=1600)
    def test_read_gpu_freq_mhz(self, mock_avail):
        # 1600 MHz already
        result = j.read_gpu_freq()
        self.assertEqual(result, 1600.0)

    @patch("jetson_power_gui.read_first_available", return_value=None)
    def test_read_gpu_freq_unavailable(self, mock_avail):
        result = j.read_gpu_freq()
        self.assertIsNone(result)

    @patch("jetson_power_gui.read_first_available", return_value=None)
    def test_read_emc_freq_unavailable(self, mock_avail):
        result = j.read_emc_freq()
        self.assertIsNone(result)


class TestToolAvailability(unittest.TestCase):
    """Test CLI tool availability detection."""

    @patch("subprocess.run", return_value=MagicMock(returncode=0))
    def test_tool_available_true(self, mock_run):
        self.assertTrue(j.tool_available("nvpmodel"))

    @patch("subprocess.run", return_value=MagicMock(returncode=1))
    def test_tool_available_false(self, mock_run):
        self.assertFalse(j.tool_available("nonexistent_tool"))


class TestRunCmd(unittest.TestCase):
    """Test command execution helper."""

    @patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="output", stderr=""))
    def test_run_cmd_success(self, mock_run):
        out, err, ok = j.run_cmd(["nvpmodel", "-q"], sudo=False)
        self.assertEqual(out, "output")
        self.assertTrue(ok)

    @patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="error"))
    def test_run_cmd_failure(self, mock_run):
        out, err, ok = j.run_cmd(["nvpmodel", "-m", "2"], sudo=False)
        self.assertFalse(ok)
        self.assertEqual(err, "error")


class TestModeDataStructure(unittest.TestCase):
    """Test that mode dicts have expected keys."""

    def test_mode_keys(self):
        modes = j.parse_power_modes()
        for mode in modes:
            self.assertIn("id", mode)
            self.assertIn("name", mode)
            self.assertIn("watts", mode)
            self.assertIn("cores", mode)
            self.assertIsInstance(mode["id"], int)
            self.assertIsInstance(mode["name"], str)


if __name__ == "__main__":
    unittest.main()
