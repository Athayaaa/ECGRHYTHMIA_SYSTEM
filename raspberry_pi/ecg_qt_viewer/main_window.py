"""Main window and application-level coordination for the ECG viewer."""

from __future__ import annotations

import logging
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Iterable

import numpy as np
from PyQt6.QtCore import (
    QSettings,
    QSignalBlocker,
    QStandardPaths,
    QTimer,
    Qt,
    QUrl,
)
from PyQt6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .ecg_plot_widget import ECGPlotWidget
from .frame_repository import FrameRecord, FrameRepository
from .frame_validator import FrameValidator, ValidationResult
from .frame_watcher import FrameWatcher
from .viewer_settings import (
    DATA_MODES,
    DisplayCalibrationDialog,
    RAW_GRID_SPACINGS,
    RAW_VIEW_MODES,
    RAW_Y_SCALES,
    load_viewer_settings,
    restore_window_geometry,
)

try:
    from calibration_utils import estimate_bpm
except ImportError:  # Support imports from the repository root in tests/tools.
    from raspberry_pi.calibration_utils import estimate_bpm


LOGGER = logging.getLogger(__name__)
FRAME_ID_SEARCH = re.compile(r"(?:frame_)?(\d+)(?:_mv)?", re.IGNORECASE)
LEAD_LABELS = ("Lead I", "Lead II", "Lead III")
LIVE_REPLAY_SPEED = 2.0


