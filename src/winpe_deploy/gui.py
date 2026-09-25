from __future__ import annotations

import ctypes
import logging
import queue
import re
import threading
import time
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .command_runner import CommandRunner
from .media_content import MEDIA_CONTENT_DIR
from .models import (
    CaptureCompression,
    FirmwareType,
    VolumeInfo,
    validate_image_index,
    validate_relative_image_destination,
    validate_wim_path,
)
from .operation_locks import OperationLocks, path_disk_number
from .services import DeploymentService, NetworkService, WinPEPowerService
from .startup_preflight import STARTUP_LOG_PATH


class TextQueueHandler(logging.Handler):
    def __init__(self, messages: queue.Queue[str]) -> None:
        super().__init__()
        self._messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self._messages.put(self.format(record))


class WinPEImageDeployerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"WinPE Image Deployer [{__version__}]")
        window_width, window_height = self._initial_window_size(self.winfo_screenwidth(), self.winfo_screenheight())
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(800, 560)
        self._messages: queue.Queue[str] = queue.Queue()
        self._logger = self._configure_logging()
        runner = CommandRunner(self._logger)
        self._deployment = DeploymentService(runner, self._logger)
        self._network = NetworkService(runner)
        self._power = WinPEPowerService(runner)
        self._busy = False
        self._operation_locks = OperationLocks()
        self._disk_buttons: list[ttk.Button] = []
        self._capture_volumes: dict[str, VolumeInfo] = {}
        self._capture_volume_letters: dict[str, str] = {}
        self._progress_estimates: dict[str, tuple[float, float] | None] = {"capture": None, "deploy": None}
        self._setup_style()
        self._build_ui()
        # WinPE shells can ignore a zoom request before the window is mapped.
        # Wait until after the first paint and retry once if necessary.
        self.after(300, lambda: self._maximize_main_window(retries_remaining=1))
        self.after(150, self._flush_log_messages)
        self._log(
            f"Application started. Build {__version__}. DISM Apply-Image does not use /ScratchDir. "
            "Run this program from an elevated Windows PE session."
        )

    @staticmethod
    def _initial_window_size(screen_width: int, screen_height: int) -> tuple[int, int]:
        """Provide a large usable fallback when the WinPE shell cannot maximize Tk."""
        width = max(800, min(1440, screen_width - 40))
        height = max(560, min(960, screen_height - 80))
        return width, height

    def _maximize_main_window(self, *, retries_remaining: int) -> None:
        """Maximize after mapping; retry once for slower WinPE window managers."""
        try:
            self.update_idletasks()
            self.wm_state("zoomed")
        except tk.TclError:
            self._log("The WinPE window manager did not support maximize; using the calculated large window size.")
            return
        if retries_remaining:
            self.after(400, lambda: self._retry_maximize_if_needed(retries_remaining=retries_remaining))

    def _retry_maximize_if_needed(self, *, retries_remaining: int) -> None:
        try:
            maximized = self.wm_state() == "zoomed"
        except tk.TclError:
            return
        if not maximized:
            self._maximize_main_window(retries_remaining=retries_remaining - 1)

    @staticmethod
    def _estimate_remaining_seconds(
        start_time: float, start_percent: float, current_time: float, current_percent: float
    ) -> int | None:
        """Estimate remaining time from measured, forward-only percentage progress."""
        elapsed = current_time - start_time
        completed = current_percent - start_percent
        if elapsed <= 0 or completed <= 0 or current_percent >= 100:
            return None
        return max(0, round((100 - current_percent) * elapsed / completed))

    @staticmethod
    def _format_remaining_time(seconds: int) -> str:
        minutes, remaining_seconds = divmod(seconds, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"~{hours} h {minutes} min"
        if minutes:
            return f"~{minutes} min {remaining_seconds:02d} s"
        return f"~{remaining_seconds} s"

    def _reset_progress_estimate(self, operation: str) -> None:
        self._progress_estimates[operation] = None

    def _progress_message_with_eta(self, operation: str, message: str, percent: float | None) -> str:
        if percent is None or percent >= 100:
            return message
        current_time = time.monotonic()
        # Deployment has a short partitioning phase before DISM starts applying
        # the image at 10%. Start the estimate there so disk preparation time
        # does not distort the much longer image-copy rate.
        if operation == "deploy" and "applying" in message.lower() and percent == 10:
            self._progress_estimates[operation] = (current_time, percent)
            return f"{message} Estimating remaining time..."
        reference = self._progress_estimates.get(operation)
        if reference is None:
            self._progress_estimates[operation] = (current_time, percent)
            return f"{message} Estimating remaining time..."
        start_time, start_percent = reference
        estimate = self._estimate_remaining_seconds(start_time, start_percent, current_time, percent)
        if estimate is None:
            return f"{message} Estimating remaining time..."
        return f"{message} Estimated remaining time: {self._format_remaining_time(estimate)}."

    def _configure_logging(self) -> logging.Logger:
        logger = logging.getLogger("winpe_deployer")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = TextQueueHandler(self._messages)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        try:
            STARTUP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            persistent_handler = logging.FileHandler(STARTUP_LOG_PATH, mode="a", encoding="utf-8")
            persistent_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
            logger.addHandler(persistent_handler)
        except OSError:
            pass
        logger.propagate = False
        return logger

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("vista" if "vista" in style.theme_names() else style.theme_use())
        style.configure("Danger.TButton", foreground="#9c0006")
        style.configure("Heading.TLabel", font=("Segoe UI", 11, "bold"))

    def _build_ui(self) -> None:
        self._status = tk.StringVar(value="Ready")
        footer = ttk.Frame(self)
        # Pack the fixed bottom bar before the expanding notebook so it always
        # reserves vertical space, including when a tab contains a scrollable
        # physical-disk management view.
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Label(footer, textvariable=self._status, relief=tk.SUNKEN, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(footer, text="Restart WinPE", command=self._restart_winpe).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="Shut Down WinPE", style="Danger.TButton", command=self._shutdown_winpe).pack(side=tk.RIGHT)

        notebook = ttk.Notebook(self)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        self._capture_tab = ttk.Frame(notebook, padding=14)
        self._deploy_tab = ttk.Frame(notebook, padding=14)
        self._service_tab = ttk.Frame(notebook, padding=14)
        self._disk_tools_tab = ttk.Frame(notebook, padding=14)
        self._physical_disks_tab = ttk.Frame(notebook, padding=14)
        self._network_tab = ttk.Frame(notebook, padding=14)
        self._text_editor_tab = ttk.Frame(notebook, padding=14)
        self._logs_tab = ttk.Frame(notebook, padding=14)
        notebook.add(self._capture_tab, text="Capture")
        notebook.add(self._deploy_tab, text="Deploy")
        notebook.add(self._service_tab, text="Service Image")
        notebook.add(self._disk_tools_tab, text="Disk Tools")
        notebook.add(self._physical_disks_tab, text="Physical Disks")
        notebook.add(self._network_tab, text="Network")
        notebook.add(self._text_editor_tab, text="Text Editor")
        notebook.add(self._logs_tab, text="Logs")
        self._build_capture_tab()
        self._build_deploy_tab()
        self._build_service_tab()
        self._build_disk_tools_tab()
        self._build_physical_disks_tab()
        self._build_network_tab()
        self._build_text_editor_tab()
        self._build_logs_tab()

    def _build_capture_tab(self) -> None:
        tab = self._capture_tab
        ttk.Label(tab, text="Capture an Offline Windows Volume", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(tab, text="Select an offline source and a separate storage volume for one compressed WIM file.", wraplength=760).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 18))
        self.capture_source = tk.StringVar()
        self.capture_destination = tk.StringVar()
        self.capture_filename = tk.StringVar(value="WindowsReference.wim")
        self.capture_compression = tk.StringVar(value=CaptureCompression.MAXIMUM.value)
        self.capture_name = tk.StringVar(value="Windows Reference Image")
        self.capture_description = tk.StringVar(value="Captured by WinPE Image Deployer")
        self.capture_progress_value = tk.DoubleVar(value=0)
        self.capture_progress_text = tk.StringVar(value="Ready")
        self.capture_source_combo = ttk.Combobox(tab, textvariable=self.capture_source, state="readonly", width=68)
        self.capture_destination_combo = ttk.Combobox(tab, textvariable=self.capture_destination, state="readonly", width=68)
        self._field(tab, 2, "Offline source volume:", self.capture_source_combo, button=("Refresh volumes", self._refresh_capture_volumes))
        self._field(tab, 3, "WIM destination volume:", self.capture_destination_combo, "USB, external disk, or mapped SMB drive")
        self._field(tab, 4, "WIM file name:", ttk.Entry(tab, textvariable=self.capture_filename, width=52), "Example: WindowsReference.wim")
        self._field(tab, 5, "Compression:", ttk.Combobox(tab, textvariable=self.capture_compression, values=[item.value for item in CaptureCompression], state="readonly", width=48), "Maximum creates the smallest WIM.")
        self._field(tab, 6, "Image name:", ttk.Entry(tab, textvariable=self.capture_name, width=52))
        self._field(tab, 7, "Description:", ttk.Entry(tab, textvariable=self.capture_description, width=52))
        self._disk_button(tab, "Capture image", self._capture_image, row=8, column=1, sticky=tk.W, pady=18)
        ttk.Progressbar(tab, variable=self.capture_progress_value, maximum=100).grid(row=9, column=1, sticky=tk.EW)
        ttk.Label(tab, textvariable=self.capture_progress_text).grid(row=10, column=1, sticky=tk.W, pady=(4, 0))
        self._configure_grid(tab)
        self.after(250, self._refresh_capture_volumes)

    def _build_deploy_tab(self) -> None:
        tab = self._deploy_tab
        ttk.Label(tab, text="Deploy a Windows Image", style="Heading.TLabel").grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(tab, text="This erases the selected disk, applies the WIM, and creates boot files. Verify the disk details first.", wraplength=760).grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(4, 12))
        self.disk_list = tk.Listbox(tab, height=7, exportselection=False)
        self.disk_list.grid(row=2, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 6))
        ttk.Button(tab, text="Refresh disks", command=self._refresh_disks).grid(row=2, column=3, sticky=tk.N, padx=(10, 0))
        self.deploy_wim = tk.StringVar()
        self.deploy_index = tk.StringVar(value="1")
        self.firmware = tk.StringVar(value=FirmwareType.UEFI.value)
        self.disk_confirmation = tk.StringVar()
        self.deploy_progress_value = tk.DoubleVar(value=0)
        self.deploy_progress_text = tk.StringVar(value="Ready")
        self.restart_after_deploy = tk.BooleanVar(value=False)
        self._path_field(tab, 3, "WIM image:", self.deploy_wim, inspect_button=True)
        self.deploy_index_combo = ttk.Combobox(tab, textvariable=self.deploy_index, width=48)
        self._field(tab, 4, "Image index:", self.deploy_index_combo, button=("Load indexes", lambda: self._load_wim_indexes(self.deploy_wim, self.deploy_index_combo, self.deploy_index)))
        self._field(tab, 5, "Firmware layout:", ttk.Combobox(tab, textvariable=self.firmware, values=[member.value for member in FirmwareType], state="readonly", width=48))
        ttk.Separator(tab).grid(row=6, column=0, columnspan=4, sticky=tk.EW, pady=12)
        ttk.Label(tab, text="Destructive confirmation", style="Heading.TLabel").grid(row=7, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(tab, text="Type the selected disk number. All data on that disk will be erased.", foreground="#9c0006", wraplength=760).grid(row=8, column=0, columnspan=4, sticky=tk.W, pady=(3, 8))
        self._field(tab, 9, "Target disk number:", ttk.Entry(tab, textvariable=self.disk_confirmation, width=15))
        self._disk_button(tab, "Deploy image", self._deploy_image, row=10, column=1, sticky=tk.W, pady=(14, 4), style="Danger.TButton")
        ttk.Checkbutton(tab, text="Restart computer after successful deployment", variable=self.restart_after_deploy).grid(row=11, column=1, columnspan=3, sticky=tk.W, pady=(0, 12))
        ttk.Progressbar(tab, variable=self.deploy_progress_value, maximum=100).grid(row=12, column=1, sticky=tk.EW)
        ttk.Label(tab, textvariable=self.deploy_progress_text).grid(row=13, column=1, sticky=tk.W, pady=(4, 0))
        self._configure_grid(tab)
        tab.rowconfigure(2, weight=1)
        self.after(300, self._refresh_disks)

    def _build_service_tab(self) -> None:
        tab = self._service_tab
        ttk.Label(tab, text="Service a WIM Image", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(tab, text="Mount a WIM, inject files or extracted .inf drivers, then commit the changes. Offline driver injection changes the selected WIM; live WinPE driver loading is below.", wraplength=720).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 14))
        self.service_wim = tk.StringVar()
        self.service_index = tk.StringVar(value="1")
        self.mount_directory = tk.StringVar(value="X:\\WimMount")
        self.image_destination = tk.StringVar(value="Install\\Packages")
        self.driver_directory = tk.StringVar()
        self.service_progress_value = tk.DoubleVar(value=0)
        self.service_progress_text = tk.StringVar(value="Ready")
        self._path_field(tab, 2, "WIM image:", self.service_wim)
        self.service_index_combo = ttk.Combobox(tab, textvariable=self.service_index, width=48)
        self._field(tab, 3, "Image index:", self.service_index_combo, button=("Load indexes", lambda: self._load_wim_indexes(self.service_wim, self.service_index_combo, self.service_index)))
        self._field(tab, 4, "Mount parent folder:", ttk.Entry(tab, textvariable=self.mount_directory, width=52), button=("Select folder...", self._choose_mount_parent))
        self._field(
            tab,
            5,
            "Destination in image:",
            ttk.Combobox(
                tab,
                textvariable=self.image_destination,
                values=(
                    "Install\\Packages",
                    "Drivers",
                    "OEM",
                    "Temp",
                    "ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup",
                    "Users\\Public\\Desktop",
                ),
                width=48,
            ),
            button=("Custom path...", self._choose_image_destination),
        )
        ttk.Label(tab, text="Files/directories to inject:").grid(row=6, column=0, sticky=tk.NW, pady=(8, 0))
        self.injection_sources = tk.Listbox(tab, height=8)
        self.injection_sources.grid(row=6, column=1, sticky=tk.NSEW, pady=(8, 0))
        controls = ttk.Frame(tab)
        controls.grid(row=6, column=2, sticky=tk.NW, padx=(8, 0), pady=(8, 0))
        ttk.Button(controls, text="Add files", command=self._add_files).pack(fill=tk.X)
        ttk.Button(controls, text="Add folder", command=self._add_folder).pack(fill=tk.X, pady=4)
        ttk.Button(controls, text="Remove selected", command=lambda: self.injection_sources.delete(tk.ANCHOR)).pack(fill=tk.X)
        ttk.Button(tab, text="Inject files and commit", command=self._inject_files).grid(row=7, column=1, sticky=tk.W, pady=14)
        ttk.Button(tab, text="Discard mounted image", command=self._discard_mount).grid(row=7, column=1, sticky=tk.E, pady=14)
        ttk.Separator(tab).grid(row=8, column=0, columnspan=3, sticky=tk.EW, pady=(2, 10))
        ttk.Label(tab, text="Offline WIM driver injection", style="Heading.TLabel").grid(row=9, column=0, columnspan=3, sticky=tk.W)
        self._field(tab, 10, "Extracted driver folder:", ttk.Entry(tab, textvariable=self.driver_directory, width=52), button=("Browse driver folder...", self._choose_driver_directory))
        ttk.Button(tab, text="Inject drivers and commit", command=self._inject_drivers).grid(row=11, column=1, sticky=tk.W, pady=14)
        ttk.Separator(tab).grid(row=12, column=0, columnspan=3, sticky=tk.EW, pady=(2, 10))
        ttk.Label(tab, text="Current WinPE session drivers", style="Heading.TLabel").grid(row=13, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(tab, text="Load extracted .inf drivers now without modifying or rebuilding boot.wim. Loaded drivers are lost after restart.", wraplength=720).grid(row=14, column=0, columnspan=3, sticky=tk.W, pady=(4, 8))
        ttk.Button(tab, text="Load drivers into current WinPE...", command=self._open_session_driver_dialog).grid(row=15, column=1, sticky=tk.W, pady=(0, 8))
        self.service_progress_bar = ttk.Progressbar(tab, variable=self.service_progress_value, maximum=100)
        self.service_progress_bar.grid(row=17, column=1, sticky=tk.EW, pady=(12, 0))
        ttk.Label(tab, textvariable=self.service_progress_text).grid(row=18, column=1, sticky=tk.W, pady=(4, 0))
        self._configure_grid(tab)
        tab.rowconfigure(6, weight=1)
        tab.rowconfigure(16, weight=1)

    def _build_disk_tools_tab(self) -> None:
        tab = self._disk_tools_tab
        ttk.Label(tab, text="Disk Tools", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(tab, text="Manage assigned volumes. Formatting erases the selected volume. X: is protected; C: is also protected when X: is absent.", wraplength=720).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 12))
        self.disk_tool_volumes: dict[str, VolumeInfo] = {}
        self.disk_tool_volume = tk.StringVar()
        self.disk_tool_file_system = tk.StringVar(value="NTFS")
        self.disk_tool_label = tk.StringVar()
        self.disk_tool_new_letter = tk.StringVar()
        self.disk_tool_confirmation = tk.StringVar()
        self.disk_tool_volume_combo = ttk.Combobox(tab, textvariable=self.disk_tool_volume, state="readonly", width=68)
        self.disk_tool_volume_combo.bind("<<ComboboxSelected>>", lambda _: self._sync_disk_tool_fields())
        self._field(tab, 2, "Volume:", self.disk_tool_volume_combo, button=("Refresh volumes", self._refresh_disk_tool_volumes))
        ttk.Separator(tab).grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Rename or change drive letter", style="Heading.TLabel").grid(row=4, column=0, columnspan=3, sticky=tk.W)
        self._field(tab, 5, "New volume label:", ttk.Entry(tab, textvariable=self.disk_tool_label, width=52))
        ttk.Button(tab, text="Apply label", command=self._change_disk_tool_label).grid(row=5, column=2, sticky=tk.W, padx=(8, 0), pady=5)
        self._field(tab, 6, "New drive letter:", ttk.Entry(tab, textvariable=self.disk_tool_new_letter, width=15), "Example: E:")
        ttk.Button(tab, text="Change letter", command=self._change_disk_tool_letter).grid(row=6, column=2, sticky=tk.W, padx=(8, 0), pady=5)
        ttk.Separator(tab).grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Format volume", style="Heading.TLabel").grid(row=8, column=0, columnspan=3, sticky=tk.W)
        self._field(tab, 9, "File system:", ttk.Combobox(tab, textvariable=self.disk_tool_file_system, values=("NTFS", "exFAT", "FAT32"), state="readonly", width=48))
        self._field(tab, 10, "New label:", ttk.Entry(tab, textvariable=self.disk_tool_label, width=52))
        self._field(tab, 11, "Type selected letter:", ttk.Entry(tab, textvariable=self.disk_tool_confirmation, width=15), "Example: E (or E:); required to format")
        ttk.Button(tab, text="Format volume", style="Danger.TButton", command=self._format_disk_tool_volume).grid(row=12, column=1, sticky=tk.W, pady=12)
        self._configure_grid(tab)
        self.after(350, self._refresh_disk_tool_volumes)

    def _build_physical_disks_tab(self) -> None:
        container = self._physical_disks_tab
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        tab = ttk.Frame(canvas, padding=14)
        window = canvas.create_window((0, 0), window=tab, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        tab.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        ttk.Label(tab, text="Physical Disk and Partition Management", style="Heading.TLabel").grid(row=0, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(tab, text="Manage partition layouts with DiskPart. Clean disk permanently removes every partition; it does not restore a manufacturer's recovery image.", foreground="#9c0006", wraplength=820).grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(4, 10))
        self.physical_disks: dict[str, int] = {}
        self.physical_partitions: dict[str, int] = {}
        self.physical_resize_volumes: dict[str, str] = {}
        self.physical_disk = tk.StringVar()
        self.physical_partition = tk.StringVar()
        self.physical_create_size = tk.StringVar()
        self.physical_create_file_system = tk.StringVar(value="NTFS")
        self.physical_create_label = tk.StringVar()
        self.physical_create_letter = tk.StringVar()
        self.physical_resize_volume = tk.StringVar()
        self.physical_resize_size = tk.StringVar()
        self.physical_clean_confirmation = tk.StringVar()
        self.physical_disk_combo = ttk.Combobox(tab, textvariable=self.physical_disk, state="readonly", width=76)
        self.physical_disk_combo.bind("<<ComboboxSelected>>", lambda _: self._refresh_physical_partitions())
        self._field(tab, 2, "Physical disk:", self.physical_disk_combo, button=("Refresh disks", self._refresh_physical_disks))
        ttk.Label(tab, text="Partitions on selected disk:").grid(row=3, column=0, sticky=tk.NW, pady=(7, 0))
        self.physical_partition_list = tk.Listbox(tab, height=6, exportselection=False)
        self.physical_partition_list.grid(row=3, column=1, columnspan=2, sticky=tk.NSEW, pady=(7, 0))
        ttk.Button(tab, text="Refresh partitions", command=self._refresh_physical_partitions).grid(row=3, column=3, sticky=tk.NW, padx=(8, 0), pady=(7, 0))
        ttk.Separator(tab).grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Create primary partition in unallocated space", style="Heading.TLabel").grid(row=5, column=0, columnspan=4, sticky=tk.W)
        self._field(tab, 6, "Size (MiB, blank = all):", ttk.Entry(tab, textvariable=self.physical_create_size, width=20), "Uses contiguous unallocated space")
        self._field(tab, 7, "File system:", ttk.Combobox(tab, textvariable=self.physical_create_file_system, values=("NTFS", "exFAT", "FAT32"), state="readonly", width=48))
        self._field(tab, 8, "Label:", ttk.Entry(tab, textvariable=self.physical_create_label, width=52))
        self._field(tab, 9, "Drive letter (optional):", ttk.Entry(tab, textvariable=self.physical_create_letter, width=15), "Example: F:")
        ttk.Button(tab, text="Create and format partition", command=self._create_physical_partition).grid(row=10, column=1, sticky=tk.W, pady=(4, 10))
        ttk.Separator(tab).grid(row=11, column=0, columnspan=4, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Delete selected partition", style="Heading.TLabel").grid(row=12, column=0, columnspan=4, sticky=tk.W)
        ttk.Button(tab, text="Delete selected partition", style="Danger.TButton", command=self._delete_physical_partition).grid(row=13, column=1, sticky=tk.W, pady=(5, 10))
        ttk.Separator(tab).grid(row=14, column=0, columnspan=4, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Resize an assigned volume", style="Heading.TLabel").grid(row=15, column=0, columnspan=4, sticky=tk.W)
        self.physical_resize_combo = ttk.Combobox(tab, textvariable=self.physical_resize_volume, state="readonly", width=68)
        self._field(tab, 16, "Volume:", self.physical_resize_combo, button=("Refresh volumes", self._refresh_physical_resize_volumes))
        self._field(tab, 17, "Size (MiB):", ttk.Entry(tab, textvariable=self.physical_resize_size, width=20), "Shrink requires a size; extend blank uses all contiguous space")
        controls = ttk.Frame(tab)
        controls.grid(row=18, column=1, sticky=tk.W, pady=(4, 10))
        ttk.Button(controls, text="Extend volume", command=lambda: self._resize_physical_volume(extend=True)).pack(side=tk.LEFT)
        ttk.Button(controls, text="Shrink volume", command=lambda: self._resize_physical_volume(extend=False)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Separator(tab).grid(row=19, column=0, columnspan=4, sticky=tk.EW, pady=10)
        ttk.Label(tab, text="Factory reset to an empty disk", style="Heading.TLabel").grid(row=20, column=0, columnspan=4, sticky=tk.W)
        ttk.Label(tab, text="This runs DiskPart CLEAN: all partitions and data on the selected physical disk are removed. It cannot restore OEM recovery partitions.", foreground="#9c0006", wraplength=820).grid(row=21, column=0, columnspan=4, sticky=tk.W, pady=(4, 4))
        self._field(tab, 22, "Type selected disk number:", ttk.Entry(tab, textvariable=self.physical_clean_confirmation, width=15), "Required to clean the disk")
        ttk.Button(tab, text="Clean entire disk", style="Danger.TButton", command=self._clean_physical_disk).grid(row=23, column=1, sticky=tk.W, pady=(4, 0))
        self._configure_grid(tab)
        tab.rowconfigure(3, weight=1)
        self.after(450, self._refresh_physical_disks)

    def _build_network_tab(self) -> None:
        tab = self._network_tab
        ttk.Label(tab, text="Network Configuration and SMB Storage", style="Heading.TLabel").grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Label(tab, text="Credentials are requested only when mapping a share and are never saved or logged.", wraplength=760).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(4, 14))
        self.adapter = tk.StringVar()
        self.ip_address = tk.StringVar()
        self.subnet_mask = tk.StringVar(value="255.255.255.0")
        self.gateway = tk.StringVar()
        self.dns_servers = tk.StringVar()
        self._field(tab, 2, "Network adapter:", ttk.Combobox(tab, textvariable=self.adapter, width=48), button=("Refresh", self._refresh_adapters))
        self._field(tab, 3, "IPv4 address:", ttk.Entry(tab, textvariable=self.ip_address, width=52))
        self._field(tab, 4, "Subnet mask:", ttk.Entry(tab, textvariable=self.subnet_mask, width=52))
        self._field(tab, 5, "Default gateway:", ttk.Entry(tab, textvariable=self.gateway, width=52))
        self._field(tab, 6, "DNS servers:", ttk.Entry(tab, textvariable=self.dns_servers, width=52), "Comma-separated")
        network_actions = ttk.Frame(tab)
        network_actions.grid(row=7, column=1, sticky=tk.W, pady=12)
        ttk.Button(network_actions, text="Apply IPv4 configuration", command=self._configure_network).pack(side=tk.LEFT)
        ttk.Button(network_actions, text="Enable DHCP (IPv4 + DNS)", command=self._enable_dhcp).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Separator(tab).grid(row=8, column=0, columnspan=3, sticky=tk.EW, pady=8)
        self.share_drive = tk.StringVar(value="Z:")
        self.share_path = tk.StringVar()
        self._field(tab, 9, "Map drive letter:", ttk.Entry(tab, textvariable=self.share_drive, width=15))
        self._field(tab, 10, "SMB share path:", ttk.Entry(tab, textvariable=self.share_path, width=52), "Example: \\server\\deployment")
        ttk.Button(tab, text="Connect SMB share", command=self._connect_share).grid(row=11, column=1, sticky=tk.W, pady=12)
        ttk.Button(tab, text="Disconnect drive", command=self._disconnect_share).grid(row=11, column=1, sticky=tk.E, pady=12)
        self._configure_grid(tab)
        self.after(400, self._refresh_adapters)

    def _build_logs_tab(self) -> None:
        self.log_text = tk.Text(self._logs_tab, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        toolbar = ttk.Frame(self._logs_tab)
        toolbar.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(toolbar, text="Save log", command=self._save_log).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Clear view", command=self._clear_log).pack(side=tk.LEFT, padx=8)

    def _build_text_editor_tab(self) -> None:
        tab = self._text_editor_tab
        ttk.Label(tab, text="Text File Editor", style="Heading.TLabel").pack(anchor=tk.W)
        ttk.Label(
            tab,
            text="Open and edit startup-config.ini, command files, and other text files stored on local WinPE media. "
            "Saving changes applies them to the current media immediately; startup configuration changes take effect after reboot.",
            wraplength=880,
        ).pack(anchor=tk.W, pady=(4, 10))
        self.text_editor_path = tk.StringVar()
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(toolbar, text="Open...", command=self._open_text_file).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Open startup-config.ini", command=self._open_startup_configuration_in_editor).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Save", command=self._save_text_file).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Save as...", command=lambda: self._save_text_file(save_as=True)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(tab, textvariable=self.text_editor_path, foreground="#555555").pack(anchor=tk.W, pady=(0, 6))
        editor_frame = ttk.Frame(tab)
        editor_frame.pack(fill=tk.BOTH, expand=True)
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self.text_editor = tk.Text(editor_frame, wrap=tk.NONE, undo=True, font=("Consolas", 10))
        vertical_scrollbar = ttk.Scrollbar(editor_frame, orient=tk.VERTICAL, command=self.text_editor.yview)
        horizontal_scrollbar = ttk.Scrollbar(editor_frame, orient=tk.HORIZONTAL, command=self.text_editor.xview)
        self.text_editor.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        self.text_editor.grid(row=0, column=0, sticky=tk.NSEW)
        vertical_scrollbar.grid(row=0, column=1, sticky=tk.NS)
        horizontal_scrollbar.grid(row=1, column=0, sticky=tk.EW)

    def _disk_button(self, parent: tk.Widget, text: str, command: Callable[[], None], **grid: object) -> None:
        style = grid.pop("style", None)
        button = ttk.Button(parent, text=text, command=command, **({"style": style} if style else {}))
        button.grid(**grid)
        self._disk_buttons.append(button)

    def _update_disk_buttons(self) -> None:
        # Only global jobs block every disk. Per-disk conflicts are checked at
        # the backend after the selected target is known.
        state = tk.DISABLED if self._operation_locks.global_active else tk.NORMAL
        for button in self._disk_buttons:
            button.configure(state=state)

    def _disk_for_path(self, path: Path) -> int | None:
        return path_disk_number(path, self._deployment.single_disk_number_for_drive)

    def _run_disk_operation(self, name: str, action: Callable[[], None], *, disk: int | None = None,
                            drive: str | None = None, on_finished: Callable[[bool], None] | None = None) -> None:
        # Resolve BEFORE taking the lock. Unknown media is conservatively exclusive.
        if drive is not None:
            try:
                disk = self._disk_for_path(Path(drive + "\\"))
            except (OSError, ValueError, RuntimeError):
                disk = None
        self._run_background(name, action, disks={disk} if disk is not None else set(),
                             unknown=disk is None, on_finished=on_finished)

    def _field(self, parent: ttk.Frame, row: int, label: str, widget: tk.Widget, hint: str = "", button: tuple[str, object] | None = None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 10), pady=5)
        widget.grid(row=row, column=1, sticky=tk.EW, pady=5)
        if button:
            ttk.Button(parent, text=button[0], command=button[1]).grid(row=row, column=2, sticky=tk.W, padx=(8, 0), pady=5)
        elif hint:
            ttk.Label(parent, text=hint, foreground="#555555").grid(row=row, column=2, sticky=tk.W, padx=(8, 0), pady=5)

    def _path_field(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, *, save: bool = False, inspect_button: bool = False) -> None:
        self._field(parent, row, label, ttk.Entry(parent, textvariable=variable, width=52))
        action = ("Save as...", lambda: self._choose_wim(variable, save=True)) if save else ("Browse...", lambda: self._choose_wim(variable))
        ttk.Button(parent, text=action[0], command=action[1]).grid(row=row, column=2, sticky=tk.W, padx=(8, 0), pady=5)
        if inspect_button:
            ttk.Button(parent, text="Inspect WIM", command=self._inspect_wim).grid(row=row, column=3, sticky=tk.W, padx=(8, 0), pady=5)

    @staticmethod
    def _configure_grid(tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)

    def _choose_wim(self, variable: tk.StringVar, *, save: bool = False) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".wim", filetypes=[("Windows Image", "*.wim")]) if save else filedialog.askopenfilename(filetypes=[("Windows Image", "*.wim"), ("All files", "*.*")])
        if path:
            variable.set(path)

    def _choose_driver_directory(self) -> None:
        path = self._choose_directory("Select Extracted Driver Folder", self.driver_directory.get())
        if path:
            self.driver_directory.set(path)

    def _choose_mount_parent(self) -> None:
        current_mount = Path(self.mount_directory.get())
        parent = self._choose_directory("Select Mount Parent Folder", str(current_mount.parent))
        if parent:
            self.mount_directory.set(str(Path(parent) / "WimMount"))

    def _choose_image_destination(self) -> None:
        value = simpledialog.askstring("Custom Image Path", "Folder inside the image (example: Install\\Packages):", initialvalue=self.image_destination.get(), parent=self)
        if value is None:
            return
        try:
            self.image_destination.set(validate_relative_image_destination(value))
        except ValueError as error:
            messagebox.showerror("Invalid image path", str(error), parent=self)

    def _choose_directory(self, title: str, initial_directory: str = "") -> str:
        """Show a WinPE-safe folder browser instead of Tk's incomplete native dialog."""
        roots = self._directory_roots()
        initial = Path(initial_directory) if initial_directory else roots[0]
        if not initial.is_dir():
            initial = next((root for root in roots if root.is_dir()), roots[0])
        result = tk.StringVar(value="")
        path_value = tk.StringVar(value=str(initial))
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry("700x470")
        dialog.minsize(580, 380)
        dialog.transient(self)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)

        ttk.Label(dialog, text="Choose a folder", style="Heading.TLabel").grid(row=0, column=0, sticky=tk.W, padx=14, pady=(14, 4))
        ttk.Label(dialog, text="Select a drive, then open folders. Click Select folder to choose the current folder.", wraplength=650).grid(row=1, column=0, sticky=tk.W, padx=14, pady=(0, 10))
        navigation = ttk.Frame(dialog)
        navigation.grid(row=2, column=0, sticky=tk.NSEW, padx=14)
        navigation.columnconfigure(1, weight=1)
        navigation.rowconfigure(2, weight=1)
        roots_by_display = {self._directory_root_display(root): root for root in roots}
        selected_root = next((root for root in roots if str(initial).upper().startswith(str(root).upper())), roots[0])
        root_value = tk.StringVar(value=self._directory_root_display(selected_root))
        ttk.Label(navigation, text="Drive:").grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        root_combo = ttk.Combobox(navigation, textvariable=root_value, values=list(roots_by_display), state="readonly", width=58)
        root_combo.grid(row=0, column=1, sticky=tk.EW, pady=(0, 6))
        ttk.Label(navigation, text="Current folder:").grid(row=1, column=0, sticky=tk.NW, pady=(0, 6))
        ttk.Entry(navigation, textvariable=path_value, state="readonly").grid(row=1, column=1, sticky=tk.EW, pady=(0, 6))
        folder_list = tk.Listbox(navigation, exportselection=False)
        folder_list.grid(row=2, column=0, columnspan=2, sticky=tk.NSEW)
        scrollbar = ttk.Scrollbar(navigation, orient=tk.VERTICAL, command=folder_list.yview)
        scrollbar.grid(row=2, column=2, sticky=tk.NS)
        folder_list.configure(yscrollcommand=scrollbar.set)
        actions = ttk.Frame(dialog)
        actions.grid(row=3, column=0, sticky=tk.EW, padx=14, pady=14)

        def current_path() -> Path:
            return Path(path_value.get())

        def populate(path: Path) -> None:
            folder_list.delete(0, tk.END)
            try:
                entries = self._list_subdirectories(path)
            except OSError as error:
                self._logger.warning("Could not list folder %s: %s", path, error)
                messagebox.showerror("Cannot open folder", f"Could not list:\n{path}\n\n{error}", parent=dialog)
                return
            path_value.set(str(path))
            for entry in entries:
                folder_list.insert(tk.END, entry.name)

        def open_selected(_: object | None = None) -> None:
            selection = folder_list.curselection()
            if selection:
                populate(current_path() / folder_list.get(selection[0]))

        def choose_root(_: object | None = None) -> None:
            populate(roots_by_display[root_value.get()])

        def go_up() -> None:
            current = current_path()
            if current.parent != current:
                populate(current.parent)

        def create_folder() -> None:
            name = simpledialog.askstring("Create folder", "New folder name:", parent=dialog)
            if name is None:
                return
            name = name.strip()
            if not name:
                messagebox.showerror("Folder name required", "Enter a folder name.", parent=dialog)
                return
            if any(character in '\\/:*?"<>|' for character in name):
                messagebox.showerror("Invalid folder name", "The folder name contains unsupported characters.", parent=dialog)
                return
            target = current_path() / name
            try:
                target.mkdir()
            except FileExistsError:
                messagebox.showerror("Folder already exists", f"The folder already exists:\n{target}", parent=dialog)
                return
            except OSError as error:
                messagebox.showerror("Cannot create folder", f"Could not create:\n{target}\n\n{error}", parent=dialog)
                return
            populate(current_path())
            for index, entry in enumerate(self._list_subdirectories(current_path())):
                if entry.name == name:
                    folder_list.selection_set(index)
                    folder_list.see(index)
                    break

        def accept() -> None:
            result.set(str(current_path()))
            dialog.destroy()

        ttk.Button(actions, text="Up", command=go_up).pack(side=tk.LEFT)
        ttk.Button(actions, text="New folder", command=create_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Select folder", command=accept).pack(side=tk.RIGHT, padx=(0, 8))
        root_combo.bind("<<ComboboxSelected>>", choose_root)
        folder_list.bind("<Double-Button-1>", open_selected)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        populate(initial)
        self.wait_window(dialog)
        return result.get()

    @staticmethod
    def _directory_roots() -> list[Path]:
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except AttributeError:
            return [Path(Path.cwd().anchor) if Path.cwd().anchor else Path.cwd()]
        roots = [Path(f"{chr(ord('A') + index)}:\\") for index in range(26) if drive_mask & (1 << index)]
        return roots or [Path.cwd()]

    @staticmethod
    def _directory_root_display(root: Path) -> str:
        return f"{root}  (WinPE RAM disk)" if str(root).upper().startswith("X:") else str(root)

    @staticmethod
    def _list_subdirectories(directory: Path) -> list[Path]:
        return sorted((entry for entry in directory.iterdir() if entry.is_dir()), key=lambda entry: entry.name.casefold())

    def _refresh_disks(self) -> None:
        self._run_background("Refreshing disks", self._load_disks, quiet=True)

    def _load_disks(self) -> None:
        disks = self._deployment.list_disks()
        self.after(0, lambda: self._set_disks(disks))
        self._log(f"Found {len(disks)} disk(s).")

    def _set_disks(self, disks: list) -> None:
        selected_disk_number = self._selected_disk_number()
        self.disk_list.delete(0, tk.END)
        for disk in disks:
            self.disk_list.insert(tk.END, disk.display_name())
        if selected_disk_number is not None:
            self._select_disk_number(selected_disk_number)

    def _selected_disk_number(self) -> int | None:
        selection = self.disk_list.curselection()
        if not selection:
            return None
        return self._disk_number_from_display(self.disk_list.get(selection[0]))

    @staticmethod
    def _disk_number_from_display(display: str) -> int | None:
        match = re.match(r"^Disk\s+(\d+):", display)
        return int(match.group(1)) if match else None

    def _select_disk_number(self, disk_number: int) -> bool:
        for index in range(self.disk_list.size()):
            if self._disk_number_from_display(self.disk_list.get(index)) == disk_number:
                self.disk_list.selection_clear(0, tk.END)
                self.disk_list.selection_set(index)
                self.disk_list.see(index)
                return True
        return False

    def _refresh_physical_disks(self) -> None:
        self._run_background(
            "Refreshing physical disks",
            self._load_physical_disks,
            quiet=True,
            on_finished=lambda succeeded: self.after(0, lambda: self._refresh_physical_details(succeeded)),
        )

    def _refresh_physical_details(self, succeeded: bool) -> None:
        if not succeeded:
            return
        self._refresh_physical_partitions()
        self.after(100, self._refresh_physical_resize_volumes)

    def _load_physical_disks(self) -> None:
        disks = self._deployment.list_disks()
        self.after(0, lambda: self._set_physical_disks(disks))

    def _set_physical_disks(self, disks: list) -> None:
        self.physical_disks = {disk.display_name(): disk.number for disk in disks}
        displays = list(self.physical_disks)
        self.physical_disk_combo["values"] = displays
        if displays and self.physical_disk.get() not in self.physical_disks:
            self.physical_disk.set(displays[0])

    def _selected_physical_disk_number(self) -> int | None:
        return self.physical_disks.get(self.physical_disk.get())

    def _refresh_physical_partitions(self) -> None:
        disk_number = self._selected_physical_disk_number()
        if disk_number is None:
            self.physical_partition_list.delete(0, tk.END)
            return

        def load() -> None:
            partitions = self._deployment.list_partitions(disk_number)
            self.after(0, lambda: self._set_physical_partitions(partitions))

        self._run_background("Refreshing partitions", load, quiet=True)

    def _set_physical_partitions(self, partitions: list) -> None:
        self.physical_partitions = {partition.display_name(): partition.number for partition in partitions}
        self.physical_partition_list.delete(0, tk.END)
        for display in self.physical_partitions:
            self.physical_partition_list.insert(tk.END, display)

    def _refresh_physical_resize_volumes(self) -> None:
        def load() -> None:
            volumes = self._deployment.list_volumes()
            self.after(0, lambda: self._set_physical_resize_volumes(volumes))

        self._run_background("Refreshing resize volumes", load, quiet=True)

    def _set_physical_resize_volumes(self, volumes: list[VolumeInfo]) -> None:
        self.physical_resize_volumes = {volume.display_name(): volume.drive_letter for volume in volumes}
        displays = list(self.physical_resize_volumes)
        self.physical_resize_combo["values"] = displays
        if displays and self.physical_resize_volume.get() not in self.physical_resize_volumes:
            self.physical_resize_volume.set(displays[0])

    def _create_physical_partition(self) -> None:
        disk_number = self._selected_physical_disk_number()
        if disk_number is None:
            messagebox.showerror("Physical disk required", "Select a physical disk first.", parent=self)
            return
        size_text = self.physical_create_size.get().strip()
        try:
            size_mib = int(size_text) if size_text else None
        except ValueError:
            messagebox.showerror("Invalid size", "Enter a whole number of MiB, or leave the size blank.", parent=self)
            return
        if not messagebox.askyesno(
            "Create partition",
            f"Create and format a new primary partition on Disk {disk_number}?\n\nOnly contiguous unallocated space can be used.",
            icon=messagebox.WARNING,
            parent=self,
        ):
            return
        self._run_background(
            "Creating partition",
            lambda: self._deployment.create_primary_partition(
                disk_number,
                size_mib,
                self.physical_create_file_system.get(),
                self.physical_create_label.get(),
                self.physical_create_letter.get() or None,
            ),
            on_finished=self._refresh_physical_disk_state_after_operation, disks={disk_number},
        )

    def _delete_physical_partition(self) -> None:
        disk_number = self._selected_physical_disk_number()
        selection = self.physical_partition_list.curselection()
        if disk_number is None or not selection:
            messagebox.showerror("Partition required", "Select a physical disk and one partition first.", parent=self)
            return
        display = self.physical_partition_list.get(selection[0])
        partition_number = self.physical_partitions[display]
        if not messagebox.askyesno(
            "Delete partition",
            f"Delete Partition {partition_number} on Disk {disk_number}?\n\nAll files on this partition will be permanently erased.",
            icon=messagebox.WARNING,
            parent=self,
        ):
            return
        self._run_background(
            "Deleting partition",
            lambda: self._deployment.delete_partition(disk_number, partition_number),
            on_finished=self._refresh_physical_disk_state_after_operation, disks={disk_number},
        )

    def _resize_physical_volume(self, *, extend: bool) -> None:
        drive = self.physical_resize_volumes.get(self.physical_resize_volume.get())
        if not drive:
            messagebox.showerror("Volume required", "Select an assigned volume first.", parent=self)
            return
        size_text = self.physical_resize_size.get().strip()
        try:
            size_mib = int(size_text) if size_text else None
        except ValueError:
            messagebox.showerror("Invalid size", "Enter a whole number of MiB.", parent=self)
            return
        action = "extend" if extend else "shrink"
        if not messagebox.askyesno(
            f"{action.title()} volume",
            f"{action.title()} {drive}" + (f" by {size_mib} MiB?" if size_mib else " using all contiguous available space?"),
            icon=messagebox.WARNING,
            parent=self,
        ):
            return
        self._run_disk_operation(
            f"{action.title()}ing volume",
            lambda: self._deployment.resize_volume(drive, extend=extend, size_mib=size_mib),
            drive=drive,
            on_finished=self._refresh_physical_disk_state_after_operation,
        )

    def _clean_physical_disk(self) -> None:
        disk_number = self._selected_physical_disk_number()
        if disk_number is None:
            messagebox.showerror("Physical disk required", "Select a physical disk first.", parent=self)
            return
        if self.physical_clean_confirmation.get().strip() != str(disk_number):
            messagebox.showerror("Confirmation does not match", f"Type {disk_number} to clean Disk {disk_number}.", parent=self)
            return
        if not messagebox.askyesno(
            "Final destructive confirmation",
            f"Run DiskPart CLEAN on Disk {disk_number}?\n\nEvery partition and all data on this disk will be permanently removed. This cannot restore OEM recovery partitions.",
            icon=messagebox.WARNING,
            parent=self,
        ):
            return
        self._run_background(
            "Cleaning entire disk",
            lambda: self._deployment.clean_disk(disk_number),
            on_finished=self._refresh_physical_disk_state_after_operation, disks={disk_number},
        )

    def _refresh_physical_disk_state_after_operation(self, succeeded: bool) -> None:
        if succeeded:
            self.after(0, self._refresh_physical_disks)

    def _capture_image(self) -> None:
        source_display = self.capture_source.get()
        destination_display = self.capture_destination.get()
        source = self._capture_volume_letters.get(source_display, "")
        destination = self._capture_volume_letters.get(destination_display, "")
        filename = self.capture_filename.get().strip()
        if not source or not destination:
            messagebox.showerror("Volumes required", "Select both the offline source volume and the WIM destination volume.", parent=self)
            return
        if source == destination:
            messagebox.showerror("Invalid destination", "Store the WIM on a different volume than the captured source.", parent=self)
            return
        if destination_info := self._capture_volumes.get(destination):
            if destination_info.file_system.upper() in {"FAT", "FAT12", "FAT16", "FAT32"}:
                messagebox.showerror(
                    "FAT32 destination is not supported",
                    f"{destination} uses {destination_info.file_system}. A single WIM can exceed FAT32's 4 GiB file limit. "
                    "Format or use an NTFS or exFAT destination volume.",
                    parent=self,
                )
                return
        if not filename:
            messagebox.showerror("WIM file name required", "Enter a filename for the captured WIM.", parent=self)
            return
        if not filename.lower().endswith(".wim"):
            filename += ".wim"
        if "\\" in filename or "/" in filename:
            messagebox.showerror("Invalid WIM file name", "Enter only a filename, not a path. The destination is selected from the list.", parent=self)
            return
        wim = f"{destination}\\{filename}"
        source_info = self._capture_volumes.get(source)
        destination_info = self._capture_volumes.get(destination)
        if source_info and destination_info and destination_info.free_bytes < source_info.used_bytes:
            proceed = messagebox.askyesno(
                "Low destination free space",
                f"Source volume currently uses {source_info.used_gib:.1f} GiB, but destination has only {destination_info.free_gib:.1f} GiB free.\n\n"
                "Maximum compression may reduce the final WIM size, but capture can still run out of space. Continue?",
                icon=messagebox.WARNING,
                parent=self,
            )
            if not proceed:
                return
        name = self.capture_name.get()
        description = self.capture_description.get()
        compression = CaptureCompression(self.capture_compression.get())
        self.capture_progress_value.set(0)
        self.capture_progress_text.set("Starting capture...")
        self._reset_progress_estimate("capture")
        self._run_background("Capturing image", lambda: self._deployment.capture_image(source, validate_wim_path(wim), name, description, compression, self._make_progress_reporter("capture")))

    def _refresh_capture_volumes(self) -> None:
        self._run_background("Refreshing volumes", self._load_capture_volumes, quiet=True)

    def _load_capture_volumes(self) -> None:
        volumes = self._deployment.list_volumes()
        self.after(0, lambda: self._set_capture_volumes(volumes))
        self._log(f"Found {len(volumes)} drive-letter volume(s).")

    def _set_capture_volumes(self, volumes: list[VolumeInfo]) -> None:
        self._capture_volumes = {volume.drive_letter: volume for volume in volumes}
        self._capture_volume_letters = {volume.display_name(): volume.drive_letter for volume in volumes}
        displays = list(self._capture_volume_letters)
        self.capture_source_combo["values"] = displays
        self.capture_destination_combo["values"] = displays
        if displays and self.capture_source.get() not in self._capture_volume_letters:
            self.capture_source.set(displays[0])
        if len(displays) > 1 and self.capture_destination.get() not in self._capture_volume_letters:
            self.capture_destination.set(displays[1])

    def _refresh_disk_tool_volumes(self) -> None:
        self._run_background("Refreshing volumes", self._load_disk_tool_volumes, quiet=True)

    def _load_disk_tool_volumes(self) -> None:
        volumes = self._deployment.list_volumes()
        self.after(0, lambda: self._set_disk_tool_volumes(volumes))

    def _set_disk_tool_volumes(self, volumes: list[VolumeInfo]) -> None:
        self.disk_tool_volumes = {volume.display_name(): volume for volume in volumes}
        displays = list(self.disk_tool_volumes)
        self.disk_tool_volume_combo["values"] = displays
        if displays and self.disk_tool_volume.get() not in self.disk_tool_volumes:
            self.disk_tool_volume.set(displays[0])
        self._sync_disk_tool_fields()

    def _selected_disk_tool_volume(self) -> VolumeInfo | None:
        return self.disk_tool_volumes.get(self.disk_tool_volume.get())

    def _sync_disk_tool_fields(self) -> None:
        if volume := self._selected_disk_tool_volume():
            self.disk_tool_label.set(volume.label)
            self.disk_tool_confirmation.set("")

    def _change_disk_tool_label(self) -> None:
        volume = self._selected_disk_tool_volume()
        if volume is None:
            messagebox.showerror("Volume required", "Select a volume first.", parent=self)
            return
        self._run_disk_operation(
            "Changing volume label",
            lambda: self._deployment.change_volume_label(volume.drive_letter, self.disk_tool_label.get()),
            drive=volume.drive_letter,
        )

    def _change_disk_tool_letter(self) -> None:
        volume = self._selected_disk_tool_volume()
        if volume is None:
            messagebox.showerror("Volume required", "Select a volume first.", parent=self)
            return
        self._run_disk_operation(
            "Changing drive letter",
            lambda: self._deployment.change_volume_letter(volume.drive_letter, self.disk_tool_new_letter.get()),
            drive=volume.drive_letter,
        )

    def _format_disk_tool_volume(self) -> None:
        volume = self._selected_disk_tool_volume()
        if volume is None:
            messagebox.showerror("Volume required", "Select a volume first.", parent=self)
            return
        confirmation = self.disk_tool_confirmation.get().strip().upper().rstrip(":")
        if confirmation != volume.drive_letter[0]:
            messagebox.showerror("Confirmation does not match", f"Type {volume.drive_letter[0]} to format this volume.", parent=self)
            return
        if not messagebox.askyesno(
            "Format volume",
            f"Format {volume.drive_letter}? All files on this volume will be erased.",
            icon=messagebox.WARNING,
            parent=self,
        ):
            return
        self._run_disk_operation(
            "Formatting volume",
            lambda: self._deployment.format_volume(
                volume.drive_letter,
                self.disk_tool_file_system.get(),
                self.disk_tool_label.get(),
            ),
            drive=volume.drive_letter,
        )

    def _inspect_wim(self) -> None:
        wim = self.deploy_wim.get()
        def inspect() -> None:
            images = self._deployment.inspect_wim(validate_wim_path(wim))
            details = "\n".join(f"Index {image.index}: {image.name}\n  {image.description}" for image in images) or "No image indexes found."
            self.after(0, lambda: messagebox.showinfo("WIM contents", details, parent=self))
        self._run_background("Inspecting WIM", inspect)

    def _load_wim_indexes(self, wim_variable: tk.StringVar, combo: ttk.Combobox, index_variable: tk.StringVar) -> None:
        wim = wim_variable.get()

        def load() -> None:
            images = self._deployment.inspect_wim(validate_wim_path(wim))
            self.after(0, lambda: self._set_wim_indexes(images, combo, index_variable))

        self._run_background("Loading WIM indexes", load, quiet=True)

    @staticmethod
    def _set_wim_indexes(images: list, combo: ttk.Combobox, index_variable: tk.StringVar) -> None:
        values = [f"{image.index} — {image.name or 'Unnamed image'}" for image in images]
        combo["values"] = values
        if values:
            try:
                current_index = validate_image_index(index_variable.get())
            except ValueError:
                current_index = None
            index_variable.set(next((value for image, value in zip(images, values) if image.index == current_index), values[0]))

    def _deploy_image(self) -> None:
        selection = self.disk_list.curselection()
        if not selection:
            messagebox.showerror("Target disk required", "Select a target disk from the list first.", parent=self)
            return
        display = self.disk_list.get(selection[0])
        disk_number = int(display.split(":", 1)[0].split()[1])
        if self.disk_confirmation.get().strip() != str(disk_number):
            messagebox.showerror("Confirmation does not match", f"Type {disk_number} in the confirmation field to deploy to the selected disk.", parent=self)
            return
        if not messagebox.askyesno("Final destructive confirmation", f"Deploy to {display}?\n\nAll data on this disk will be permanently erased.", icon=messagebox.WARNING, parent=self):
            return
        wim, index, firmware = self.deploy_wim.get(), self.deploy_index.get(), self.firmware.get()
        restart_after_deploy = self.restart_after_deploy.get()
        def deploy() -> None:
            self._deployment.deploy_image(validate_wim_path(wim), validate_image_index(index), disk_number, FirmwareType(firmware), self._make_progress_reporter("deploy"))
            if restart_after_deploy:
                self._log("Deployment completed successfully. Restarting WinPE now.")
                self._power.restart()
        self.deploy_progress_value.set(0)
        self.deploy_progress_text.set("Starting deployment...")
        self._reset_progress_estimate("deploy")
        self._run_background("Deploying image", deploy)

    def _make_progress_reporter(self, operation: str) -> Callable[[str], None]:
        def report(message: str) -> None:
            self._log(message)
            self.after(0, lambda: self._update_progress(operation, message))
        return report

    def _update_progress(self, operation: str, message: str) -> None:
        match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", message)
        value = min(100.0, float(match.group(1))) if match else None
        if operation == "capture":
            if value is not None:
                self.capture_progress_value.set(value)
            self.capture_progress_text.set(self._progress_message_with_eta(operation, message, value))
        elif operation == "deploy":
            if value is not None:
                self.deploy_progress_value.set(value)
            self.deploy_progress_text.set(self._progress_message_with_eta(operation, message, value))
        else:
            if value is not None:
                self.service_progress_bar.stop()
                self.service_progress_value.set(value)
            self.service_progress_text.set(message)

    def _add_files(self) -> None:
        for path in filedialog.askopenfilenames(parent=self):
            self.injection_sources.insert(tk.END, path)

    def _add_folder(self) -> None:
        path = self._choose_directory("Select Folder to Inject")
        if path:
            self.injection_sources.insert(tk.END, path)

    def _inject_files(self) -> None:
        paths = self.injection_sources.get(0, tk.END)
        wim, index, mount, destination = self.service_wim.get(), self.service_index.get(), self.mount_directory.get(), self.image_destination.get()
        self._start_service_progress("Mounting image and preparing file injection...")
        self._run_background(
            "Servicing image",
            lambda: self._deployment.inject_files(validate_wim_path(wim), validate_image_index(index), mount, paths, destination, self._make_progress_reporter("service")),
            on_finished=self._finish_service_progress,
        )

    def _inject_drivers(self) -> None:
        wim, index, mount, drivers = self.service_wim.get(), self.service_index.get(), self.mount_directory.get(), self.driver_directory.get()
        self._start_service_progress("Mounting image and preparing driver injection...")
        self._run_background(
            "Injecting drivers",
            lambda: self._deployment.inject_drivers(validate_wim_path(wim), validate_image_index(index), mount, drivers, self._make_progress_reporter("service")),
            on_finished=self._finish_service_progress,
        )

    def _open_session_driver_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Load Drivers into Current WinPE Session")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("760x480")
        dialog.minsize(620, 360)
        directory = tk.StringVar()
        summary = tk.StringVar(value="Choose a folder containing extracted .inf driver files. This does not modify a WIM.")
        ttk.Label(dialog, text="Load Drivers into Current WinPE Session", style="Heading.TLabel").pack(anchor=tk.W, padx=14, pady=(14, 4))
        ttk.Label(dialog, text="Drivers are loaded with drvload.exe for this boot only. Restarting WinPE removes them.", wraplength=700).pack(anchor=tk.W, padx=14)
        top = ttk.Frame(dialog)
        top.pack(fill=tk.X, padx=14, pady=12)
        ttk.Entry(top, textvariable=directory).pack(side=tk.LEFT, fill=tk.X, expand=True)
        driver_list = tk.Listbox(dialog)
        driver_list.pack(fill=tk.BOTH, expand=True, padx=14)

        def refresh_list() -> None:
            driver_list.delete(0, tk.END)
            try:
                files = self._session_driver_inf_files(Path(directory.get()))
            except OSError as error:
                summary.set(f"Cannot read folder: {error}")
                return
            for inf_file in files:
                driver_list.insert(tk.END, str(inf_file))
            summary.set(f"Found {len(files)} INF driver file(s).")

        def browse() -> None:
            selected = self._choose_directory("Select Extracted Driver Folder")
            if selected:
                directory.set(selected)
                refresh_list()

        def load() -> None:
            selected_directory = directory.get()
            try:
                if not self._session_driver_inf_files(Path(selected_directory)):
                    raise ValueError("Choose a folder containing at least one extracted .inf driver file.")
            except (OSError, ValueError) as error:
                messagebox.showerror("Driver folder required", str(error), parent=dialog)
                return

            result = ""

            def run() -> None:
                nonlocal result
                loaded, failures = self._deployment.load_session_drivers(selected_directory, self._make_progress_reporter("service"))
                result = f"Loaded {loaded} driver(s) into the current WinPE session."
                if failures:
                    result += f"\n\n{len(failures)} driver(s) failed. See Logs for details."

            dialog.destroy()
            self._run_background(
                "Loading drivers into current WinPE",
                run,
                on_finished=lambda succeeded: self.after(0, lambda: self._show_session_driver_result(succeeded, result)),
            )

        ttk.Button(top, text="Browse...", command=browse).pack(side=tk.LEFT, padx=(8, 0))
        footer = ttk.Frame(dialog)
        footer.pack(fill=tk.X, padx=14, pady=12)
        ttk.Label(footer, textvariable=summary).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(footer, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(footer, text="Load now", command=load).pack(side=tk.RIGHT, padx=(0, 8))

    @staticmethod
    def _session_driver_inf_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return sorted((path for path in directory.rglob("*.inf") if path.is_file()), key=lambda path: str(path).casefold())

    def _show_session_driver_result(self, succeeded: bool, result: str) -> None:
        if succeeded:
            messagebox.showinfo("Session driver loading complete", result, parent=self)
            self._refresh_after_session_driver_load(True)

    def _refresh_after_session_driver_load(self, succeeded: bool) -> None:
        if not succeeded:
            return

        def refresh() -> None:
            disks = self._deployment.list_disks()
            adapters = self._network.list_adapters()
            self.after(0, lambda: self._set_disks(disks))
            self.after(0, lambda: self._set_adapters(adapters))

        self._run_background("Refreshing hardware after driver load", refresh, quiet=True)

    @classmethod
    def _auto_driver_directories(cls) -> list[Path]:
        directories: list[Path] = []
        for root in cls._directory_roots():
            if str(root).upper().startswith("X:"):
                continue
            directory = root / MEDIA_CONTENT_DIR / "Drivers"
            if directory.is_dir() and cls._session_driver_inf_files(directory):
                directories.append(directory)
        return directories

    @classmethod
    def _startup_configuration_files(cls) -> list[Path]:
        files: list[Path] = []
        for root in cls._directory_roots():
            if str(root).upper().startswith("X:"):
                continue
            path = root / MEDIA_CONTENT_DIR / "startup-config.ini"
            if path.is_file():
                files.append(path)
        return files

    def _start_service_progress(self, message: str) -> None:
        self.service_progress_value.set(0)
        self.service_progress_text.set(message)
        self.service_progress_bar.start(12)

    def _finish_service_progress(self, succeeded: bool) -> None:
        self.service_progress_bar.stop()
        if succeeded and self.service_progress_value.get() < 100:
            self.service_progress_value.set(100)

    def _discard_mount(self) -> None:
        if messagebox.askyesno("Discard changes", "Discard all uncommitted changes in the mounted image?", icon=messagebox.WARNING, parent=self):
            mount = self.mount_directory.get()
            self._run_background("Discarding mounted image", lambda: self._deployment.discard_mounted_image(mount))

    def _refresh_adapters(self) -> None:
        def load() -> None:
            adapters = self._network.list_adapters()
            self.after(0, lambda: self._set_adapters(adapters))
        self._run_background("Refreshing network adapters", load, quiet=True)

    def _set_adapters(self, adapters: list[str]) -> None:
        for child in self._network_tab.grid_slaves(row=2, column=1):
            if isinstance(child, ttk.Combobox):
                child["values"] = adapters
        if adapters and not self.adapter.get():
            self.adapter.set(adapters[0])

    def _configure_network(self) -> None:
        adapter, address, mask, gateway, dns = self.adapter.get(), self.ip_address.get(), self.subnet_mask.get(), self.gateway.get(), self.dns_servers.get()
        self._run_background("Configuring network", lambda: self._network.configure_ipv4(adapter, address, mask, gateway, dns))

    def _enable_dhcp(self) -> None:
        adapter = self.adapter.get().strip()
        self._run_background("Enabling DHCP for network adapter", lambda: self._network.enable_dhcp(adapter))

    def _connect_share(self) -> None:
        username = simpledialog.askstring("SMB credentials", "User name (DOMAIN\\user or user):", parent=self)
        if username is None:
            return
        password = simpledialog.askstring("SMB credentials", "Password:", show="*", parent=self)
        if password is None:
            return
        drive, share = self.share_drive.get(), self.share_path.get()
        self._run_background("Connecting SMB share", lambda: self._network.connect_share(drive, share, username, password))

    def _disconnect_share(self) -> None:
        drive = self.share_drive.get()
        self._run_background("Disconnecting SMB share", lambda: self._network.disconnect_share(drive))

    @staticmethod
    def _read_text_file(path: Path) -> str:
        if not path.is_file():
            raise ValueError(f"Text file does not exist: {path}")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("The text editor supports files up to 4 MiB.")
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return path.read_text(encoding="cp1252")

    @staticmethod
    def _write_text_file(path: Path, content: str) -> None:
        if not path.name:
            raise ValueError("Choose a file name before saving.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\r\n")

    def _open_text_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            filetypes=[("Text and configuration files", "*.ini *.txt *.cmd *.bat *.ps1 *.log *.json *.xml"), ("All files", "*.*")],
        )
        if path:
            self._load_text_file(Path(path))

    def _open_startup_configuration_in_editor(self) -> None:
        files = self._startup_configuration_files()
        if len(files) == 1:
            self._load_text_file(files[0])
            return
        if not files:
            messagebox.showinfo("Startup configuration", "No active startup-config.ini was found on mounted WinPE media.", parent=self)
            return
        messagebox.showerror("Startup configuration", "More than one active startup-config.ini was found. Use Open... to select one explicitly.", parent=self)

    def _load_text_file(self, path: Path) -> None:
        try:
            with self._operation_locks.editor_access(path, self._disk_for_path):
                content = self._read_text_file(path)
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot open text file", str(error), parent=self)
            return
        self.text_editor.delete("1.0", tk.END)
        self.text_editor.insert("1.0", content)
        self.text_editor.edit_modified(False)
        self.text_editor_path.set(str(path))
        self._log(f"Opened text file for editing: {path}")

    def _save_text_file(self, *, save_as: bool = False) -> None:
        current_path = Path(self.text_editor_path.get()) if self.text_editor_path.get() else None
        if save_as or current_path is None:
            selected = filedialog.asksaveasfilename(
                parent=self,
                defaultextension=".txt",
                initialfile=current_path.name if current_path else "startup-config.ini",
                filetypes=[("Text and configuration files", "*.ini *.txt *.cmd *.bat *.ps1 *.log *.json *.xml"), ("All files", "*.*")],
            )
            if not selected:
                return
            current_path = Path(selected)
        try:
            with self._operation_locks.editor_access(current_path, self._disk_for_path):
                self._write_text_file(current_path, self.text_editor.get("1.0", "end-1c"))
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot save text file", str(error), parent=self)
            return
        self.text_editor_path.set(str(current_path))
        self.text_editor.edit_modified(False)
        self._log(f"Saved text file: {current_path}")
        messagebox.showinfo("Text file saved", f"Saved:\n{current_path}", parent=self)

    def _restart_winpe(self) -> None:
        self._power_action(
            title="Restart WinPE",
            message="Restart the computer now? Unsaved data and active operations will be interrupted.",
            operation="Restarting WinPE",
            action=self._power.restart,
        )

    def _shutdown_winpe(self) -> None:
        self._power_action(
            title="Shut Down WinPE",
            message="Shut down the computer now? Unsaved data and active operations will be interrupted.",
            operation="Shutting down WinPE",
            action=self._power.shutdown,
        )

    def _power_action(self, *, title: str, message: str, operation: str, action: Callable[[], None]) -> None:
        if self._operation_locks.active:
            self._status.set("An operation is already in progress. Please wait.")
            return
        if messagebox.askyesno(title, message, icon=messagebox.WARNING, parent=self):
            self._run_background(operation, action)

    def _run_background(
        self,
        operation: str,
        function: Callable[[], None],
        *,
        quiet: bool = False,
        on_finished: Callable[[bool], None] | None = None,
        disks: set[int] | None = None,
        unknown: bool = False,
    ) -> None:
        if self._busy and disks is None:
            if not quiet:
                self._status.set("An operation is already in progress. Please wait.")
            return
        try:
            token = self._operation_locks.acquire(operation, disks, unknown=unknown)
        except RuntimeError as error:
            if not quiet:
                self._status.set(str(error))
            return
        self._busy = self._operation_locks.active
        if hasattr(self, "_disk_buttons"):
            self._update_disk_buttons()
        if not quiet:
            self._status.set(operation + "...")
        def worker() -> None:
            succeeded = False
            try:
                function()
            except Exception as error:  # User-facing boundary: native tool failures and input errors.
                error_message = str(error) or f"{type(error).__name__} (no error text was provided)"
                self._logger.exception("%s failed: %s", operation, error_message)
                self.after(0, lambda message=error_message: messagebox.showerror("Operation failed", message, parent=self))
            else:
                succeeded = True
                self._log(operation + " finished.")
            finally:
                self.after(0, lambda: self._finish_operation(succeeded, on_finished, token))
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._operation_locks.release(token)
            self._busy = self._operation_locks.active
            raise

    def _finish_operation(self, succeeded: bool, on_finished: Callable[[bool], None] | None = None,
                          token: int | None = None) -> None:
        try:
            if on_finished:
                on_finished(succeeded)
        finally:
            if token is not None:
                self._operation_locks.release(token)
            self._busy = self._operation_locks.active
            if hasattr(self, "_disk_buttons"):
                self._update_disk_buttons()
            self._status.set("An operation is still in progress..." if self._busy else "Ready")

    def _log(self, message: str) -> None:
        self._logger.info(message)

    def _flush_log_messages(self) -> None:
        entries: list[str] = []
        while True:
            try:
                entries.append(self._messages.get_nowait())
            except queue.Empty:
                break
        if entries:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, "\n".join(entries) + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.after(150, self._flush_log_messages)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".log", initialfile=f"winpe-image-deployer-{datetime.now():%Y%m%d-%H%M%S}.log", filetypes=[("Log files", "*.log")], parent=self)
        if path:
            Path(path).write_text(self.log_text.get("1.0", tk.END), encoding="utf-8")
            messagebox.showinfo("Log saved", f"Log saved to:\n{path}", parent=self)