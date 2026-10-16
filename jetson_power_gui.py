#!/usr/bin/env python3
"""
Jetson Power Modes Manager GUI
================================
A tkinter desktop application for managing power modes on NVIDIA Jetson
boards using the inbuilt ``nvpmodel`` and ``jetson_clocks`` CLI tools.

Supported boards (nvpmodel/jetson_clocks present in L4T):
* Jetson Xavier NX series
* Jetson AGX Xavier
* Jetson Orin Nano / Orin NX
* Jetson AGX Orin
* Jetson Thor (JetPack 6/7 - best-effort, may need board-specific paths)

Features
--------
* Auto-detects the Jetson board model from /proc/device-tree/model
* Lists all available power modes parsed from /etc/nvpmodel.conf (dynamic)
* Shows the currently active power mode (nvpmodel -q)
* Switches power modes (nvpmodel -m <id>) with root elevation
* One-click jetson_clocks max-performance toggle + restore defaults
* Live system monitor: temperatures, CPU & GPU clocks (multi-path fallbacks)
* Auto-refreshing stats with configurable interval
* Activity log console
* Startup compatibility check

Tested on: NVIDIA Jetson Xavier NX (JetPack/L4T)
Best-effort: Jetson AGX Xavier, Orin series, Jetson Thor

Requirements
------------
* Python 3 with tkinter (usually pre-installed on JetPack)
* nvpmodel and jetson_clocks present on the system
* Root access (sudo / pkexec) to change power modes

Run
---
    python3 jetson_power_gui.py
"""

import os
import re
import sys
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
CONFIG_FILE = "/etc/nvpmodel.conf"
THERMAL_DIR = "/sys/class/thermal"
BOARD_FILE = "/proc/device-tree/model"

ICON_PATHS = [
    "/usr/share/icons/hicolor/512x512/apps/jetson-power-gui.png",
    "/usr/share/icons/hicolor/256x256/apps/jetson-power-gui.png",
    "/usr/share/icons/hicolor/128x128/apps/jetson-power-gui.png",
    "/usr/share/icons/hicolor/64x64/apps/jetson-power-gui.png",
    "/usr/share/icons/hicolor/48x48/apps/jetson-power-gui.png",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "512x512", "jetson-power-gui.png"),
]

REFRESH_INTERVAL_MS = 2000  # default auto-refresh interval

# Possible sysfs paths for GPU current frequency (varies by board generation)
GPU_FREQ_PATHS = [
    "/sys/devices/17000000.gv11b/devfreq_dev/cur_freq",       # Xavier NX
    "/sys/devices/gpu.0/devfreq/17000000.gp10b/cur_freq",     # older Tegra
    "/sys/devices/platform/17000000.gp10b/devfreq/cur_freq",  # alternate
]

# Possible sysfs paths for EMC current frequency (varies by board generation)
EMC_FREQ_PATHS = [
    "/sys/kernel/debug/bpmp/debug/clk/emc/freq",
    "/sys/kernel/debug/clock/emc/freq",
    "/sys/kernel/debug/clk/emc/freq",
]

