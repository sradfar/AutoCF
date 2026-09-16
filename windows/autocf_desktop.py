"""AutoCF Windows desktop front end."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def package_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


class AutoCFWindow(QMainWindow):
    OPERATIONS = [
        ("Build project only (CPU)", "build"),
        ("Run existing project (CPU)", "run"),
        ("Build + run project (CPU)", "full"),
        ("Evaluate results (maps + gauges)", "evaluate"),
        ("Impact attribution (Shapely + exposure)", "impact"),
        ("Evaluate + impact attribution", "evaluate_impact"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.root = package_root()
        self.backend = self.root / "windows" / "AutoCF-Windows.ps1"
        self.process: QProcess | None = None
        self.command_queue: list[list[str]] = []
        self.failed = False
        self.setWindowTitle("AutoCF Desktop")
        self.resize(1220, 900)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 18)
        layout.setSpacing(14)

        title = QLabel("AutoCF Desktop")
        title.setObjectName("title")
        subtitle = QLabel("Build, run, evaluate, and attribute HydroMT–SFINCS flood simulations with the bundled CPU runtime.")
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        simulation = QGroupBox("Project")
        project_grid = QGridLayout(simulation)
        self.config = QLineEdit(str(self.root / "examples" / "sfincs_agent_subgrid_config.yml"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_config)
        self.operation = QComboBox()
        for label, key in self.OPERATIONS:
            self.operation.addItem(label, key)
        self.operation.setCurrentIndex(0)
        project_grid.addWidget(QLabel("Configuration"), 0, 0)
        project_grid.addWidget(self.config, 0, 1)
        project_grid.addWidget(browse, 0, 2)
        project_grid.addWidget(QLabel("Operation"), 1, 0)
        project_grid.addWidget(self.operation, 1, 1, 1, 2)
        layout.addWidget(simulation)

        tabs = QTabWidget()
        eval_tab = QWidget()
        eval_form = QFormLayout(eval_tab)
        self.eval_hmax = QCheckBox("Create flood-depth map and GeoTIFF")
        self.eval_hmax.setChecked(True)
        self.eval_observations = QCheckBox("Compare SFINCS with configured gauges")
        self.eval_observations.setChecked(True)
        self.eval_tolerance = QDoubleSpinBox()
        self.eval_tolerance.setRange(0.1, 120.0)
        self.eval_tolerance.setValue(4.0)
        self.eval_tolerance.setSuffix(" min")
        self.eval_vmax = QDoubleSpinBox()
        self.eval_vmax.setRange(0.1, 100.0)
        self.eval_vmax.setValue(3.0)
        self.eval_vmax.setSuffix(" m")
        eval_form.addRow(self.eval_hmax)
        eval_form.addRow(self.eval_observations)
        eval_form.addRow("Gauge match tolerance", self.eval_tolerance)
        eval_form.addRow("Map color maximum", self.eval_vmax)
        tabs.addTab(eval_tab, "Evaluation")

        impact_tab = QWidget()
        impact_form = QFormLayout(impact_tab)
        self.impact_buildings = QCheckBox("Buildings — Overture Maps footprints")
        self.impact_buildings.setChecked(True)
        self.impact_roads = QCheckBox("Roads — Overture Maps transportation segments")
        self.impact_roads.setChecked(True)
        self.impact_population = QCheckBox("Population — WorldPop API v2")
        self.impact_population.setChecked(True)
        self.impact_thresholds = QLineEdit("0.15,0.3,0.6,1.0,2.0")
        self.impact_resolution = QDoubleSpinBox()
        self.impact_resolution.setRange(3.0, 1000.0)
        self.impact_resolution.setValue(30.0)
        self.impact_resolution.setSuffix(" m")
        self.force_downloads = QCheckBox("Refresh cached exposure data and WorldPop results")
        impact_form.addRow(self.impact_buildings)
        impact_form.addRow(self.impact_roads)
        impact_form.addRow(self.impact_population)
        impact_form.addRow("Flood-depth thresholds (m)", self.impact_thresholds)
        impact_form.addRow("Attribution grid resolution", self.impact_resolution)
        impact_form.addRow(self.force_downloads)
        tabs.addTab(impact_tab, "Impact attribution")
        layout.addWidget(tabs)

        controls = QHBoxLayout()
        install = QPushButton("Install / repair runtime")
        install.clicked.connect(self.install_runtime)
        status = QPushButton("System status")
        status.clicked.connect(self.system_status)
        self.cancel = QPushButton("Cancel")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_process)
        self.run_button = QPushButton("Run AutoCF")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_operation)
        controls.addWidget(install)
        controls.addWidget(status)
        controls.addStretch(1)
        controls.addWidget(self.cancel)
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Cascadia Mono", 10))
        self.output.setPlaceholderText("AutoCF output will appear here.")
        layout.addWidget(self.output, 1)
        input_row = QHBoxLayout()
        self.process_input = QLineEdit()
        self.process_input.setPlaceholderText("Type a response for the running job, then press Enter")
        self.process_input.setEnabled(False)
        self.process_input.returnPressed.connect(self.send_process_input)
        self.send_input_button = QPushButton("Send")
        self.send_input_button.setEnabled(False)
        self.send_input_button.clicked.connect(self.send_process_input)
        input_row.addWidget(self.process_input, 1)
        input_row.addWidget(self.send_input_button)
        layout.addLayout(input_row)
        self.status_label = QLabel("Ready — SFINCS execution is CPU (OpenMP).")
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #191919; color: #f2f2f2; }
            QLabel#title { font-size: 30px; font-weight: 700; }
            QLabel#subtitle { color: #7692a6; font-size: 15px; }
            QGroupBox { border: 1px solid #343434; border-radius: 4px; margin-top: 10px; padding-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QComboBox, QDoubleSpinBox, QTextEdit, QTabWidget::pane {
                background: #292929; border: 1px solid #414141; padding: 5px;
            }
            QTabBar::tab { background: #242424; padding: 8px 16px; border: 1px solid #3c3c3c; }
            QTabBar::tab:selected { background: #353535; }
            QPushButton { background: #3a3a3a; border: 1px solid #555; padding: 7px 12px; border-radius: 3px; }
            QPushButton:hover { background: #494949; }
            QPushButton:disabled { color: #888; background: #303030; }
            QPushButton#primary { border-color: #3a9be8; background: #31526b; }
            """
        )

    def choose_config(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Choose AutoCF configuration", str(self.root / "examples"), "YAML (*.yml *.yaml)")
        if selected:
            self.config.setText(selected)

    def powershell(self, arguments: list[str]) -> list[str]:
        return ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.backend), *arguments]

    def common_config(self) -> list[str] | None:
        config = Path(self.config.text().strip())
        if not config.is_file():
            QMessageBox.warning(self, "Configuration not found", "Choose an existing AutoCF YAML configuration file.")
            return None
        return ["-Config", str(config.resolve())]

    def evaluation_args(self) -> list[str]:
        args = [
            "-Action", "Evaluate",
            "-EvaluationToleranceMinutes", f"{self.eval_tolerance.value():g}",
            "-EvaluationHmaxVmax", f"{self.eval_vmax.value():g}",
        ]
        if not self.eval_hmax.isChecked():
            args.append("-SkipEvaluationHmax")
        if not self.eval_observations.isChecked():
            args.append("-SkipEvaluationObservations")
        return args

    def impact_args(self) -> list[str]:
        args = [
            "-Action", "Impact",
            "-ImpactThresholds", self.impact_thresholds.text().strip(),
            "-ImpactAnalysisResolution", f"{self.impact_resolution.value():g}",
        ]
        if not self.impact_buildings.isChecked():
            args.append("-SkipImpactBuildings")
        if not self.impact_roads.isChecked():
            args.append("-SkipImpactRoads")
        if not self.impact_population.isChecked():
            args.append("-SkipImpactPopulation")
        if self.force_downloads.isChecked():
            args.append("-ForceImpactDownloads")
        return args

    def run_operation(self) -> None:
        config_args = self.common_config()
        if config_args is None:
            return
        key = self.operation.currentData()
        if key in {"build", "run", "full"}:
            commands = [self.powershell(["-Action", "Run", *config_args, "-Mode", key])]
        elif key == "evaluate":
            commands = [self.powershell([*self.evaluation_args(), *config_args])]
        elif key == "impact":
            commands = [self.powershell([*self.impact_args(), *config_args])]
        else:
            commands = [
                self.powershell([*self.evaluation_args(), *config_args]),
                self.powershell([*self.impact_args(), *config_args]),
            ]
        self.start_commands(commands, f"> {self.operation.currentText()}")

    def install_runtime(self) -> None:
        cmd = self.root / "Install-AutoCF.cmd"
        self.start_commands([["cmd.exe", "/d", "/c", str(cmd), "--non-interactive"]], "> Install / repair AutoCF runtime\nAccept the Windows administrator prompt if it appears.")

    def system_status(self) -> None:
        self.start_commands([self.powershell(["-Action", "Status"])], "> AutoCF Status")

    def start_commands(self, commands: list[list[str]], heading: str) -> None:
        if self.process is not None:
            return
        self.output.clear()
        self.output.append(heading)
        self.command_queue = commands
        self.failed = False
        self.run_button.setEnabled(False)
        self.cancel.setEnabled(True)
        self._start_next()

    def _start_next(self) -> None:
        if not self.command_queue:
            self._finish_all()
            return
        command = self.command_queue.pop(0)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUTF8", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(self.root))
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.status_label.setText("Running…")
        self.process_input.setEnabled(True)
        self.send_input_button.setEnabled(True)
        self.process.start(command[0], command[1:])

    def send_process_input(self) -> None:
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.process.write((self.process_input.text() + "\r\n").encode("utf-8"))
        self.process_input.clear()

    def read_output(self) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").replace("\x00", "")
        if data:
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(data)
            self.output.setTextCursor(cursor)
            self.output.ensureCursorVisible()

    def process_finished(self, code: int, _status: QProcess.ExitStatus) -> None:
        self.read_output()
        self.process = None
        if code != 0:
            self.failed = True
            self.command_queue.clear()
            self.output.append(f"\nFailed with exit code {code}.")
        self._start_next()

    def process_error(self, error: QProcess.ProcessError) -> None:
        self.output.append(f"\nCould not start AutoCF process: {error}")
        self.process_input.setEnabled(False)
        self.send_input_button.setEnabled(False)

    def _finish_all(self) -> None:
        self.run_button.setEnabled(True)
        self.cancel.setEnabled(False)
        self.process_input.clear()
        self.process_input.setEnabled(False)
        self.send_input_button.setEnabled(False)
        self.status_label.setText("Failed" if self.failed else "Completed successfully")

    def cancel_process(self) -> None:
        self.command_queue.clear()
        if self.process is not None:
            self.process.kill()
        self.failed = True
        self.process_input.clear()
        self.process_input.setEnabled(False)
        self.send_input_button.setEnabled(False)
        self.status_label.setText("Cancelled")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AutoCF Desktop")
    window = AutoCFWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