class MainWindow(QMainWindow):
    """Coordinate read-only frame discovery, validation, plotting, and UI state."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        default_data_dir: str | Path,
        initial_frame: str | int | None = None,
        follow_live: bool | None = None,
        paper_speed_mm_s: float | None = None,
        gain_mm_mv: float | None = None,
        y_range_mv: float | None = None,
        source_mode: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ECGRHYTHMIA ECG Viewer")
        self.setMinimumSize(1024, 680)
        self._initializing = True

        self._settings = QSettings()
        self._default_data_dir = Path(default_data_dir).expanduser().resolve()
        selected_dir = self._resolve_initial_directory(data_dir)

        # Determine source mode from settings or CLI
        saved_settings = load_viewer_settings(self._settings)
        if source_mode is not None:
            normalized_mode = "raw" if "raw" in source_mode.lower() else "mv"
        else:
            normalized_mode = "raw" if saved_settings.data_mode == "Raw ADC" else "mv"
        self._initial_data_mode = (
            "Raw ADC" if normalized_mode == "raw" else "Calibrated mV"
        )

        self._repository = FrameRepository(selected_dir, source_mode=normalized_mode)
        self._validator = FrameValidator(source_mode=normalized_mode)
        self._current_record: FrameRecord | None = None
        self._current_validation: ValidationResult | None = None
        self._current_source_signature: tuple[Any, ...] | None = None
        self._updating_frame_combo = False
        self._pending_initial_frame_id: int | None = None
        self._seeking_first_valid_frame = True
        self._pending_live_record: FrameRecord | None = None
        self._live_selection_scheduled = False
        self._raw_source_data: np.ndarray | None = None
        self._zero_code: float | None = None
        self._adcmax: float | None = None
        self._estimated_bpm: float | None = None
        self._bpm_peak_count = 0
        self._bpm_reliable = False
        self._ai_prediction: dict[str, Any] | None = None
        self._ai_prediction_frame_id: int | None = None

        self._create_actions()
        self._create_control_panel()
        self._create_central_area()
        self._connect_control_signals()  # Connect after plot_widget exists
        self._create_menus()
        self._create_status_bar()
        self._apply_style()
        self._restore_window_state()
        self._restore_control_settings()
        if source_mode is not None:
            self._select_combo_text(self.data_mode_combo, self._initial_data_mode)
            self._update_mode_controls_visibility()
            self._apply_all_plot_controls()
        self._apply_cli_overrides(
            paper_speed_mm_s=paper_speed_mm_s,
            gain_mm_mv=gain_mm_mv,
            y_range_mv=y_range_mv,
        )
        self._initializing = False

        # Share the mode-aware repository.  Constructing the watcher from a
        # path would create a second repository in its default mV mode, so Raw
        # folders containing frame_*.npy would never become "stable"/ready.
        self._watcher = FrameWatcher(self._repository, parent=self)
        self._watcher.frames_changed.connect(self._on_watched_frames_changed)
        self._watcher.stable_frame_available.connect(self._on_stable_frame_available)
        self._watcher.directory_unavailable.connect(self._on_directory_unavailable)
        self._watcher.error.connect(self._on_watcher_error)

        if follow_live is not None:
            self.follow_live_checkbox.setChecked(bool(follow_live))

        self._set_data_directory(selected_dir, initial_frame=initial_frame)
        self._watcher.start()

    @property
    def current_frame_path(self) -> str | None:
        """Absolute current NPY path, used by status reporting and smoke tests."""

        if self._current_record is None:
            return None
        return str(self._current_record.npy_path)

    @property
    def current_frame_is_valid(self) -> bool:
        return bool(
            self._current_validation is not None
            and self._current_validation.can_plot
        )

    def _resolve_initial_directory(self, requested: str | Path | None) -> Path:
        if requested is not None:
            return Path(requested).expanduser().resolve()
        saved = load_viewer_settings(self._settings).last_directory
        if saved:
            saved_path = Path(saved).expanduser()
            if saved_path.is_dir():
                return saved_path.resolve()
        return self._default_data_dir

    def _create_actions(self) -> None:
        self.open_folder_action = QAction("Open Folder", self)
        self.open_folder_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_folder_action.triggered.connect(self._open_folder)

        self.refresh_action = QAction("Refresh", self)
        self.refresh_action.triggered.connect(self.refresh_frames)

        self.first_action = QAction("First", self)
        self.first_action.setShortcut(QKeySequence(Qt.Key.Key_Home))
        self.first_action.triggered.connect(self._go_first)

        self.previous_action = QAction("Previous", self)
        self.previous_action.setShortcut(QKeySequence(Qt.Key.Key_Left))
        self.previous_action.triggered.connect(self._go_previous)

        self.next_action = QAction("Next", self)
        self.next_action.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self.next_action.triggered.connect(self._go_next)

        self.latest_action = QAction("Latest", self)
        self.latest_action.setShortcut(QKeySequence(Qt.Key.Key_End))
        self.latest_action.triggered.connect(self._go_latest)

        self.replay_action = QAction("Replay", self)
        self.replay_action.triggered.connect(self._start_replay)
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self._pause_replay)
        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self._stop_replay)

        self.replay_toggle_action = QAction("Replay/Pause", self)
        self.replay_toggle_action.setShortcut(QKeySequence(Qt.Key.Key_Space))
        self.replay_toggle_action.triggered.connect(self._toggle_replay)
        self.addAction(self.replay_toggle_action)

        self.reset_view_action = QAction("Reset View", self)
        self.reset_view_action.setShortcut(QKeySequence(Qt.Key.Key_R))
        self.reset_view_action.triggered.connect(self._reset_view)

        self.auto_y_action = QAction("Auto Y", self)
        self.auto_y_action.triggered.connect(self._auto_scale_y)

        self.display_calibration_action = QAction("Display Calibration", self)
        self.display_calibration_action.triggered.connect(self._show_display_calibration)

        self.export_png_action = QAction("Export Current View as PNG", self)
        self.export_png_action.triggered.connect(self._export_png)
        self.copy_path_action = QAction("Copy Current Frame Path", self)
        self.copy_path_action.triggered.connect(self._copy_current_path)
        self.open_containing_action = QAction("Open Containing Folder", self)
        self.open_containing_action.triggered.connect(self._open_containing_folder)

        self.exit_action = QAction("Exit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

    def _create_central_area(self) -> None:
        self.plot_widget = ECGPlotWidget(self)
        self.plot_widget.replay_state_changed.connect(self._on_replay_state_changed)
        self.plot_widget.replay_finished.connect(self._on_replay_finished)

        # QTabWidget for right-side details panel
        self.info_tab_widget = QTabWidget(self)
        self.info_tab_widget.setObjectName("infoTabWidget")
        self.info_tab_widget.setMinimumWidth(260)

        def create_info_table():
            table = QTableWidget(0, 2, self)
            table.setHorizontalHeaderLabels(("Property", "Value"))
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
            table.setAlternatingRowColors(True)
            table.setWordWrap(True)
            table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            return table

        # 1. Overview Tab
        self.overview_table = create_info_table()
        self.info_table = self.overview_table  # Alias for backward/test compatibility!
        self.info_tab_widget.addTab(self.overview_table, "Overview")

        # 2. Signal Tab
        self.signal_table = create_info_table()
        self.info_tab_widget.addTab(self.signal_table, "Signal")

        # 3. Acquisition Tab
        self.acquisition_table = create_info_table()
        self.info_tab_widget.addTab(self.acquisition_table, "Acquisition")

        # 4. AI Result Tab
        self.ai_tab_widget = QWidget(self)
        ai_layout = QVBoxLayout(self.ai_tab_widget)
        ai_layout.setContentsMargins(6, 6, 6, 6)
        ai_layout.setSpacing(6)

        # AI Summary Card
        self.ai_card_widget = QFrame(self.ai_tab_widget)
        self.ai_card_widget.setObjectName("aiCard")
        self.ai_card_widget.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(self.ai_card_widget)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(2)

        self.ai_card_header = QLabel("AI RESULT", self.ai_card_widget)
        self.ai_card_header.setStyleSheet("font-weight: 700; color: #555; font-size: 11px;")
        self.ai_card_title = QLabel("WAITING", self.ai_card_widget)
        self.ai_card_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #333;")
        self.ai_card_conf = QLabel("Confidence: —", self.ai_card_widget)
        self.ai_card_latency = QLabel("Latency: —", self.ai_card_widget)

        card_layout.addWidget(self.ai_card_header)
        card_layout.addWidget(self.ai_card_title)
        card_layout.addWidget(self.ai_card_conf)
        card_layout.addWidget(self.ai_card_latency)
        ai_layout.addWidget(self.ai_card_widget)

        # Probabilities table
        self.ai_prob_table = create_info_table()
        ai_layout.addWidget(self.ai_prob_table)

        self.info_tab_widget.addTab(self.ai_tab_widget, "AI Result")

        # Splitter layout
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self.plot_widget)
        splitter.addWidget(self.info_tab_widget)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes((1050, 300))
        self._central_splitter = splitter

        # Create main container with control panel above splitter
        container = QWidget(self)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(self._control_panel)
        container_layout.addWidget(splitter)
        self.setCentralWidget(container)

    def _create_control_panel(self) -> None:
        """Create fixed 3-row control panel without toolbar overflow."""
        self._control_panel = QWidget(self)
        self._control_panel.setObjectName("controlPanel")
        self._control_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        main_layout = QVBoxLayout(self._control_panel)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # === ROW 1: NAVIGATION ===
        row1 = QHBoxLayout()
        row1.setSpacing(4)

        # Navigation buttons
        self.open_folder_button = QPushButton("Open Folder", self._control_panel)
        self.open_folder_button.clicked.connect(self._open_folder)
        row1.addWidget(self.open_folder_button)

        self.refresh_button = QPushButton("Refresh", self._control_panel)
        self.refresh_button.clicked.connect(self.refresh_frames)
        row1.addWidget(self.refresh_button)

        self.first_button = QPushButton("First", self._control_panel)
        self.first_button.clicked.connect(self._go_first)
        row1.addWidget(self.first_button)

        self.previous_button = QPushButton("Previous", self._control_panel)
        self.previous_button.clicked.connect(self._go_previous)
        row1.addWidget(self.previous_button)

        self.next_button = QPushButton("Next", self._control_panel)
        self.next_button.clicked.connect(self._go_next)
        row1.addWidget(self.next_button)

        self.latest_button = QPushButton("Latest", self._control_panel)
        self.latest_button.clicked.connect(self._go_latest)
        row1.addWidget(self.latest_button)

        row1.addWidget(QLabel("Frame:", self._control_panel))
        self.frame_combo = QComboBox(self._control_panel)
        self.frame_combo.setMinimumContentsLength(18)
        self.frame_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.frame_combo.activated.connect(self._frame_combo_activated)
        row1.addWidget(self.frame_combo)

        self.follow_live_checkbox = QCheckBox("Follow Live", self._control_panel)
        self.follow_live_checkbox.toggled.connect(self._follow_live_toggled)
        row1.addWidget(self.follow_live_checkbox)

        self.live_badge = QLabel(" LIVE ", self._control_panel)
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.setVisible(False)
        row1.addWidget(self.live_badge)

        # Replay controls
        separator1 = QFrame(self._control_panel)
        separator1.setFrameShape(QFrame.Shape.VLine)
        separator1.setFrameShadow(QFrame.Shadow.Sunken)
        row1.addWidget(separator1)

        self.replay_button = QPushButton("Replay", self._control_panel)
        self.replay_button.clicked.connect(self._start_replay)
        row1.addWidget(self.replay_button)

        self.pause_button = QPushButton("Pause", self._control_panel)
        self.pause_button.clicked.connect(self._pause_replay)
        row1.addWidget(self.pause_button)

        self.stop_button = QPushButton("Stop", self._control_panel)
        self.stop_button.clicked.connect(self._stop_replay)
        row1.addWidget(self.stop_button)

        row1.addWidget(QLabel("Replay Speed:", self._control_panel))
        self.replay_speed_combo = QComboBox(self._control_panel)
        for label, value in (("0.5×", 0.5), ("1×", 1.0), ("2×", 2.0)):
            self.replay_speed_combo.addItem(label, value)
        self.replay_speed_combo.currentIndexChanged.connect(self._replay_speed_changed)
        row1.addWidget(self.replay_speed_combo)

        row1.addStretch()
        main_layout.addLayout(row1)

        # === ROW 2: MODE AND SCALE ===
        row2 = QHBoxLayout()
        row2.setSpacing(4)

        # Data mode
        row2.addWidget(QLabel("Data Mode:", self._control_panel))
        self.data_mode_combo = QComboBox(self._control_panel)
        self.data_mode_combo.addItems(DATA_MODES)
        self.data_mode_combo.currentTextChanged.connect(self._data_mode_changed)
        row2.addWidget(self.data_mode_combo)

        self.mode_badge = QLabel("", self._control_panel)
        self.mode_badge.setObjectName("modeBadge")
        row2.addWidget(self.mode_badge)

        self.bpm_badge = QLabel(" BPM — ", self._control_panel)
        self.bpm_badge.setObjectName("bpmBadge")
        self.bpm_badge.setToolTip(
            "Estimated heart rate from R peaks detected on Lead II"
        )
        row2.addWidget(self.bpm_badge)

        separator2 = QFrame(self._control_panel)
        separator2.setFrameShape(QFrame.Shape.VLine)
        separator2.setFrameShadow(QFrame.Shadow.Sunken)
        row2.addWidget(separator2)

        # Raw view (visible in raw mode only)
        self.raw_view_label = QLabel("Raw View:", self._control_panel)
        row2.addWidget(self.raw_view_label)
        self.raw_view_combo = QComboBox(self._control_panel)
        self.raw_view_combo.addItems(RAW_VIEW_MODES)
        self.raw_view_combo.currentTextChanged.connect(self._raw_view_mode_changed)
        row2.addWidget(self.raw_view_combo)

        # Paper speed (visible in mV mode only)
        self.paper_speed_label = QLabel("Paper:", self._control_panel)
        row2.addWidget(self.paper_speed_label)
        self.paper_speed_combo = QComboBox(self._control_panel)
        self.paper_speed_combo.addItem("25 mm/s", 25.0)
        self.paper_speed_combo.addItem("50 mm/s", 50.0)
        self.paper_speed_combo.currentIndexChanged.connect(self._paper_speed_changed)
        row2.addWidget(self.paper_speed_combo)

        # Gain (visible in mV mode only)
        self.gain_label = QLabel("Gain:", self._control_panel)
        row2.addWidget(self.gain_label)
        self.gain_combo = QComboBox(self._control_panel)
        for text, value in (("5 mm/mV", 5.0), ("10 mm/mV", 10.0), ("20 mm/mV", 20.0)):
            self.gain_combo.addItem(text, value)
        self.gain_combo.currentIndexChanged.connect(self._gain_changed)
        row2.addWidget(self.gain_combo)

        # Window (always visible)
        self.time_window_label = QLabel("Window:", self._control_panel)
        row2.addWidget(self.time_window_label)
        self.time_window_combo = QComboBox(self._control_panel)
        for text, value in (("2.5 s", 2.5), ("5 s", 5.0), ("10 s", 10.0)):
            self.time_window_combo.addItem(text, value)
        self.time_window_combo.currentIndexChanged.connect(self._time_window_changed)
        row2.addWidget(self.time_window_combo)

        # Y Range (visible in mV mode only)
        self.y_range_label = QLabel("Y Range:", self._control_panel)
        row2.addWidget(self.y_range_label)
        self.y_range_combo = QComboBox(self._control_panel)
        for text, value in (("±0.5 mV", 0.5), ("±1 mV", 1.0), ("±2 mV", 2.0), ("±5 mV", 5.0)):
            self.y_range_combo.addItem(text, value)
        self.y_range_combo.currentIndexChanged.connect(self._y_range_changed)
        row2.addWidget(self.y_range_combo)

        # Raw Y Scale (visible in raw mode only)
        self.raw_y_scale_label = QLabel("Raw Y Scale:", self._control_panel)
        row2.addWidget(self.raw_y_scale_label)
        self.raw_y_scale_combo = QComboBox(self._control_panel)
        for value in RAW_Y_SCALES:
            self.raw_y_scale_combo.addItem(f"±{value:.0f} count", value)
        self.raw_y_scale_combo.addItem("Auto", -1.0)
        self.raw_y_scale_combo.currentIndexChanged.connect(self._raw_y_scale_changed)
        row2.addWidget(self.raw_y_scale_combo)

        # Raw Grid (visible in raw mode only)
        self.raw_grid_label = QLabel("Raw Grid Count:", self._control_panel)
        row2.addWidget(self.raw_grid_label)
        self.raw_grid_combo = QComboBox(self._control_panel)
        for value in RAW_GRID_SPACINGS:
            self.raw_grid_combo.addItem(f"{value:.0f} count", value)
        self.raw_grid_combo.currentIndexChanged.connect(self._raw_grid_changed)
        row2.addWidget(self.raw_grid_combo)

        # Leads (always visible)
        self.lead_mode_label = QLabel("Leads:", self._control_panel)
        row2.addWidget(self.lead_mode_label)
        self.lead_mode_combo = QComboBox(self._control_panel)
        self.lead_mode_combo.addItems(("All leads", "Lead I", "Lead II", "Lead III"))
        row2.addWidget(self.lead_mode_combo)

        row2.addStretch()
        main_layout.addLayout(row2)

        # === ROW 3: DISPLAY CONTROLS ===
        row3 = QHBoxLayout()
        row3.setSpacing(4)

        self.grid_checkbox = QCheckBox("Grid", self._control_panel)
        row3.addWidget(self.grid_checkbox)

        self.crosshair_checkbox = QCheckBox("Crosshair", self._control_panel)
        row3.addWidget(self.crosshair_checkbox)

        self.lock_view_checkbox = QCheckBox("Lock View", self._control_panel)
        row3.addWidget(self.lock_view_checkbox)

        self.sync_y_checkbox = QCheckBox("Sync Y", self._control_panel)
        self.sync_y_checkbox.setToolTip(
            "Synchronize manual Y-axis zoom and pan across all visible leads"
        )
        row3.addWidget(self.sync_y_checkbox)

        separator3 = QFrame(self._control_panel)
        separator3.setFrameShape(QFrame.Shape.VLine)
        separator3.setFrameShadow(QFrame.Shadow.Sunken)
        row3.addWidget(separator3)

        row3.addWidget(QLabel("Line:", self._control_panel))
        self.line_width_spin = QDoubleSpinBox(self._control_panel)
        self.line_width_spin.setRange(0.5, 4.0)
        self.line_width_spin.setSingleStep(0.25)
        self.line_width_spin.setSuffix(" px")
        row3.addWidget(self.line_width_spin)

        separator4 = QFrame(self._control_panel)
        separator4.setFrameShape(QFrame.Shape.VLine)
        separator4.setFrameShadow(QFrame.Shadow.Sunken)
        row3.addWidget(separator4)

        self.auto_y_button = QPushButton("Auto Y", self._control_panel)
        self.auto_y_button.clicked.connect(self._auto_scale_y)
        row3.addWidget(self.auto_y_button)

        self.reset_view_button = QPushButton("Reset View", self._control_panel)
        self.reset_view_button.clicked.connect(self._reset_view)
        row3.addWidget(self.reset_view_button)

        self.display_calibration_btn = QPushButton("Display Calibration", self._control_panel)
        self.display_calibration_btn.clicked.connect(self._show_display_calibration)
        row3.addWidget(self.display_calibration_btn)

        separator5 = QFrame(self._control_panel)
        separator5.setFrameShape(QFrame.Shape.VLine)
        separator5.setFrameShadow(QFrame.Shadow.Sunken)
        row3.addWidget(separator5)

        self.details_button = QPushButton("Details", self._control_panel)
        self.details_button.setCheckable(True)
        self.details_button.setChecked(True)
        self.details_button.setToolTip("Toggle details panel on the right")
        self.details_button.toggled.connect(self._toggle_details_panel)
        row3.addWidget(self.details_button)

        row3.addStretch()
        main_layout.addLayout(row3)

    def _toggle_details_panel(self, checked: bool) -> None:
        """Toggle visibility of the right details panel without destroying data."""
        self.info_tab_widget.setVisible(checked)
        self._settings.setValue("display/show_details", checked)
        self.plot_widget._update_physical_geometry()

    def _connect_control_signals(self) -> None:
        """Connect control signals to plot_widget after it's created."""
        self.lead_mode_combo.currentTextChanged.connect(self.plot_widget.set_lead_mode)
        self.grid_checkbox.toggled.connect(self.plot_widget.set_grid_visible)
        self.crosshair_checkbox.toggled.connect(self.plot_widget.set_crosshair_enabled)
        self.lock_view_checkbox.toggled.connect(self.plot_widget.set_view_locked)
        self.sync_y_checkbox.toggled.connect(self.plot_widget.set_y_ranges_synchronized)
        self.line_width_spin.valueChanged.connect(self.plot_widget.set_line_width)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.open_folder_action)
        file_menu.addAction(self.refresh_action)
        file_menu.addSeparator()
        file_menu.addAction(self.export_png_action)
        file_menu.addAction(self.copy_path_action)
        file_menu.addAction(self.open_containing_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        navigation_menu = self.menuBar().addMenu("&Navigate")
        navigation_menu.addActions(
            (self.first_action, self.previous_action, self.next_action, self.latest_action)
        )

        replay_menu = self.menuBar().addMenu("&Replay")
        replay_menu.addActions((self.replay_action, self.pause_action, self.stop_action))

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction(self.auto_y_action)
        view_menu.addAction(self.reset_view_action)
        view_menu.addAction(self.display_calibration_action)

    def _create_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)
        self.status_summary = QLabel("No frame selected", bar)
        self.status_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.validation_badge = QLabel(" NO DATA ", bar)
        self.validation_badge.setObjectName("validationBadge")
        self.ai_badge = QLabel(" AI — ", bar)
        self.ai_badge.setObjectName("aiBadge")
        self.ai_badge.setToolTip("AI prediction status for the current frame")
        self.live_status = QLabel("Follow Live: OFF", bar)
        bar.addWidget(self.status_summary, 1)
        bar.addPermanentWidget(self.ai_badge)
        bar.addPermanentWidget(self.validation_badge)
        bar.addPermanentWidget(self.live_status)

        # AI prediction polling timer
        self._ai_poll_timer = QTimer(self)
        self._ai_poll_timer.setInterval(500)
        self._ai_poll_timer.timeout.connect(self._poll_ai_prediction)
        self._ai_poll_timer.start()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #fffafa; }
            QMenuBar { background: #312d2f; color: #ffffff; }
            QMenuBar::item { color: #ffffff; background: transparent;
                             padding: 3px 8px; }
            QMenuBar::item:selected { background: #67575b; }
            QMenu { background: #ffffff; color: #252124;
                    border: 1px solid #b99da2; }
            QWidget#controlPanel {
                background: #fff7f8;
                border-bottom: 2px solid #d8b8bd;
            }
            QWidget#controlPanel QPushButton {
                color: #252124;
                background: #ffffff;
                border: 1px solid #b99da2;
                border-radius: 3px;
                padding: 4px 8px;
                min-height: 20px;
            }
            QWidget#controlPanel QPushButton:hover {
                background: #f0e4e6;
            }
            QWidget#controlPanel QPushButton:pressed {
                background: #e0d0d2;
            }
            QWidget#controlPanel QComboBox,
            QWidget#controlPanel QDoubleSpinBox,
            QWidget#controlPanel QCheckBox {
                color: #252124; background: #ffffff; min-height: 20px;
            }
            QWidget#controlPanel QLabel {
                color: #252124;
            }
            QTableWidget { background: #fffdfd; alternate-background-color: #fff4f5;
                           color: #282326; gridline-color: #ead4d8; }
            QHeaderView::section { background: #f6dde1; color: #2c2427;
                                   padding: 4px; font-weight: 600; }
            QLabel#liveBadge { color: white; background: #c62828; border-radius: 3px;
                               font-weight: 700; padding: 2px 5px; }
            QLabel#validationBadge { color: white; background: #616161;
                                     border-radius: 3px; font-weight: 700;
                                     padding: 2px 6px; }
            QLabel#aiBadge { color: white; background: #616161;
                             border-radius: 3px; font-weight: 700;
                             padding: 2px 6px; }
            QLabel#bpmBadge { color: white; background: #616161;
                              border-radius: 3px; font-weight: 700;
                              padding: 2px 6px; }
            QStatusBar { color: #252124; background: #fff7f8;
                         border-top: 1px solid #d8b8bd; }
            QStatusBar QLabel { color: #252124; }
            QTabWidget::pane { border: 1px solid #d8b8bd; background: #fffdfd; }
            QTabBar::tab { background: #f6dde1; color: #2c2427; padding: 4px 8px; font-weight: 600; border: 1px solid #d8b8bd; }
            QTabBar::tab:selected { background: #ffffff; border-bottom-color: #ffffff; }
            QFrame#aiCard { background: #f9f2f3; border: 1px solid #d8b8bd; border-radius: 4px; }
            """
        )

    def _restore_window_state(self) -> None:
        if not restore_window_geometry(self, self._settings):
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                geom = screen.availableGeometry()
                w = min(1366, geom.width() - 20)
                h = min(768, geom.height() - 40)
                self.resize(max(1024, w), max(680, h))
            else:
                self.resize(1366, 768)

        show_details = bool(self._settings.value("display/show_details", True, type=bool))
        self.details_button.setChecked(show_details)
        self.info_tab_widget.setVisible(show_details)

        splitter_state = self._settings.value("window/splitter_state")
        if splitter_state:
            self._central_splitter.restoreState(splitter_state)
        else:
            self._central_splitter.setSizes((1050, 300))

    def _restore_control_settings(self) -> None:
        saved = load_viewer_settings(self._settings)

        # Restore data mode first
        self._select_combo_text(self.data_mode_combo, saved.data_mode)
        self._select_combo_text(self.raw_view_combo, saved.raw_view_mode)
        self._select_combo_data(self.raw_y_scale_combo, saved.raw_y_scale)
        self._select_combo_data(self.raw_grid_combo, saved.raw_grid_spacing)

        self._select_combo_data(
            self.paper_speed_combo,
            saved.paper_speed_mm_s,
        )
        self._select_combo_data(
            self.gain_combo,
            saved.gain_mm_per_mv,
        )
        self._select_combo_data(
            self.time_window_combo,
            saved.time_window_s,
        )
        self._select_combo_data(
            self.y_range_combo,
            saved.y_range_mv,
        )
        self._select_combo_text(
            self.lead_mode_combo,
            saved.lead_mode,
        )
        self.grid_checkbox.setChecked(saved.grid_enabled)
        self.crosshair_checkbox.setChecked(saved.crosshair_enabled)
        self.lock_view_checkbox.setChecked(saved.lock_view)
        self.sync_y_checkbox.setChecked(saved.synchronize_y_ranges)
        self.line_width_spin.setValue(saved.line_width)
        self._select_combo_data(self.replay_speed_combo, saved.replay_speed)
        self.plot_widget.set_display_scale_factor(saved.display_scale_factor)

        # Update control visibility based on mode
        self._update_mode_controls_visibility()

        # Explicit calls make defaults effective even if setting a combo did
        # not emit a signal because it already held the requested index.
        self._apply_all_plot_controls()

    def _apply_cli_overrides(
        self,
        *,
        paper_speed_mm_s: float | None,
        gain_mm_mv: float | None,
        y_range_mv: float | None,
    ) -> None:
        if paper_speed_mm_s is not None:
            self._select_combo_data(self.paper_speed_combo, paper_speed_mm_s)
        if gain_mm_mv is not None:
            self._select_combo_data(self.gain_combo, gain_mm_mv)
        if y_range_mv is not None:
            self._select_combo_data(self.y_range_combo, y_range_mv)
        self._apply_all_plot_controls()

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: float) -> None:
        for index in range(combo.count()):
            if float(combo.itemData(index)) == float(value):
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _select_combo_text(combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _update_mode_controls_visibility(self) -> None:
        """Show/hide controls based on current data mode."""
        is_raw = self.data_mode_combo.currentText() == "Raw ADC"

        # Raw-specific controls
        self.raw_view_label.setVisible(is_raw)
        self.raw_view_combo.setVisible(is_raw)
        self.raw_y_scale_label.setVisible(is_raw)
        self.raw_y_scale_combo.setVisible(is_raw)
        self.raw_grid_label.setVisible(is_raw)
        self.raw_grid_combo.setVisible(is_raw)

        # mV-specific controls
        self.paper_speed_label.setVisible(not is_raw)
        self.paper_speed_combo.setVisible(not is_raw)
        self.gain_label.setVisible(not is_raw)
        self.gain_combo.setVisible(not is_raw)
        self.y_range_label.setVisible(not is_raw)
        self.y_range_combo.setVisible(not is_raw)
        self.display_calibration_btn.setVisible(not is_raw)
        self.display_calibration_action.setEnabled(not is_raw)

        metadata = (
            self._current_validation.metadata
            if self._current_validation is not None
            else None
        )
        self._update_mode_badge(metadata)

        # Update plot widget grid style
        if is_raw:
            self.plot_widget.set_grid_style("engineering")
        else:
            self.plot_widget.set_grid_style("medical")

    def _update_mode_badge(self, metadata: dict[str, Any] | None = None) -> None:
        """Reflect raw units or calibrated/preliminary metadata status."""

        is_raw = self.data_mode_combo.currentText() == "Raw ADC"
        if is_raw:
            text, color = " RAW ADC — NOT mV ", "#d84315"
        else:
            calibration = (metadata or {}).get("calibration", {})
            if not isinstance(calibration, dict):
                calibration = {}
            status = str(calibration.get("status", "")).strip().lower()
            method = str(calibration.get("method", "")).strip().lower()
            explicitly_preliminary = any(
                marker in f"{status} {method}"
                for marker in ("prelim", "legacy", "unverified", "estimated")
            )
            has_calibration = bool(
                calibration.get("scale_mV_per_count") is not None
                or calibration.get("method")
                or status in {"calibrated", "valid", "validated", "pass", "success"}
            )
            if has_calibration and not explicitly_preliminary:
                text, color = " CALIBRATED mV ", "#1976d2"
            else:
                text, color = " PRELIMINARY mV ", "#ef6c00"
        self.mode_badge.setText(text)
        self.mode_badge.setStyleSheet(
            f"color: white; background: {color}; border-radius: 3px; "
            "font-weight: 700; padding: 2px 6px;"
        )

    def _data_mode_changed(self) -> None:
        """Handle data mode change between Raw ADC and Calibrated mV."""
        current_mode = self.data_mode_combo.currentText()
        is_raw = current_mode == "Raw ADC"
        source_mode = "raw" if is_raw else "mv"

        # Save setting
        self._settings.setValue("display/data_mode", current_mode)

        # Update control visibility
        self._update_mode_controls_visibility()

        if self._initializing:
            return

        # Update repository and validator mode
        try:
            self._repository.set_source_mode(source_mode)
        except OSError as error:
            self._show_nonfatal_error("Unable to change data mode", error)
            return
        self._validator.source_mode = source_mode

        # Apply plot controls for new mode
        self._apply_all_plot_controls()

        # Refresh frame list and try to keep current frame
        current_id = self._current_record.frame_id if self._current_record else None
        self.refresh_frames()

        # Try to select same frame ID in new mode with Auto Y
        if current_id is not None:
            record = self._repository.get_by_id(current_id)
            if record is not None and self._record_is_stable(record):
                self._select_record(record, user_requested=False, show_error=False)

    def _raw_view_mode_changed(self) -> None:
        """Handle raw view mode change (Absolute/Zero-Centered/Median-Centered)."""
        mode = self.raw_view_combo.currentText()
        self._settings.setValue("display/raw_view_mode", mode)

        if self._initializing:
            return

        # Re-display current frame with new view mode and Auto Y
        if self._current_record is not None:
            self._select_record(self._current_record, user_requested=False, show_error=False)

    def _raw_y_scale_changed(self) -> None:
        """Handle raw Y scale change."""
        scale = float(self.raw_y_scale_combo.currentData())
        if scale > 0:
            self._settings.setValue("display/raw_y_scale", scale)
            self.plot_widget.set_raw_y_scale(scale)
            self.plot_widget.reset_view()
        else:
            # Auto scale
            self.plot_widget.auto_scale_y()

    def _raw_grid_changed(self) -> None:
        """Handle raw grid spacing change."""
        spacing = float(self.raw_grid_combo.currentData())
        self._settings.setValue("display/raw_grid_spacing", spacing)
        self.plot_widget.set_raw_grid_spacing(spacing)

    def _apply_all_plot_controls(self) -> None:
        is_raw = self.data_mode_combo.currentText() == "Raw ADC"

        # Set display unit
        self.plot_widget.set_display_unit("count" if is_raw else "mv")

        self.plot_widget.set_paper_speed(float(self.paper_speed_combo.currentData()))
        self.plot_widget.set_time_window(float(self.time_window_combo.currentData()))

        if is_raw:
            # Apply raw-specific controls
            raw_y_scale = float(self.raw_y_scale_combo.currentData())
            if raw_y_scale > 0:
                self.plot_widget.set_raw_y_scale(raw_y_scale)
            raw_grid = float(self.raw_grid_combo.currentData())
            self.plot_widget.set_raw_grid_spacing(raw_grid)
        else:
            # Apply mV-specific controls
            self.plot_widget.set_gain(float(self.gain_combo.currentData()))
            self.plot_widget.set_y_range(float(self.y_range_combo.currentData()))

        self.plot_widget.set_lead_mode(self.lead_mode_combo.currentText())
        self.plot_widget.set_grid_visible(self.grid_checkbox.isChecked())
        self.plot_widget.set_crosshair_enabled(self.crosshair_checkbox.isChecked())
        self.plot_widget.set_view_locked(self.lock_view_checkbox.isChecked())
        self.plot_widget.set_y_ranges_synchronized(
            self.sync_y_checkbox.isChecked()
        )
        self.plot_widget.set_line_width(float(self.line_width_spin.value()))
        self.plot_widget.set_replay_speed(float(self.replay_speed_combo.currentData()))

    def _set_data_directory(
        self,
        directory: str | Path,
        *,
        initial_frame: str | int | None = None,
    ) -> None:
        try:
            resolved = Path(directory).expanduser().resolve()
        except OSError as error:
            if self.isVisible():
                self._show_nonfatal_error("Unable to open frame directory", error)
            LOGGER.warning("Unable to resolve frame directory %s: %s", directory, error)
            return
        self._stop_replay()
        self._current_record = None
        self._current_validation = None
        self._current_source_signature = None
        self._pending_live_record = None
        self.plot_widget.clear_frame()
        try:
            self._repository.set_data_dir(resolved)
        except OSError as error:
            LOGGER.warning("Unable to scan frame directory %s: %s", resolved, error)
            self._pending_initial_frame_id = None
            self._seeking_first_valid_frame = not self.follow_live_checkbox.isChecked()
            self._populate_frame_combo()
            self.status_summary.setText(f"Unable to read frame directory {resolved}: {error}")
            self.validation_badge.setText(" NO DATA ")
            self._set_validation_badge_color("NO DATA")
            self._set_information_rows(
                (("Source directory", str(resolved)), ("Error", str(error)))
            )
            if hasattr(self, "_watcher"):
                self._watcher.set_directory(resolved)
            self._update_navigation_actions()
            if self.isVisible():
                self._show_nonfatal_error("Unable to open frame directory", error)
            return
        self._settings.setValue("paths/last_directory", str(resolved))

        requested_id = self._parse_frame_id(initial_frame)
        self._pending_initial_frame_id = (
            requested_id
            if requested_id is not None
            and self._repository.get_by_id(requested_id) is not None
            else None
        )
        self._seeking_first_valid_frame = (
            not self.follow_live_checkbox.isChecked()
            and self._pending_initial_frame_id is None
        )
        self._populate_frame_combo()
        if self._repository.frame_count():
            self._show_waiting_for_stable(resolved)
        else:
            self._show_no_frames(resolved)

        if hasattr(self, "_watcher"):
            self._watcher.set_directory(resolved)
        self._update_navigation_actions()

    @staticmethod
    def _parse_frame_id(value: str | int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        match = FRAME_ID_SEARCH.search(str(value))
        return int(match.group(1)) if match else None

    def refresh_frames(self) -> None:
        current_id = self._current_record.frame_id if self._current_record else None
        try:
            self._repository.refresh()
        except OSError as error:
            self._show_nonfatal_error("Unable to refresh frame directory", error)
            return
        self._populate_frame_combo()
        if not self._repository.frame_count():
            self._show_no_frames(self._repository.data_dir)
        elif current_id is None or self._repository.get_by_id(current_id) is None:
            self._current_record = None
            self._current_validation = None
            self._current_source_signature = None
            self.plot_widget.clear_frame()
            fallback = self._ready_fallback_record(current_id)
            if fallback is not None:
                self._seeking_first_valid_frame = False
                self._select_record(
                    fallback,
                    user_requested=False,
                    show_error=False,
                    live_replay=self.follow_live_checkbox.isChecked(),
                )
            else:
                self._seeking_first_valid_frame = (
                    not self.follow_live_checkbox.isChecked()
                )
                self._show_waiting_for_stable(self._repository.data_dir)
        if hasattr(self, "_watcher"):
            self._watcher.scan_now()
        if self.plot_widget.source_data is not None:
            QTimer.singleShot(0, self._auto_scale_y)
        self._update_navigation_actions()

    def _populate_frame_combo(self) -> None:
        current_id = self._current_record.frame_id if self._current_record else None
        self._updating_frame_combo = True
        blocker = QSignalBlocker(self.frame_combo)
        self.frame_combo.clear()
        for record in self._repository.frames():
            suffix = "" if record.has_metadata else "  ⚠ metadata"
            self.frame_combo.addItem(f"{record.display_id}{suffix}", record.frame_id)
        del blocker
        self._updating_frame_combo = False
        if current_id is not None:
            self._set_frame_combo_id(current_id)

    def _set_frame_combo_id(self, frame_id: int) -> None:
        for index in range(self.frame_combo.count()):
            if int(self.frame_combo.itemData(index)) == int(frame_id):
                blocker = QSignalBlocker(self.frame_combo)
                self.frame_combo.setCurrentIndex(index)
                del blocker
                return

    def _frame_combo_activated(self, index: int) -> None:
        if self._updating_frame_combo or index < 0:
            return
        frame_id = self.frame_combo.itemData(index)
        record = self._repository.get_by_id(int(frame_id))
        if record is None:
            return
        if not self._record_is_stable(record):
            if self._current_record is not None:
                self._set_frame_combo_id(self._current_record.frame_id)
            self.statusBar().showMessage(
                f"Waiting for {record.filename} to finish writing", 4000
            )
            return
        latest = self._newest_record()
        if (
            self.follow_live_checkbox.isChecked()
            and latest is not None
            and record.frame_id != latest.frame_id
        ):
            self.follow_live_checkbox.setChecked(False)
        self._select_record(record, user_requested=True, show_error=True)

    def _select_record(
        self,
        record: FrameRecord,
        *,
        user_requested: bool,
        show_error: bool,
        live_replay: bool = False,
    ) -> None:
        self._stop_replay()
        signature_before = self._record_signature(record)
        if signature_before is None or (
            hasattr(self, "_watcher") and not self._record_is_stable(record)
        ):
            self.statusBar().showMessage(
                f"Waiting for {record.filename} to finish writing", 4000
            )
            if hasattr(self, "_watcher"):
                QTimer.singleShot(0, self._watcher.scan_now)
            return
        try:
            result = self._validator.validate(record)
        except (OSError, ValueError, TypeError) as error:
            LOGGER.exception("Unable to validate %s", record.npy_path)
            self.plot_widget.clear_frame()
            self._current_record = record
            self._current_validation = None
            self._current_source_signature = self._record_signature(record)
            self._set_frame_combo_id(record.frame_id)
            self._show_failed_load(record, str(error))
            if show_error or user_requested:
                self._show_nonfatal_error("Unable to load ECG frame", error)
            return

        signature_after = self._record_signature(record)
        if signature_before is None or signature_after != signature_before:
            LOGGER.info("Frame changed while loading; waiting for stability: %s", record.npy_path)
            self.statusBar().showMessage(
                f"{record.filename} changed while loading; waiting for it to stabilize",
                5000,
            )
            if hasattr(self, "_watcher"):
                QTimer.singleShot(0, self._watcher.scan_now)
            return

        self._current_record = record
        self._current_validation = result
        self._current_source_signature = signature_after
        self._set_frame_combo_id(record.frame_id)
        self._update_bpm_display(result)

        # Reset AI prediction state for the new frame
        self._ai_prediction = None
        self._ai_prediction_frame_id = record.frame_id
        self._update_ai_badge()

        if result.can_plot:
            # Extract metadata for raw mode
            is_raw = self.data_mode_combo.currentText() == "Raw ADC"
            metadata = result.metadata or {}
            self._update_mode_badge(metadata)

            if is_raw:
                # Retain the validated source by reference. Display
                # transforms allocate only when they change values.
                self._raw_source_data = result.samples
                self._zero_code = metadata.get("zero_code")
                self._adcmax = metadata.get("ADCMAX")

                # Apply selected transformation
                transformed_data = self._apply_raw_transformation(result.samples, metadata)
                self.plot_widget.set_frame(transformed_data, sample_rate=250)
            else:
                # For mV mode, use data as-is
                self._raw_source_data = None
                self._zero_code = None
                self._adcmax = None
                self.plot_widget.set_frame(result.samples, sample_rate=250)

            # Auto Y on every frame load
            QTimer.singleShot(0, self._auto_scale_y)
            if live_replay:
                # A stable live frame is revealed progressively instead of
                # flashing the complete 10-second trace onto the plots.
                self.plot_widget.start_replay(LIVE_REPLAY_SPEED)

        else:
            self.plot_widget.clear_frame()
            if show_error or user_requested:
                details = "; ".join(result.errors or result.reasons)
                self._show_nonfatal_error(
                    "Frame failed validation",
                    ValueError(
                        f"{record.npy_path}: "
                        f"{details or 'unknown validation failure'}"
                    ),
                )

        self._update_information(record, result)
        self._update_status(record, result)
        self._update_navigation_actions()

    def _apply_raw_transformation(self, data: np.ndarray, metadata: dict[str, Any]) -> np.ndarray:
        """Apply raw view transformation (Absolute/Zero-Centered/Median-Centered)."""
        view_mode = self.raw_view_combo.currentText()

        if view_mode == "Absolute Code":
            # Return data as-is
            return data

        elif view_mode == "Zero-Centered":
            # Apply zero_code correction to Lead I and Lead II only
            # Lead III remains unchanged (it's already Lead II - Lead I)
            zero_code = metadata.get("zero_code")

            if zero_code is None:
                # If zero_code not available, fall back to absolute
                LOGGER.warning("zero_code not available in metadata; using Absolute Code")
                return data

            try:
                zero_val = float(zero_code)
                result = data.copy()
                result[:, 0] = data[:, 0] - zero_val  # Lead I
                result[:, 1] = data[:, 1] - zero_val  # Lead II
                # Lead III unchanged
                return result
            except (TypeError, ValueError) as e:
                LOGGER.warning("Invalid zero_code value %r: %s; using Absolute Code", zero_code, e)
                return data

        elif view_mode == "Median-Centered":
            # Subtract median from each lead independently for display
            result = data.copy()
            for channel in range(3):
                median_val = float(np.median(data[:, channel]))
                result[:, channel] = data[:, channel] - median_val
            return result

        else:
            # Unknown mode, return as-is
            return data

    def _show_failed_load(self, record: FrameRecord, reason: str) -> None:
        self._clear_bpm_display()
        self.validation_badge.setText(" FAIL ")
        self._set_validation_badge_color("FAIL")
        self.status_summary.setText(f"Frame {record.frame_id} | {record.npy_path} | {reason}")
        self._set_information_rows(
            (("Frame ID", record.display_id), ("Filename", record.filename), ("Error", reason))
        )

    def _show_no_frames(self, directory: Path) -> None:
        if (
            self._pending_initial_frame_id is not None
            and self._repository.get_by_id(self._pending_initial_frame_id) is None
        ):
            self._pending_initial_frame_id = None
        self._pending_live_record = None
        self._seeking_first_valid_frame = (
            not self.follow_live_checkbox.isChecked()
            and self._pending_initial_frame_id is None
        )
        self._current_record = None
        self._current_validation = None
        self._current_source_signature = None
        self.plot_widget.clear_frame()
        self._clear_bpm_display()

        # Show appropriate message based on data mode
        is_raw = self.data_mode_combo.currentText() == "Raw ADC"
        if is_raw:
            pattern = "frame_*.npy"
        else:
            pattern = "frame_*_mv.npy"

        self.status_summary.setText(f"No {pattern} files in {directory}")
        self.validation_badge.setText(" NO DATA ")
        self._set_validation_badge_color("NO DATA")
        self._set_information_rows(
            (("Source directory", str(directory)), ("Status", f"No {pattern} frames found"))
        )

    def _show_waiting_for_stable(self, directory: Path) -> None:
        self._clear_bpm_display()
        self.status_summary.setText(
            f"Waiting for a stable completed ECG frame in {directory}"
        )
        self.validation_badge.setText(" WAITING ")
        self._set_validation_badge_color("NO DATA")
        self._set_information_rows(
            (
                ("Source directory", str(directory)),
                ("Status", "Waiting for an unchanged NPY across two scans"),
            )
        )

    def _update_information(
        self, record: FrameRecord, result: ValidationResult
    ) -> None:
        metadata = result.metadata or {}
        calibration = metadata.get("calibration", {})
        if not isinstance(calibration, dict):
            calibration = {}
        created = metadata.get("created_at_utc", metadata.get("created_at", "Unavailable"))
        sample_rate = metadata.get("sample_rate_hz", 250)
        duration = metadata.get("duration_seconds", 10.0)
        channel_order = metadata.get("channel_order", LEAD_LABELS)
        if not isinstance(channel_order, (list, tuple)):
            channel_order = LEAD_LABELS

        is_raw = self.data_mode_combo.currentText() == "Raw ADC"

        # -------------------------------------------------------------
        # Tab 1: Overview
        # -------------------------------------------------------------
        quality = metadata.get("signal_quality", {})
        if isinstance(quality, dict):
            quality_text = quality.get("status", self._validation_status_text(result))
        else:
            quality_text = self._validation_status_text(result)

        overview_rows: list[tuple[str, Any]] = [
            ("Frame ID", record.display_id),
            ("Filename", record.filename),
            ("Data Mode", "Raw ADC" if is_raw else "Calibrated mV"),
            ("Sample rate", f"{sample_rate} Hz"),
            ("Duration", f"{duration} s"),
            (
                "Samples",
                result.source_shape[0]
                if result.source_shape is not None and result.source_shape
                else "N/A",
            ),
            ("Channels", ", ".join(str(value) for value in channel_order)),
            ("Shape", result.source_shape or "N/A"),
            ("Dtype", result.source_dtype or "N/A"),
            ("Unit", metadata.get("unit", "count" if is_raw else "mV")),
            ("Validation", self._validation_status_text(result)),
            ("Signal quality", quality_text),
            ("Estimated BPM", self._bpm_information_text()),
            ("R peaks detected", self._bpm_peak_count),
            ("BPM source", "Lead II"),
        ]
        self._set_table_rows(self.overview_table, overview_rows)

        # -------------------------------------------------------------
        # Tab 2: Signal
        # -------------------------------------------------------------
        signal_rows: list[tuple[str, Any]] = [
            ("Absolute max (ref only)", self._format_optional_value(result.max_abs_mv, is_raw)),
            ("Centered max amplitude", self._format_optional_value(
                getattr(result, "centered_max_abs_mv", None), is_raw)),
            ("Lead III max error", self._format_optional_value(result.lead_iii_max_error_mv, is_raw)),
        ]

        # Per-lead baseline median, centered max, peak-to-peak
        if not is_raw:
            baseline_medians = getattr(result, "baseline_median_mv", None)
            peak_to_peaks = getattr(result, "peak_to_peak_mv", None)
            if baseline_medians and len(baseline_medians) == 3:
                for lead_index, lead_label in enumerate(LEAD_LABELS):
                    signal_rows.append(
                        (f"{lead_label} baseline median",
                         f"{baseline_medians[lead_index]:+.4f} mV")
                    )
            if peak_to_peaks and len(peak_to_peaks) == 3:
                for lead_index, lead_label in enumerate(LEAD_LABELS):
                    signal_rows.append(
                        (f"{lead_label} peak-to-peak",
                         f"{peak_to_peaks[lead_index]:.4f} mV")
                    )

        stats = result.per_lead_stats or {}
        unit_str = "count" if is_raw else "mV"
        for lead_index, lead_label in enumerate(LEAD_LABELS):
            stat = self._lookup_lead_stats(stats, lead_index, lead_label)
            if stat:
                signal_rows.extend([
                    (f"{lead_label} minimum", f"{stat.get('min', stat.get('minimum', 0.0)):.9g} {unit_str}"),
                    (f"{lead_label} maximum", f"{stat.get('max', stat.get('maximum', 0.0)):.9g} {unit_str}"),
                    (f"{lead_label} mean", f"{stat.get('mean', 0.0):.9g} {unit_str}"),
                    (f"{lead_label} std", f"{stat.get('std', stat.get('standard_deviation', 0.0)):.9g} {unit_str}"),
                ])
            else:
                signal_rows.append((f"{lead_label} stats", "N/A"))

        reasons = list(result.errors) + list(result.warnings)
        if reasons:
            signal_rows.append(("Validation notes", "\n".join(reasons)))
        self._set_table_rows(self.signal_table, signal_rows)

        # -------------------------------------------------------------
        # Tab 3: Acquisition
        # -------------------------------------------------------------
        acq_rows: list[tuple[str, Any]] = [
            ("Protocol version", metadata.get("protocol_version", "N/A")),
            ("Native rate", f"{metadata.get('native_sample_rate_hz', 'N/A')} Hz" if metadata.get("native_sample_rate_hz") else "N/A"),
            ("Output rate", f"{sample_rate} Hz"),
            ("Packet loss", metadata.get("packet_loss", metadata.get("parser_stats", {}).get("packet_loss_count", "N/A"))),
            ("CRC error", metadata.get("parser_stats", {}).get("crc_errors", "N/A")),
            ("Resampler reset", metadata.get("resampler_reset_count", metadata.get("parser_stats", {}).get("resampler_reset_count", "N/A"))),
            ("Zero code", metadata.get("zero_code", "N/A")),
            ("ADCMAX", metadata.get("ADCMAX", "N/A")),
            ("Calibration method", calibration.get("method", "N/A")),
            ("Calibration scale", calibration.get("scale_mV_per_count", "N/A")),
            ("Calibration status", calibration.get("status", "N/A")),
        ]
        self._set_table_rows(self.acquisition_table, acq_rows)

        # -------------------------------------------------------------
        # Tab 4: AI Result
        # -------------------------------------------------------------
        self._update_ai_result_tab()

    def _set_table_rows(self, table: QTableWidget, rows: Iterable[tuple[str, Any]]) -> None:
        materialized = list(rows)
        table.setRowCount(len(materialized))
        for row_index, (key, value) in enumerate(materialized):
            table.setItem(row_index, 0, QTableWidgetItem(str(key)))
            table.setItem(row_index, 1, QTableWidgetItem(str(value)))
        table.resizeRowsToContents()

    def _set_information_rows(self, rows: Iterable[tuple[str, Any]]) -> None:
        """Alias kept for tests and fallback state."""
        self._set_table_rows(self.overview_table, rows)

    def _format_optional_value(self, value: float | None, is_raw: bool) -> str:
        """Format optional value with appropriate unit."""
        if value is None:
            return "Unavailable"
        unit = "count" if is_raw else "mV"
        return f"{value:.9g} {unit}"

    @staticmethod
    def _lookup_lead_stats(
        stats: Any, index: int, label: str
    ) -> dict[str, float] | None:
        if isinstance(stats, (list, tuple)) and index < len(stats):
            value = stats[index]
            if isinstance(value, dict):
                return value
            if all(
                hasattr(value, attribute)
                for attribute in ("minimum_mv", "maximum_mv", "mean_mv", "std_mv")
            ):
                return {
                    "min": float(value.minimum_mv),
                    "max": float(value.maximum_mv),
                    "mean": float(value.mean_mv),
                    "std": float(value.std_mv),
                }
        if isinstance(stats, dict):
            for key in (label, label.lower().replace(" ", "_"), index):
                value = stats.get(key)
                if isinstance(value, dict):
                    return value
        return None

    @staticmethod
    def _format_optional_mv(value: float | None) -> str:
        return "Unavailable" if value is None else f"{value:.9g} mV"

    def _set_information_rows(self, rows: Iterable[tuple[str, Any]]) -> None:
        materialized = list(rows)
        self.info_table.setRowCount(len(materialized))
        for row_index, (key, value) in enumerate(materialized):
            self.info_table.setItem(row_index, 0, QTableWidgetItem(str(key)))
            self.info_table.setItem(row_index, 1, QTableWidgetItem(str(value)))
        self.info_table.resizeRowsToContents()

    def _update_status(self, record: FrameRecord, result: ValidationResult) -> None:
        metadata = result.metadata or {}
        sample_rate = metadata.get("sample_rate_hz", 250)
        duration = metadata.get("duration_seconds", 10.0)
        is_raw = self.data_mode_combo.currentText() == "Raw ADC"
        unit = metadata.get("unit", "count" if is_raw else "mV")
        bpm_text = self._bpm_status_text()
        status = self._validation_status_text(result)

        ai_text = "WAITING"
        if self._ai_prediction is not None:
            pred = self._ai_prediction.get("prediction", "")
            conf = self._ai_prediction.get("confidence_percent")
            if pred and conf is not None:
                ai_text = f"AI {pred} {conf:.1f}%"
            elif pred:
                ai_text = f"AI {pred}"

        # Concise status summary
        self.status_summary.setText(
            f"Frame {record.display_id} | {sample_rate} Hz | {duration} s | {unit} | "
            f"BPM {bpm_text} | {status} | {ai_text}"
        )
        self.status_summary.setToolTip(f"File: {record.npy_path}")

        self.validation_badge.setText(f" {status} ")
        self._set_validation_badge_color(status)

    def _update_bpm_display(self, result: ValidationResult) -> None:
        """Estimate heart rate from Lead II and refresh the prominent badge."""
        self._estimated_bpm = None
        self._bpm_peak_count = 0
        self._bpm_reliable = False

        if result.samples is None:
            self._clear_bpm_display()
            return

        metadata = result.metadata or {}
        try:
            sample_rate = int(round(float(metadata.get("sample_rate_hz", 250))))
            if sample_rate <= 0:
                raise ValueError("sample_rate_hz must be positive")
            bpm, peak_count = estimate_bpm(
                result.samples,
                sample_rate,
                lead="lead_ii",
            )
        except (IndexError, TypeError, ValueError):
            LOGGER.exception("Unable to estimate BPM for the selected frame")
            self._clear_bpm_display()
            return

        self._bpm_peak_count = int(peak_count)
        if self._bpm_peak_count < 2 or not np.isfinite(bpm):
            self._clear_bpm_display(keep_peak_count=True)
            return

        self._estimated_bpm = float(bpm)
        status = self._validation_status_text(result)
        self._bpm_reliable = status != "FAIL" and 20.0 <= bpm <= 300.0
        prefix = "" if self._bpm_reliable else "~"
        color = "#2e7d32" if self._bpm_reliable else "#c62828"
        self.bpm_badge.setText(f" BPM {prefix}{bpm:.1f} ")
        self.bpm_badge.setStyleSheet(
            f"color: white; background: {color}; border-radius: 3px; "
            "font-weight: 700; padding: 2px 6px;"
        )
        reliability = (
            "Estimate based on Lead II R peaks"
            if self._bpm_reliable
            else "Approximate only: this frame failed validation or BPM is outside the expected range"
        )
        self.bpm_badge.setToolTip(
            f"{reliability}. Detected R peaks: {self._bpm_peak_count}."
        )

    def _clear_bpm_display(self, *, keep_peak_count: bool = False) -> None:
        self._estimated_bpm = None
        if not keep_peak_count:
            self._bpm_peak_count = 0
        self._bpm_reliable = False
        self.bpm_badge.setText(" BPM — ")
        self.bpm_badge.setStyleSheet(
            "color: white; background: #616161; border-radius: 3px; "
            "font-weight: 700; padding: 2px 6px;"
        )
        self.bpm_badge.setToolTip(
            f"BPM unavailable. Detected R peaks: {self._bpm_peak_count}."
        )

    def _bpm_information_text(self) -> str:
        if self._estimated_bpm is None:
            return "Unavailable"
        suffix = "" if self._bpm_reliable else " (unreliable)"
        return f"{self._estimated_bpm:.1f} BPM{suffix}"

    def _bpm_status_text(self) -> str:
        if self._estimated_bpm is None:
            return "Unavailable"
        prefix = "" if self._bpm_reliable else "~"
        return f"{prefix}{self._estimated_bpm:.1f}"

    @staticmethod
    def _validation_status_text(result: ValidationResult) -> str:
        status = result.status
        return str(getattr(status, "value", status)).upper()

    def _set_validation_badge_color(self, status: str) -> None:
        colors = {
            "PASS": "#2e7d32",
            "WARNING": "#ef6c00",
            "FAIL": "#c62828",
            "NO DATA": "#616161",
        }
        self.validation_badge.setStyleSheet(
            f"color: white; background: {colors.get(status, '#616161')}; "
            "border-radius: 3px; font-weight: 700; padding: 2px 6px;"
        )

    # ------------------------------------------------------------------
    # AI prediction integration (read-only, no ECGEngine import)
    # ------------------------------------------------------------------

    def _resolve_predictions_dir(self) -> Path | None:
        """Locate the predictions directory relative to the data directory."""

        data_dir = self._repository.data_dir
        # Pattern A: predictions/ sibling of data_dir
        candidate = data_dir.parent / "predictions"
        if candidate.is_dir():
            return candidate
        # Pattern B: predictions/ inside data_dir parent's parent
        candidate = data_dir.parent.parent / "predictions"
        if candidate.is_dir():
            return candidate
        # Pattern A fallback: even if not yet created
        return data_dir.parent / "predictions"

    def _poll_ai_prediction(self) -> None:
        """Timer callback: check for prediction JSON matching current frame."""

        if self._current_record is None or self._ai_prediction_frame_id is None:
            return

        frame_id = self._ai_prediction_frame_id
        predictions_dir = self._resolve_predictions_dir()
        if predictions_dir is None:
            return

        prediction_file = predictions_dir / f"frame_{frame_id:06d}_prediction.json"
        if not prediction_file.is_file():
            return

        try:
            with prediction_file.open("r", encoding="utf-8") as handle:
                data = __import__("json").load(handle)
        except (ValueError, OSError):
            return

        if not isinstance(data, dict):
            return

        # Only accept predictions for the current frame
        pred_frame_id = data.get("frame_id", "")
        expected_id = f"{frame_id:06d}"
        if pred_frame_id != expected_id:
            return

        if data.get("schema_version") != 1:
            return

        # Avoid redundant updates
        if self._ai_prediction == data:
            return

        self._ai_prediction = data
        self._update_ai_badge()

        # Refresh the info table if we have a current record and validation
        if self._current_record is not None and self._current_validation is not None:
            self._update_information(self._current_record, self._current_validation)

    def _update_ai_badge(self) -> None:
        """Update the AI status badge in the status bar."""

        if self._ai_prediction is None:
            self.ai_badge.setText(" AI WAITING ")
            self.ai_badge.setStyleSheet(
                "color: white; background: #616161; border-radius: 3px; "
                "font-weight: 700; padding: 2px 6px;"
            )
            self.ai_badge.setToolTip("Waiting for AI prediction")
            return

        status = self._ai_prediction.get("status", "")
        prediction = self._ai_prediction.get("prediction", "")
        confidence = self._ai_prediction.get("confidence_percent")

        if status == "FAIL":
            self.ai_badge.setText(" AI FAIL ")
            self.ai_badge.setStyleSheet(
                "color: white; background: #c62828; border-radius: 3px; "
                "font-weight: 700; padding: 2px 6px;"
            )
            error_msg = self._ai_prediction.get("error", "Inference failed")
            self.ai_badge.setToolTip(f"AI error: {error_msg}")
        elif prediction == "Unclassified":
            conf_text = f" {confidence:.1f}%" if confidence is not None else ""
            self.ai_badge.setText(f" AI UNCLASS{conf_text} ")
            self.ai_badge.setStyleSheet(
                "color: white; background: #ef6c00; border-radius: 3px; "
                "font-weight: 700; padding: 2px 6px;"
            )
            self.ai_badge.setToolTip(
                "AI prediction below confidence threshold"
            )
        else:
            conf_text = f" {confidence:.1f}%" if confidence is not None else ""
            short_label = prediction[:8] if prediction else "?"
            self.ai_badge.setText(f" AI {short_label}{conf_text} ")
            self.ai_badge.setStyleSheet(
                "color: white; background: #2e7d32; border-radius: 3px; "
                "font-weight: 700; padding: 2px 6px;"
            )
            latency = self._ai_prediction.get("latency_ms")
            tip = f"{prediction} ({confidence:.1f}%)"
            if latency is not None:
                tip += f" — {latency:.1f} ms"
            self.ai_badge.setToolTip(tip)

    def _update_ai_result_tab(self) -> None:
        """Update the dedicated AI Result tab card and probabilities table."""

        if self._ai_prediction is None:
            self.ai_card_title.setText("WAITING")
            self.ai_card_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #666;")
            self.ai_card_conf.setText("Confidence: —")
            self.ai_card_latency.setText("Latency: —")
            self._set_table_rows(self.ai_prob_table, [("AI Status", "WAITING")])
            return

        status = self._ai_prediction.get("status", "UNKNOWN")
        prediction = self._ai_prediction.get("prediction", "N/A")
        confidence = self._ai_prediction.get("confidence_percent")
        latency = self._ai_prediction.get("latency_ms")
        probabilities = self._ai_prediction.get("probabilities", {})

        if status == "FAIL":
            self.ai_card_title.setText("FAIL")
            self.ai_card_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #c62828;")
            self.ai_card_conf.setText("Inference Error")
            self.ai_card_latency.setText(f"Error: {self._ai_prediction.get('error', 'N/A')}")
        elif prediction == "Unclassified":
            self.ai_card_title.setText("UNCLASSIFIED")
            self.ai_card_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #ef6c00;")
            self.ai_card_conf.setText(f"Confidence: {confidence:.1f}%" if confidence is not None else "Confidence: N/A")
            self.ai_card_latency.setText(f"Latency: {latency:.1f} ms" if latency is not None else "Latency: N/A")
        else:
            self.ai_card_title.setText(str(prediction).upper())
            self.ai_card_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #2e7d32;")
            self.ai_card_conf.setText(f"Confidence: {confidence:.1f}%" if confidence is not None else "Confidence: N/A")
            self.ai_card_latency.setText(f"Latency: {latency:.1f} ms" if latency is not None else "Latency: N/A")

        prob_rows: list[tuple[str, Any]] = []
        if isinstance(probabilities, dict) and probabilities:
            for label, prob in probabilities.items():
                prob_rows.append((label, f"{prob:.1f}%"))
        prob_rows.extend([
            ("Status", status),
            ("Threshold", self._ai_prediction.get("threshold", "N/A")),
            ("Runtime", self._ai_prediction.get("runtime", "N/A")),
        ])
        if self._ai_prediction.get("warning"):
            prob_rows.append(("Warning", self._ai_prediction.get("warning")))
        self._set_table_rows(self.ai_prob_table, prob_rows)

    def _update_navigation_actions(self) -> None:
        current = self._current_record
        has_current = current is not None
        self.first_action.setEnabled(bool(has_current and self._repository.previous(current)))
        self.previous_action.setEnabled(bool(has_current and self._repository.previous(current)))
        self.next_action.setEnabled(bool(has_current and self._repository.next(current)))
        newest = self._newest_record()
        self.latest_action.setEnabled(
            bool(has_current and newest is not None and newest != current)
        )
        can_replay = bool(
            has_current
            and self._current_validation is not None
            and self._current_validation.can_plot
        )
        self.replay_action.setEnabled(can_replay)
        self.pause_action.setEnabled(can_replay)
        self.stop_action.setEnabled(can_replay)
        self.export_png_action.setEnabled(can_replay)
        self.copy_path_action.setEnabled(has_current)
        self.open_containing_action.setEnabled(has_current)

    def _go_first(self) -> None:
        self._navigate_to(self._repository.first())

    def _go_previous(self) -> None:
        record = (
            self._repository.previous(self._current_record)
            if self._current_record is not None
            else None
        )
        self._navigate_to(record)

    def _go_next(self) -> None:
        record = (
            self._repository.next(self._current_record)
            if self._current_record is not None
            else None
        )
        self._navigate_to(record)

    def _go_latest(self) -> None:
        self._navigate_to(self._newest_record())

    def _navigate_to(self, record: FrameRecord | None) -> None:
        if record is None:
            return
        if not self._record_is_stable(record):
            self.statusBar().showMessage(
                f"Waiting for {record.filename} to finish writing", 4000
            )
            return
        latest = self._newest_record()
        if (
            self.follow_live_checkbox.isChecked()
            and latest is not None
            and record.frame_id != latest.frame_id
        ):
            self.follow_live_checkbox.setChecked(False)
        self._select_record(record, user_requested=True, show_error=True)

    def _open_folder(self) -> None:
        mode_name = (
            "raw ADC"
            if self.data_mode_combo.currentText() == "Raw ADC"
            else "calibrated ECG"
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            f"Open {mode_name} folder",
            str(self._repository.data_dir),
        )
        if selected:
            self._set_data_directory(selected)

    def _follow_live_toggled(self, enabled: bool) -> None:
        if enabled:
            self._select_combo_data(self.replay_speed_combo, LIVE_REPLAY_SPEED)
        self.replay_speed_combo.setEnabled(not enabled)
        self.live_badge.setText(" LIVE 2× " if enabled else " LIVE ")
        self.live_badge.setVisible(enabled)
        self.live_status.setText(
            "Follow Live: ON · Replay 2×" if enabled else "Follow Live: OFF"
        )
        if enabled:
            self._pending_initial_frame_id = None
            self._seeking_first_valid_frame = False
            latest = self._newest_record()
            if latest is not None and self._record_is_stable(latest):
                self._select_record(
                    latest,
                    user_requested=False,
                    show_error=False,
                    live_replay=True,
                )
            elif latest is not None:
                self.statusBar().showMessage(
                    f"Following live; waiting for {latest.filename} to stabilize",
                    4000,
                )
        else:
            self._pending_initial_frame_id = None
            self._seeking_first_valid_frame = self._current_record is None
            self._pending_live_record = None

    def _on_watched_frames_changed(self, _records: object) -> None:
        current_id = self._current_record.frame_id if self._current_record else None
        try:
            self._repository.refresh()
        except OSError as error:
            LOGGER.warning("Watched directory refresh failed: %s", error)
            return
        self._populate_frame_combo()
        if (
            self._pending_initial_frame_id is not None
            and self._repository.get_by_id(self._pending_initial_frame_id) is None
        ):
            self._pending_initial_frame_id = None
        if current_id is not None and self._repository.get_by_id(current_id) is None:
            self._current_record = None
            self._current_validation = None
            self._current_source_signature = None
            self.plot_widget.clear_frame()
            fallback = self._ready_fallback_record(current_id)
            if fallback is not None:
                self._seeking_first_valid_frame = False
                self._select_record(
                    fallback,
                    user_requested=False,
                    show_error=False,
                    live_replay=self.follow_live_checkbox.isChecked(),
                )
            elif self._repository.frame_count():
                self._seeking_first_valid_frame = (
                    not self.follow_live_checkbox.isChecked()
                )
                self._show_waiting_for_stable(self._repository.data_dir)
            else:
                self._show_no_frames(self._repository.data_dir)
        elif current_id is None:
            if not self._repository.frame_count():
                self._show_no_frames(self._repository.data_dir)
            elif self._pending_initial_frame_id is not None:
                self._seeking_first_valid_frame = False
                self._show_waiting_for_stable(self._repository.data_dir)
            else:
                fallback = self._ready_fallback_record()
                if fallback is not None:
                    self._seeking_first_valid_frame = False
                    self._select_record(
                        fallback,
                        user_requested=False,
                        show_error=False,
                        live_replay=self.follow_live_checkbox.isChecked(),
                    )
                else:
                    self._seeking_first_valid_frame = (
                        not self.follow_live_checkbox.isChecked()
                    )
                    self._show_waiting_for_stable(self._repository.data_dir)
        self._update_navigation_actions()

    def _ready_fallback_record(
        self, removed_frame_id: int | None = None
    ) -> FrameRecord | None:
        """Return an already-announced frame after refresh or deletion.

        Existing stable files are deliberately not re-announced by
        :class:`FrameWatcher`. This prevents the UI from remaining empty when
        the selected file disappears or refresh starts without a selection.
        """

        records = list(self._repository.frames())
        if self.follow_live_checkbox.isChecked():
            candidates = sorted(
                records, key=self._record_freshness_key, reverse=True
            )
        elif removed_frame_id is None:
            candidates = records
        else:
            later = [
                record for record in records if record.frame_id > removed_frame_id
            ]
            earlier = [
                record for record in records if record.frame_id < removed_frame_id
            ]
            candidates = later + list(reversed(earlier))

        for candidate in candidates:
            if self._record_is_stable(candidate):
                return candidate
        return None

    def _on_stable_frame_available(self, record: FrameRecord) -> None:
        refreshed = self._repository.get_by_id(record.frame_id) or record
        if self.follow_live_checkbox.isChecked():
            self._queue_live_record(refreshed)
        elif self._pending_initial_frame_id is not None:
            if refreshed.frame_id == self._pending_initial_frame_id:
                self._pending_initial_frame_id = None
                self._seeking_first_valid_frame = False
                self._select_record(
                    refreshed, user_requested=False, show_error=False
                )
        elif self._seeking_first_valid_frame:
            candidate_result = self._validator.validate(refreshed)
            if self._current_record is None or candidate_result.can_plot:
                self._select_record(
                    refreshed, user_requested=False, show_error=False
                )
            if candidate_result.can_plot:
                self._seeking_first_valid_frame = False
        elif (
            self._current_record is not None
            and refreshed.frame_id == self._current_record.frame_id
            and self._record_signature(refreshed)
            != self._current_source_signature
        ):
            self._select_record(
                refreshed, user_requested=False, show_error=False
            )
        self._update_navigation_actions()

    def _queue_live_record(self, record: FrameRecord) -> None:
        """Coalesce ready signals to the most recently updated frame."""

        pending = self._pending_live_record
        if (
            pending is None
            or self._record_freshness_key(record)
            >= self._record_freshness_key(pending)
        ):
            self._pending_live_record = record
        if not self._live_selection_scheduled:
            self._live_selection_scheduled = True
            QTimer.singleShot(0, self._flush_pending_live_record)

    def _flush_pending_live_record(self) -> None:
        """Plot only the newest ready frame after a synchronous scan burst."""

        self._live_selection_scheduled = False
        pending = self._pending_live_record
        self._pending_live_record = None
        if pending is None or not self.follow_live_checkbox.isChecked():
            return

        refreshed = self._repository.get_by_id(pending.frame_id)
        if refreshed is not None and self._record_is_stable(refreshed):
            candidate = refreshed
        elif self._record_is_stable(pending):
            candidate = pending
        else:
            return

        current = self._current_record
        candidate_signature = self._record_signature(candidate)
        replacement = (
            current is not None
            and candidate.frame_id == current.frame_id
            and candidate_signature != self._current_source_signature
        )
        newer_file = (
            current is None
            or (
                candidate.frame_id != current.frame_id
                and self._record_freshness_key(candidate)
                > self._record_freshness_key(current)
            )
        )
        if replacement or newer_file:
            self._select_record(
                candidate,
                user_requested=False,
                show_error=False,
                live_replay=True,
            )

    def _record_is_stable(self, record: FrameRecord) -> bool:
        return hasattr(self, "_watcher") and self._watcher.is_ready(record)

    def _newest_record(self) -> FrameRecord | None:
        """Return newest frame by file update time, with ID only as a tie-break."""

        records = self._repository.frames()
        if not records:
            return None
        return max(records, key=self._record_freshness_key)

    @staticmethod
    def _record_freshness_key(record: FrameRecord) -> tuple[int, int, int]:
        """Order frames by NPY/metadata mtime rather than numerical frame ID."""

        try:
            npy_modified_ns = int(record.npy_path.stat().st_mtime_ns)
        except OSError:
            npy_modified_ns = -1
        try:
            metadata_modified_ns = (
                int(record.metadata_path.stat().st_mtime_ns)
                if record.metadata_path is not None
                else -1
            )
        except OSError:
            metadata_modified_ns = -1
        return npy_modified_ns, metadata_modified_ns, record.frame_id

    @staticmethod
    def _record_signature(record: FrameRecord) -> tuple[Any, ...] | None:
        try:
            npy_stat = record.npy_path.stat()
            npy_signature: tuple[Any, ...] = (
                npy_stat.st_dev,
                npy_stat.st_ino,
                npy_stat.st_size,
                npy_stat.st_mtime_ns,
            )
            metadata_path = record.metadata_path
            if metadata_path is None:
                candidate = record.npy_path.with_suffix(".json")
                try:
                    candidate_stat = candidate.stat()
                except FileNotFoundError:
                    return (*npy_signature, None)
                if not stat_module.S_ISREG(candidate_stat.st_mode):
                    return (*npy_signature, None)
                json_stat = candidate_stat
            else:
                json_stat = metadata_path.stat()
            return (
                *npy_signature,
                json_stat.st_dev,
                json_stat.st_ino,
                json_stat.st_size,
                json_stat.st_mtime_ns,
            )
        except OSError:
            return None

    def _on_directory_unavailable(self, path: str) -> None:
        self.statusBar().showMessage(f"Directory temporarily unavailable: {path}", 5000)

    def _on_watcher_error(self, message: str) -> None:
        LOGGER.warning("Frame watcher: %s", message)
        self.statusBar().showMessage(message, 5000)

    def _start_replay(self) -> None:
        if self._current_validation is None or not self._current_validation.can_plot:
            return
        self.plot_widget.start_replay(float(self.replay_speed_combo.currentData()))

    def _pause_replay(self) -> None:
        self.plot_widget.pause_replay()

    def _stop_replay(self) -> None:
        if hasattr(self, "plot_widget"):
            self.plot_widget.stop_replay()

    def _toggle_replay(self) -> None:
        state = str(getattr(self.plot_widget, "replay_state", "stopped")).lower()
        if "playing" in state:
            self._pause_replay()
        else:
            self._start_replay()

    def _on_replay_state_changed(self, state: object) -> None:
        self.statusBar().showMessage(f"Replay: {state}", 1500)

    def _on_replay_finished(self) -> None:
        self.statusBar().showMessage("Replay complete", 2500)

    def _replay_speed_changed(self) -> None:
        self.plot_widget.set_replay_speed(float(self.replay_speed_combo.currentData()))

    def _paper_speed_changed(self) -> None:
        self.plot_widget.set_paper_speed(float(self.paper_speed_combo.currentData()))

    def _gain_changed(self) -> None:
        self.plot_widget.set_gain(float(self.gain_combo.currentData()))

    def _time_window_changed(self) -> None:
        self.plot_widget.set_time_window(float(self.time_window_combo.currentData()))
        self.plot_widget.reset_view()

    def _y_range_changed(self) -> None:
        self.plot_widget.set_y_range(float(self.y_range_combo.currentData()))
        self.plot_widget.reset_view()

    def _reset_view(self) -> None:
        self.plot_widget.reset_view()

    def _auto_scale_y(self) -> None:
        self.plot_widget.auto_scale_y()

    def _show_display_calibration(self) -> None:
        current = load_viewer_settings(self._settings).display_scale_factor
        dialog = DisplayCalibrationDialog(
            parent=self,
            qsettings=self._settings,
            scale_factor=current,
        )
        if dialog.exec():
            factor = float(dialog.scale_factor)
            self._settings.setValue("display/calibration_factor", factor)
            self.plot_widget.set_display_scale_factor(factor)

    def _export_png(self) -> None:
        if self._current_record is None or self._current_validation is None:
            return
        saved_export_dir = str(
            self._settings.value("paths/last_export_directory", "") or ""
        )
        export_dir = Path(saved_export_dir).expanduser() if saved_export_dir else None
        if export_dir is None or not export_dir.is_dir():
            pictures = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.PicturesLocation
            )
            export_dir = Path(pictures) if pictures else Path.home()
        suggested = export_dir / f"{self._current_record.npy_path.stem}_view.png"
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export current ECG view",
            str(suggested),
            "PNG image (*.png)",
        )
        if not target:
            return
        path = Path(target)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        try:
            self.plot_widget.export_png(path)
        except (OSError, RuntimeError, ValueError) as error:
            self._show_nonfatal_error("Unable to export PNG", error)
            return
        self._settings.setValue("paths/last_export_directory", str(path.parent))
        self.statusBar().showMessage(f"Exported {path}", 5000)

    def _copy_current_path(self) -> None:
        if self._current_record is None:
            return
        QApplication.clipboard().setText(str(self._current_record.npy_path))
        self.statusBar().showMessage("Frame path copied", 2000)

    def _open_containing_folder(self) -> None:
        if self._current_record is None:
            return
        if not QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._current_record.npy_path.parent))
        ):
            self._show_nonfatal_error(
                "Unable to open containing folder",
                OSError(str(self._current_record.npy_path.parent)),
            )

    def _show_nonfatal_error(self, title: str, error: Exception) -> None:
        LOGGER.warning("%s: %s", title, error)
        QMessageBox.warning(self, title, str(error))

    def _save_settings(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue(
            "display/paper_speed_mm_s", self.paper_speed_combo.currentData()
        )
        self._settings.setValue(
            "display/gain_mm_per_mv", self.gain_combo.currentData()
        )
        self._settings.setValue(
            "display/time_window_s", self.time_window_combo.currentData()
        )
        self._settings.setValue(
            "display/y_range_mv", self.y_range_combo.currentData()
        )
        self._settings.setValue(
            "display/lead_mode", self.lead_mode_combo.currentText()
        )
        self._settings.setValue(
            "display/grid_enabled", self.grid_checkbox.isChecked()
        )
        self._settings.setValue(
            "display/crosshair_enabled", self.crosshair_checkbox.isChecked()
        )
        self._settings.setValue(
            "display/lock_view", self.lock_view_checkbox.isChecked()
        )
        self._settings.setValue(
            "display/synchronize_y_ranges", self.sync_y_checkbox.isChecked()
        )
        self._settings.setValue(
            "display/line_width", self.line_width_spin.value()
        )
        self._settings.setValue(
            "replay/speed", self.replay_speed_combo.currentData()
        )
        self._settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._save_settings()
        self._settings.setValue("window/splitter_state", self._central_splitter.saveState())
        self._settings.setValue("display/show_details", self.details_button.isChecked())
        self._watcher.stop()
        self.plot_widget.stop_replay()
        super().closeEvent(event)


__all__ = ["MainWindow"]