# Possible paths for CPU current frequency (fallback chain)
CPU_FREQ_PATHS = [
    "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
    "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run_cmd(cmd, sudo=False, timeout=30):
    """Run a shell command.

    When ``sudo`` is True, elevation is attempted via pkexec (GUI polkit
    prompt), then sudo, then directly as a fallback.
    Returns (stdout, stderr, ok).
    """
    if sudo:
        for elev in (["pkexec"], ["sudo"]):
            try:
                full = elev + list(cmd)
                res = subprocess.run(
                    full, capture_output=True, text=True, timeout=timeout
                )
                if res.returncode == 0:
                    return res.stdout.strip(), res.stderr.strip(), True
                if elev[0] in ("pkexec", "sudo"):
                    return res.stdout.strip(), res.stderr.strip(), False
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                return "", "Command timed out", False
            except Exception as exc:  # pragma: no cover - defensive
                return "", str(exc), False
        # Final fallback: run directly (may succeed if already root)
        try:
            res = subprocess.run(
                list(cmd), capture_output=True, text=True, timeout=timeout
            )
            return res.stdout.strip(), res.stderr.strip(), res.returncode == 0
        except subprocess.TimeoutExpired:
            return "", "Command timed out", False
        except Exception as exc:
            return "", str(exc), False
    else:
        try:
            res = subprocess.run(
                list(cmd), capture_output=True, text=True, timeout=timeout
            )
            return res.stdout.strip(), res.stderr.strip(), res.returncode == 0
        except subprocess.TimeoutExpired:
            return "", "Command timed out", False
        except Exception as exc:
            return "", str(exc), False


def detect_board():
    """Return the Jetson board model string."""
    try:
        with open(BOARD_FILE, "r") as fh:
            return fh.read().replace("\x00", "").strip()
    except Exception:
        return "Unknown Jetson Board"


def parse_power_modes(path=CONFIG_FILE):
    """Parse POWER_MODEL sections from nvpmodel.conf.

    Returns a list of dicts: id, name, watts, cores.
    Works for any Jetson board because it reads the config file dynamically.
    """
    modes = []
    if not os.path.exists(path):
        return modes
    pattern = re.compile(
        r"<\s*POWER_MODEL\s+ID=(\d+)\s+NAME=([^\s>]+)\s*>"
    )
    try:
        with open(path, "r") as fh:
            for line in fh:
                m = pattern.search(line)
                if m:
                    mode_id = int(m.group(1))
                    name = m.group(2)
                    w_match = re.search(r"(\d+)W", name)
                    c_match = re.search(r"(\d+)CORE", name)
                    modes.append(
                        {
                            "id": mode_id,
                            "name": name,
                            "watts": int(w_match.group(1)) if w_match else None,
                            "cores": int(c_match.group(1)) if c_match else None,
                        }
                    )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[jetson_power_gui] Failed to parse {path}: {exc}")
    modes.sort(key=lambda x: x["id"])
    return modes


def tool_available(name):
    """Check if a CLI tool exists on the system."""
    return subprocess.run(
        ["which", name], capture_output=True, text=True
    ).returncode == 0


def get_current_mode():
    """Query the active power mode via nvpmodel -q.

    Returns (name, id) where name like "MODE_20W_6CORE" and id is an int.
    """
    out, _, _ = run_cmd(["nvpmodel", "-q"], sudo=False)
    name = None
    mode_id = None
    # nvpmodel -q prints the mode info on the first two lines, followed by
    # NVPM ERROR messages (also on stdout). Skip the error lines.
    clean = [ln for ln in out.splitlines() if ln.strip()
             and not ln.startswith("NVPM")]
    for ln in clean:
        if "NV Power Mode:" in ln:
            name = ln.split("NV Power Mode:")[-1].strip()
    # The mode ID integer is on its own line right after the mode name line.
    for ln in clean:
        stripped = ln.strip()
        if stripped.isdigit():
            mode_id = int(stripped)
            break
    return name, mode_id


def read_thermal_zones():
    """Read all thermal zone temperatures in degrees Celsius.

    Returns list of (zone_name, zone_type, temp_c). Reads every
    thermal_zone* directory found - works across all Jetson boards
    because thermal zone naming varies between Xavier/Orin/Thor.
    """
    temps = []
    if not os.path.isdir(THERMAL_DIR):
        return temps
    try:
        zones = sorted(
            d for d in os.listdir(THERMAL_DIR) if d.startswith("thermal_zone")
        )
    except OSError:
        return temps
    for zone in zones:
        zone_path = os.path.join(THERMAL_DIR, zone)
        type_file = os.path.join(zone_path, "type")
        temp_file = os.path.join(zone_path, "temp")
        try:
            with open(type_file, "r") as fh:
                zone_type = fh.read().strip()
            with open(temp_file, "r") as fh:
                temp_raw = int(fh.read().strip())
            temps.append((zone, zone_type, temp_raw / 1000.0))
        except Exception:
            continue
    return temps


def read_first_available(paths):
    """Read the first available integer-valued file from a list of paths."""
    for path in paths:
        try:
            with open(path, "r") as fh:
                return int(fh.read().strip())
        except Exception:
            continue
    return None


def read_cpu_freq():
    """Return current CPU frequency in MHz (from cpu0, with fallbacks)."""
    raw = read_first_available(CPU_FREQ_PATHS)
    if raw is not None:
        return raw / 1000.0
    return None


def read_gpu_freq():
    """Return current GPU frequency in MHz (tries multiple board-specific paths)."""
    raw = read_first_available(GPU_FREQ_PATHS)
    if raw is not None:
        # Guess unit by magnitude:
        #   > 100_000_000 -> Hz      (e.g. 1_109_250_000 Hz = 1109 MHz)
        #   > 100_000     -> kHz     (e.g. 1_109_250 kHz = 1109 MHz)
        #   else          -> MHz
        if raw > 100_000_000:
            return raw / 1_000_000.0
        if raw > 100_000:
            return raw / 1_000.0
        return float(raw)
    return None


def read_emc_freq():
    """Return current EMC frequency in MHz (tries multiple board-specific paths)."""
    raw = read_first_available(EMC_FREQ_PATHS)
    if raw is not None:
        if raw > 100_000_000:
            return raw / 1_000_000.0
        if raw > 100_000:
            return raw / 1_000.0
        return float(raw)
    return None


# --------------------------------------------------------------------------- #
# Main Application
# --------------------------------------------------------------------------- #
class JetsonPowerGUI(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Jetson Power Modes Manager")
        self.geometry("840x720")
        self.minsize(780, 640)

        self.board = detect_board()
        self.modes = parse_power_modes()
        self.current_name, self.current_id = get_current_mode()
        self._refresh_job = None
        self._auto_refresh = tk.BooleanVar(value=True)
        self._refresh_interval = tk.IntVar(value=2000)
        self._is_root = os.geteuid() == 0
        self._force_reboot = tk.BooleanVar(value=False)

        # Set window icon
        self._set_window_icon()

        # Tool availability
        self._has_nvpmodel = tool_available("nvpmodel")
        self._has_jetson_clocks = tool_available("jetson_clocks")

        self._build_ui()
        self._populate_modes()
        self._run_startup_check()
        self._refresh_stats()
        self._start_auto_refresh()

        # Handle window close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # ------------------------------------------------------------------ #
        # Header: board info + current mode
        # ------------------------------------------------------------------ #
        header = ttk.Frame(self, padding=10)
        header.pack(fill="x")

        ttk.Label(
            header, text=self.board
        ).pack(side="left")

        ttk.Label(
            header, text="  |  "
        ).pack(side="left")

        self.mode_var = tk.StringVar(value="Querying...")
        self.mode_label = ttk.Label(
            header,
            textvariable=self.mode_var,
            foreground="#1f77b4",
        )
        self.mode_label.pack(side="left")

        # ------------------------------------------------------------------ #
        # Tool availability banner
        # ------------------------------------------------------------------ #
        self._tool_var = tk.StringVar()
        self._update_tool_banner()
        tool_bar = ttk.Label(
            self, textvariable=self._tool_var, anchor="w", padding=(8, 3)
        )
        tool_bar.pack(fill="x")

        # Root / privilege banner
        self._priv_var = tk.StringVar()
        self._update_priv_banner()
        priv_bar = ttk.Label(
            self, textvariable=self._priv_var, anchor="w", padding=(8, 3)
        )
        priv_bar.pack(fill="x")

        # ------------------------------------------------------------------ #
        # Power modes panel
        # ------------------------------------------------------------------ #
        modes_frame = ttk.LabelFrame(
            self, text="Available Power Modes (nvpmodel)", padding=10
        )
        modes_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Toolbar for modes
        mode_toolbar = ttk.Frame(modes_frame)
        mode_toolbar.pack(fill="x", pady=(0, 8))

        self.selected_mode = tk.IntVar(value=self.current_id or -1)

        ttk.Button(
            mode_toolbar,
            text="Refresh Modes",
            command=self._refresh_modes,
        ).pack(side="left", padx=(0, 8))

        ttk.Checkbutton(
            mode_toolbar,
            text="Auto-reboot if required (--force)",
            variable=self._force_reboot,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            mode_toolbar,
            text="Apply Selected Mode",
            command=self._apply_selected_mode,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            mode_toolbar,
            text="Test Root Access",
            command=self._check_root_access,
        ).pack(side="left", padx=(8, 0))

        ttk.Button(
            mode_toolbar,
            text="Reboot Now",
            command=self._reboot_now,
        ).pack(side="left", padx=(8, 0))

        # Scrollable list of mode buttons
        canvas = tk.Canvas(modes_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            modes_frame, orient="vertical", command=canvas.yview
        )
        self.modes_container = ttk.Frame(canvas)

        self.modes_container.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.modes_container, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling on the canvas
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        # ------------------------------------------------------------------ #
        # jetson_clocks panel
        # ------------------------------------------------------------------ #
        clocks_frame = ttk.LabelFrame(
            self, text="jetson_clocks (Max Performance)", padding=10
        )
        clocks_frame.pack(fill="x", padx=10, pady=(0, 10))

        self.clocks_var = tk.StringVar(value="Unknown")
        ttk.Label(
            clocks_frame, text="Status:"
        ).pack(side="left")
        self.clocks_label = ttk.Label(
            clocks_frame, textvariable=self.clocks_var, foreground="#555"
        )
        self.clocks_label.pack(side="left", padx=(6, 16))

        ttk.Button(
            clocks_frame,
            text="Enable Max Clocks",
            command=self._enable_max_clocks,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            clocks_frame,
            text="Restore Default Clocks",
            command=self._restore_default_clocks,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            clocks_frame,
            text="Refresh Status",
            command=self._refresh_clocks_status,
        ).pack(side="left")

        # ------------------------------------------------------------------ #
        # System monitor panel
        # ------------------------------------------------------------------ #
        mon_frame = ttk.LabelFrame(self, text="Live System Monitor", padding=10)
        mon_frame.pack(fill="x", padx=10, pady=(0, 10))

        # Two-column grid of stats
        self.stat_labels = {}
        stats = [
            ("cpu_temp", "CPU Temp", "- C"),
            ("gpu_temp", "GPU Temp", "- C"),
            ("aux_temp", "AUX Temp", "- C"),
            ("cpu_freq", "CPU Clock", "- MHz"),
            ("gpu_freq", "GPU Clock", "- MHz"),
            ("emc_freq", "EMC Clock", "- MHz"),
        ]
        for i, (key, label, default) in enumerate(stats):
            col = i % 3
            row = i // 3
            box = ttk.Frame(mon_frame)
            box.grid(row=row, column=col, sticky="ew", padx=6, pady=4)
            ttk.Label(box, text=label).pack(
                side="top", anchor="w"
            )
            var = tk.StringVar(value=default)
            ttk.Label(box, textvariable=var).pack(
                side="top", anchor="w"
            )
            self.stat_labels[key] = var
        mon_frame.columnconfigure(0, weight=1)
        mon_frame.columnconfigure(1, weight=1)
        mon_frame.columnconfigure(2, weight=1)

        # Auto-refresh controls
        refresh_ctrl = ttk.Frame(mon_frame)
        refresh_ctrl.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            refresh_ctrl,
            text="Auto-refresh stats",
            variable=self._auto_refresh,
            command=self._toggle_auto_refresh,
        ).pack(side="left")
        ttk.Label(refresh_ctrl, text="Interval (ms):").pack(
            side="left", padx=(12, 4)
        )
        ttk.Spinbox(
            refresh_ctrl,
            from_=500,
            to=60000,
            increment=500,
            width=7,
            textvariable=self._refresh_interval,
            command=self._restart_auto_refresh,
        ).pack(side="left")
        ttk.Button(
            refresh_ctrl, text="Refresh Now", command=self._refresh_stats
        ).pack(side="right")

        # ------------------------------------------------------------------ #
        # Log console
        # ------------------------------------------------------------------ #
        log_frame = ttk.LabelFrame(self, text="Activity Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log = scrolledtext.ScrolledText(
            log_frame, height=8, wrap="word"
        )
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        # ------------------------------------------------------------------ #
        # Status bar
        # ------------------------------------------------------------------ #
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(
            self, textvariable=self.status_var, anchor="w", padding=(6, 3),
            relief="sunken",
        )
        status_bar.pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ #
    # UI helpers
    # ------------------------------------------------------------------ #
    def _update_tool_banner(self):
        parts = []
        if self._has_nvpmodel:
            parts.append("[OK] nvpmodel found")
        else:
            parts.append("[FAIL] nvpmodel NOT found")
        if self._has_jetson_clocks:
            parts.append("[OK] jetson_clocks found")
        else:
            parts.append("[FAIL] jetson_clocks NOT found")
        self._tool_var.set("  ".join(parts))

    def _set_window_icon(self):
        """Set the window icon from the best available icon file."""
        try:
            from tkinter import PhotoImage
            for path in ICON_PATHS:
                if os.path.exists(path):
                    img = PhotoImage(file=path)
                    self.iconphoto(True, img)
                    break
        except Exception:
            pass  # Icon is optional; app works without it

    def _update_priv_banner(self):
        if self._is_root:
            self._priv_var.set(
                "Running as root - power mode changes will execute directly."
            )
        else:
            self._priv_var.set(
                "Not running as root - mode changes require sudo/pkexec "
                "(you may be prompted for a password)."
            )

    def _log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, message):
        self.status_var.set(message)
        self.after(5000, lambda: self.status_var.set("Ready"))

    def _set_mode_display(self, name, mode_id):
        if name:
            self.mode_var.set(f"Current: {name} (ID {mode_id})")
        else:
            self.mode_var.set(f"Current: Unknown (ID {mode_id})")

    # ------------------------------------------------------------------ #
    # Startup compatibility check
    # ------------------------------------------------------------------ #
    def _run_startup_check(self):
        issues = []
        if not self._has_nvpmodel:
            issues.append("nvpmodel not found - power mode switching will not work.")
        if not self._has_jetson_clocks:
            issues.append("jetson_clocks not found - clock controls will not work.")
        if not self.modes:
            issues.append(
                f"No power modes found in {CONFIG_FILE} - "
                "this board may not use nvpmodel."
            )
        if self._is_root:
            issues.append("Running as root - no password prompt needed for changes.")
        else:
            issues.append(
                "Not running as root - you may be prompted for sudo/pkexec password."
            )

        self._log("=== Jetson Power Modes Manager started ===")
        self._log(f"Board: {self.board}")
        self._log(f"Power modes found: {len(self.modes)}")
        for issue in issues:
            self._log(f"* {issue}")
        self._log("")

        # Show a warning dialog if critical tools are missing
        if not self._has_nvpmodel:
            self.after(
                500,
                lambda: messagebox.showwarning(
                    "Missing tool",
                    "nvpmodel was not found on this system.\n\n"
                    "Power mode switching requires nvpmodel, which is part of "
                    "NVIDIA L4T / JetPack. Some boards (e.g. Jetson Thor on "
                    "early JetPack) may not include it.",
                ),
            )

    # ------------------------------------------------------------------ #
    # Modes
    # ------------------------------------------------------------------ #
    def _populate_modes(self):
        for widget in self.modes_container.winfo_children():
            widget.destroy()

        if not self.modes:
            ttk.Label(
                self.modes_container,
                text="No power modes found in /etc/nvpmodel.conf.\n"
                     "This board may not use nvpmodel, or the config file "
                     "is missing/different.",
                foreground="#888",
            ).pack(anchor="w", pady=10)
            return

        for mode in self.modes:
            self._add_mode_row(mode)

    def _add_mode_row(self, mode):
        mid = mode["id"]
        name = mode["name"]
        watts = mode["watts"]
        cores = mode["cores"]

        row = ttk.Frame(self.modes_container, padding=(4, 4))
        row.pack(fill="x", pady=2)

        # Outer card look
        card = ttk.Frame(row, padding=8, relief="raised", borderwidth=1)
        card.pack(fill="x")

        # Radio button to select
        rb = ttk.Radiobutton(
            card,
            variable=self.selected_mode,
            value=mid,
        )
        rb.pack(side="left")

        # Details text
        parts = [f"ID {mid}", name]
        if watts is not None:
            parts.append(f"{watts} W")
        if cores is not None:
            parts.append(f"{cores} core" + ("s" if cores != 1 else ""))
        detail = " - ".join(parts)

        is_active = (self.current_id is not None and mid == self.current_id)
        if is_active:
            detail = "* " + detail + "  (active)"
            detail_color = "#1f77b4"
        else:
            detail_color = "#333"

        ttk.Label(
            card, text=detail,
            foreground=detail_color,
        ).pack(side="left", padx=(8, 0))

        # Make the whole row clickable to select
        for widget in (card,):
            widget.bind("<Button-1>", lambda e, m=mid: self.selected_mode.set(m))

    def _refresh_modes(self):
        self._log("Refreshing power mode list from /etc/nvpmodel.conf...")
        self.modes = parse_power_modes()
        self.current_name, self.current_id = get_current_mode()
        self._set_mode_display(self.current_name, self.current_id)
        # Re-select current mode
        if self.current_id is not None:
            self.selected_mode.set(self.current_id)
        self._populate_modes()
        self._log(
            f"Found {len(self.modes)} power mode(s). Current: {self.current_name}"
        )

    def _apply_selected_mode(self):
        mid = self.selected_mode.get()
        mode = next((m for m in self.modes if m["id"] == mid), None)
        if mode is None:
            messagebox.showwarning(
                "No mode selected", "Please select a power mode to apply."
            )
            return

        name = mode["name"]
        if messagebox.askyesno(
            "Switch Power Mode",
            f"Switch to {name} (ID {mid})?\n\n"
            "This requires root privileges and may briefly affect "
            "performance. Some mode changes require a reboot to take full effect.",
        ):
            self._run_in_thread(self._switch_mode, mid, name)

    def _switch_mode(self, mid, name):
        self.after(0, lambda: self._set_status(f"Switching to {name} (ID {mid})..."))
        self.after(0, lambda: self._log(f"Running: nvpmodel -m {mid} (elevated)"))

        cmd = ["nvpmodel", "-m", str(mid)]
        force_reboot = self._force_reboot.get()
        if force_reboot:
            cmd.append("--force")
            self.after(
                0,
                lambda: self._log(
                    "[WARN] --force is set: board will reboot automatically after switching."
                ),
            )

        out, err, ok = run_cmd(cmd, sudo=True)

        if ok:
            self.after(0, lambda: self._log(f"[OK] Switched to {name} (ID {mid})"))

            if force_reboot:
                # Auto-reboot: show countdown dialog, then reboot
                self.after(0, lambda: self._reboot_countdown(name))
            else:
                # Check if nvpmodel output mentions reboot
                if out and "reboot" in out.lower():
                    self.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Reboot recommended",
                            f"The power mode change to {name} may require a reboot "
                            "to take full effect. You can reboot now or later.",
                        ),
                    )
                # Re-query current mode after a short delay
                self.after(1500, self._refresh_current_mode)
        else:
            self.after(0, lambda: self._log(f"[FAIL] Failed: {err or out}"))
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Mode switch failed",
                    f"Could not switch to {name}.\n\n{err or out}",
                ),
            )
        self.after(0, lambda: self._set_status("Ready"))

    def _reboot_countdown(self, mode_name):
        """Show a countdown dialog before rebooting the board."""
        dialog = tk.Toplevel(self)
        dialog.title("Rebooting…")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Center the dialog
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() // 2) - 200
        y = (dialog.winfo_screenheight() // 2) - 100
        dialog.geometry(f"400x150+{x}+{y}")

        ttk.Label(
            dialog,
            text=f"Power mode switched to {mode_name}.\n"
            "The board will reboot automatically in:",
            font=("TkDefaultFont", 10),
            justify="center",
        ).pack(pady=(20, 5))

        countdown_var = tk.IntVar(value=10)
        countdown_label = ttk.Label(
            dialog, textvariable=countdown_var, font=("TkDefaultFont", 24, "bold")
        )
        countdown_label.pack(pady=5)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(pady=(10, 20))

        cancel_btn = ttk.Button(
            btn_frame, text="Cancel", command=lambda: self._cancel_reboot(dialog)
        )
        cancel_btn.pack(side="left", padx=10)

        reboot_now_btn = ttk.Button(
            btn_frame,
            text="Reboot Now",
            command=lambda: self._do_reboot(dialog),
        )
        reboot_now_btn.pack(side="left", padx=10)

        self._reboot_dialog = dialog
        self._reboot_count = 10
        self._reboot_cancelled = False
        self._reboot_countdown_tick(dialog, countdown_var, countdown_label)

    def _reboot_countdown_tick(self, dialog, countdown_var, countdown_label):
        if self._reboot_cancelled:
            dialog.destroy()
            self._reboot_dialog = None
            return

        if self._reboot_count > 0:
            countdown_var.set(self._reboot_count)
            self._reboot_count -= 1
            dialog.after(1000, lambda: self._reboot_countdown_tick(
                dialog, countdown_var, countdown_label
            ))
        else:
            dialog.destroy()
            self._reboot_dialog = None
            self._do_reboot(None)

    def _cancel_reboot(self, dialog):
        self._reboot_cancelled = True
        dialog.destroy()
        self._reboot_dialog = None
        self._log("Auto-reboot cancelled by user.")
        self._set_status("Ready")

    def _do_reboot(self, dialog):
        if dialog:
            dialog.destroy()
            self._reboot_dialog = None
        self._log("Rebooting board now…")
        self._set_status("Rebooting…")
        self._run_in_thread(self._run_reboot)

    def _run_reboot(self):
        out, err, ok = run_cmd(["reboot"], sudo=True)
        if ok:
            self.after(0, lambda: self._log("[OK] Reboot command issued."))
        else:
            self.after(0, lambda: self._log(f"[FAIL] Reboot failed: {err or out}"))
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Reboot failed",
                    f"Could not reboot the board.\n\n{err or out}",
                ),
            )
            self.after(0, lambda: self._set_status("Ready"))

    def _reboot_now(self):
        """Immediate reboot button handler."""
        if messagebox.askyesno(
            "Reboot Board",
            "Reboot the Jetson board now?\n\n"
            "All unsaved work will be lost. This requires root privileges.",
        ):
            self._do_reboot(None)

    def _refresh_current_mode(self):
        self.current_name, self.current_id = get_current_mode()
        self._set_mode_display(self.current_name, self.current_id)
        if self.current_id is not None:
            self.selected_mode.set(self.current_id)
        self._populate_modes()  # refresh active highlight
        self._log(f"Current mode re-queried: {self.current_name}")
        self._refresh_stats()

    # ------------------------------------------------------------------ #
    # jetson_clocks
    # ------------------------------------------------------------------ #
    def _enable_max_clocks(self):
        if not self._has_jetson_clocks:
            messagebox.showerror(
                "Missing tool",
                "jetson_clocks is not available on this system.",
            )
            return
        if messagebox.askyesno(
            "jetson_clocks",
            "Run jetson_clocks to set static max frequency for CPU, GPU and EMC?\n\n"
            "This overrides power-mode clock limits until the next mode change "
            "or reboot. Requires root.",
        ):
            self._run_in_thread(self._run_max_clocks)

    def _run_max_clocks(self):
        self.after(0, lambda: self._set_status("Running jetson_clocks..."))
        self.after(0, lambda: self._log("Running: jetson_clocks (elevated)"))
        out, err, ok = run_cmd(["jetson_clocks"], sudo=True)
        if ok:
            self.after(0, lambda: self._log("[OK] jetson_clocks: max clocks enabled"))
        else:
            self.after(0, lambda: self._log(f"[FAIL] jetson_clocks failed: {err or out}"))
        self.after(0, self._refresh_clocks_status)
        self.after(0, lambda: self._set_status("Ready"))

    def _refresh_clocks_status(self):
        out, _, _ = run_cmd(["jetson_clocks", "--show"], sudo=True)
        if out and "Error" not in out and "error" not in out.lower():
            # jetson_clocks --show prints current settings; treat presence as active
            self.clocks_var.set("Max clocks active")
            self.clocks_label.configure(foreground="#2ca02c")
        else:
            self.clocks_var.set("Default clocks")
            self.clocks_label.configure(foreground="#555")

    def _restore_default_clocks(self):
        """Revert jetson_clocks max-performance override to power-mode defaults."""
        if not self._has_jetson_clocks:
            messagebox.showerror(
                "Missing tool",
                "jetson_clocks is not available on this system.",
            )
            return
        if messagebox.askyesno(
            "Restore Default Clocks",
            "Restore default clocks (undo jetson_clocks max performance)?\n\n"
            "This re-applies the current power mode's clock settings. "
            "Requires root.",
        ):
            self._run_in_thread(self._run_restore_default_clocks)

    def _run_restore_default_clocks(self):
        self.after(0, lambda: self._set_status("Restoring default clocks..."))
        self.after(0, lambda: self._log("Running: jetson_clocks --restore (elevated)"))

        # First try the official restore (needs a previously stored l4t_dfs.conf)
        out, err, ok = run_cmd(["jetson_clocks", "--restore"], sudo=True)

        if not ok:
            # Fallback: re-apply the current power mode, which resets clocks
            # to that mode's defaults (this is the reliable way to undo jetson_clocks)
            mid = self.current_id
            if mid is None:
                self.after(0, lambda: self._log("[FAIL] Cannot restore: no current mode known"))
                self.after(0, lambda: self._set_status("Ready"))
                return
            self.after(
                0,
                lambda: self._log(
                    f"jetson_clocks --restore unavailable; re-applying mode ID {mid} instead"
                ),
            )
            out, err, ok = run_cmd(["nvpmodel", "-m", str(mid)], sudo=True)

        if ok:
            self.after(0, lambda: self._log("[OK] Default clocks restored"))
        else:
            self.after(0, lambda: self._log(f"[FAIL] Restore failed: {err or out}"))
        self.after(0, self._refresh_clocks_status)
        self.after(0, lambda: self._set_status("Ready"))

    def _check_root_access(self):
        """Verify that elevated commands (sudo/pkexec) work."""
        self._run_in_thread(self._run_root_check)

    def _run_root_check(self):
        self.after(0, lambda: self._set_status("Checking root access..."))
        self.after(0, lambda: self._log("Testing elevated access: nvpmodel -q"))
        out, err, ok = run_cmd(["nvpmodel", "-q"], sudo=True)
        if ok:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Root Access OK",
                    "Elevated commands (sudo/pkexec) are working.\n\n"
                    "You can switch power modes and run jetson_clocks.",
                ),
            )
            self.after(0, lambda: self._log("[OK] Root access confirmed"))
        else:
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Root Access Failed",
                    "Elevated commands are not available.\n\n"
                    "Mode changes and jetson_clocks will fail.\n\n"
                    f"Error: {err or out}",
                ),
            )
            self.after(0, lambda: self._log(f"[FAIL] Root access failed: {err or out}"))
        self.after(0, lambda: self._set_status("Ready"))

    # ------------------------------------------------------------------ #
    # Stats monitor
    # ------------------------------------------------------------------ #
    def _refresh_stats(self):
        # Temperatures - read all thermal zones and map by type
        temps = read_thermal_zones()
        temp_map = {ztype: round(temp, 1) for _, ztype, temp in temps}
        # Common thermal zone type names across Jetson boards:
        # Xavier NX: CPU-therm, GPU-therm, AUX-therm, AO-therm, PMIC-Die
        # Orin:      CPU-therm, GPU-therm, SOC-therm, etc. (varies)
        for key, zone_types in (
            ("cpu_temp", ("CPU-therm", "cpu-therm", "CPU")),
            ("gpu_temp", ("GPU-therm", "gpu-therm", "GPU")),
            ("aux_temp", ("AUX-therm", "aux-therm", "AUX", "SOC-therm", "soc-therm")),
        ):
            val = None
            for zt in zone_types:
                if zt in temp_map:
                    val = temp_map[zt]
                    break
            self.stat_labels[key].set(
                f"{val} C" if val is not None else "- C"
            )

        # Frequencies
        cpu = read_cpu_freq()
        self.stat_labels["cpu_freq"].set(
            f"{int(cpu)} MHz" if cpu is not None else "- MHz"
        )
        gpu = read_gpu_freq()
        self.stat_labels["gpu_freq"].set(
            f"{int(gpu)} MHz" if gpu is not None else "- MHz"
        )
        emc = read_emc_freq()
        self.stat_labels["emc_freq"].set(
            f"{int(emc)} MHz" if emc is not None else "- MHz"
        )

    def _start_auto_refresh(self):
        self._schedule_refresh()

    def _schedule_refresh(self):
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
        if self._auto_refresh.get():
            self._refresh_job = self.after(
                self._refresh_interval.get(), self._auto_refresh_tick
            )

    def _auto_refresh_tick(self):
        self._refresh_stats()
        self._schedule_refresh()

    def _toggle_auto_refresh(self):
        if self._auto_refresh.get():
            self._schedule_refresh()
        else:
            if self._refresh_job is not None:
                self.after_cancel(self._refresh_job)
                self._refresh_job = None

    def _restart_auto_refresh(self):
        self._schedule_refresh()

    # ------------------------------------------------------------------ #
    # Threading helper
    # ------------------------------------------------------------------ #
    def _run_in_thread(self, func, *args):
        t = threading.Thread(target=func, args=args, daemon=True)
        t.start()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _on_close(self):
        self._toggle_auto_refresh()
        self.destroy()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    # Warn if tkinter is missing
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is not installed. On JetPack install:")
        print("  sudo apt-get install python3-tk")
        sys.exit(1)

    app = JetsonPowerGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
