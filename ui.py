"""Picard 3 interface for the library audit and reviewed folder repair."""

from __future__ import annotations

from pathlib import Path
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .core import AlbumReport, MovePlan, apply_consolidation, plan_consolidation, scan_library


class ScanThread(QThread):
    progress = pyqtSignal(int, str)
    scanned = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = root
        self.cancel = threading.Event()

    def run(self):
        try:
            result = scan_library(self.root, self.cancel, lambda count, path: self.progress.emit(count, str(path)))
            self.scanned.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ApplyThread(QThread):
    completed = pyqtSignal(object)
    progress = pyqtSignal(int, int, str)

    def __init__(self, plans: list[MovePlan], parent=None):
        super().__init__(parent)
        self.plans = plans

    def run(self):
        results = []
        total_files = sum(len(plan.actions) for plan in self.plans)
        completed_files = 0
        for plan in self.plans:
            before = completed_files
            result = apply_consolidation(
                plan,
                lambda done, _total, path: self.progress.emit(before + done, total_files, path.name),
            )
            results.append(result)
            completed_files += len(result.moved)
            if result.errors:
                break
        self.completed.emit(results)


class LibraryRepairDialog(QDialog):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.reports: list[AlbumReport] = []
        self.worker: ScanThread | None = None
        self.apply_worker: ApplyThread | None = None
        self.setWindowTitle("Library Repair")
        self.resize(1000, 660)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Scan a music folder. Only albums needing attention appear below."))

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home() / "Music"))
        self.folder.setPlaceholderText("Choose a music folder")
        browse = QPushButton("Choose folder…")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        controls = QHBoxLayout()
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button = QPushButton("Cancel scan")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.status = QLabel("Ready")
        controls.addWidget(self.scan_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.status, 1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Album folder", "Tracks", "Problems"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 590)
        self.table.setColumnWidth(1, 70)
        self.table.itemSelectionChanged.connect(self.show_details)
        layout.addWidget(self.table, 3)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select an album to see the failing tracks and reasons.")
        layout.addWidget(self.details, 2)

        action_row = QHBoxLayout()
        self.open_button = QPushButton("Open selected in Picard")
        self.open_button.clicked.connect(self.open_in_picard)
        self.preview_button = QPushButton("Preview disc consolidation…")
        self.preview_button.clicked.connect(self.preview_moves)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        action_row.addWidget(self.open_button)
        action_row.addWidget(self.preview_button)
        action_row.addStretch(1)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose music folder", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def start_scan(self):
        if self.worker and self.worker.isRunning():
            return
        root = Path(self.folder.text()).expanduser()
        if not root.is_dir():
            QMessageBox.warning(self, "Invalid folder", f"Folder does not exist:\n{root}")
            return
        self.reports.clear()
        self.table.setRowCount(0)
        self.details.clear()
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("Scanning…")
        self.worker = ScanThread(root, self)
        self.worker.progress.connect(lambda count, path: self.status.setText(f"Read {count} tracks · {path}"))
        self.worker.scanned.connect(self.scan_complete)
        self.worker.failed.connect(self.scan_failed)
        self.worker.finished.connect(self.scan_finished)
        self.worker.start()

    def cancel_scan(self):
        if self.worker:
            self.worker.cancel.set()
            self.status.setText("Cancelling…")

    def scan_finished(self):
        self.scan_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def scan_failed(self, message: str):
        self.status.setText("Scan failed")
        QMessageBox.critical(self, "Scan failed", message)

    def scan_complete(self, result):
        if result.cancelled:
            self.status.setText(f"Cancelled after {result.files_seen} tracks")
            return
        self.reports = result.albums
        self.table.setRowCount(len(self.reports))
        for row, report in enumerate(self.reports):
            self.table.setItem(row, 0, QTableWidgetItem(str(report.root)))
            self.table.setItem(row, 1, QTableWidgetItem(str(len(report.tracks))))
            problems = set(report.issues)
            problems.update(issue for track in report.tracks for issue in track.issues)
            self.table.setItem(row, 2, QTableWidgetItem("; ".join(sorted(problems))))
        self.status.setText(f"{len(self.reports)} problem albums from {result.files_seen} tracks")

    def selected_reports(self) -> list[AlbumReport]:
        return [self.reports[index.row()] for index in self.table.selectionModel().selectedRows()]

    def show_details(self):
        chosen = self.selected_reports()
        if not chosen:
            self.details.clear()
            return
        lines = []
        for report in chosen:
            lines.append(str(report.root))
            lines.extend(f"  • {issue}" for issue in report.issues)
            for track in report.tracks:
                if track.issues:
                    lines.append(f"  {track.path.relative_to(report.root)}: {', '.join(track.issues)}")
            lines.append("")
        self.details.setPlainText("\n".join(lines))

    def open_in_picard(self):
        chosen = self.selected_reports()
        if not chosen:
            return
        paths = [str(track.path) for report in chosen for track in report.tracks if track.path.exists()]
        if not paths:
            QMessageBox.information(self, "No files", "The selected albums have no existing audio files.")
            return
        self.api.tagger.add_files(paths)
        self.status.setText(f"Loaded {len(paths)} files into Picard; review matches before saving")

    def preview_moves(self):
        chosen = self.selected_reports()
        if not chosen:
            return
        reports = [report for report in chosen if report.disc_dirs]
        if not reports:
            QMessageBox.information(self, "No disc folders", "Select an album with CD or Disc subfolders.")
            return
        plans = [plan_consolidation(report) for report in reports]
        self._show_plan(plans)

    def _show_plan(self, plans: list[MovePlan]):
        dialog = QDialog(self)
        dialog.setWindowTitle("Review disc consolidation")
        dialog.resize(950, 600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Review every move and tag change. Existing files are never overwritten."))
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        lines = []
        for plan in plans:
            lines.append(f"ALBUM: {plan.album}")
            for action in plan.actions:
                lines.append(f"MOVE  {action.source}  →  {action.destination}")
                if action.tag_updates:
                    lines.append(f"      Set numeric tags: {action.tag_updates}")
                if action.sidecar:
                    lines.append(f"      Remove paired sidecar after move: {action.sidecar}")
            lines.extend(f"CONFLICT  {problem}" for problem in plan.conflicts)
            lines.extend(plan.retained)
            lines.append("Remove CD folders only when empty.\n")
        preview.setPlainText("\n".join(lines))
        layout.addWidget(preview)
        row = QHBoxLayout()
        apply_button = QPushButton("Apply reviewed moves")
        apply_button.setEnabled(all(plan.ready for plan in plans))
        apply_button.clicked.connect(lambda: self._apply_plans(dialog, plans))
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(dialog.reject)
        row.addStretch(1)
        row.addWidget(apply_button)
        row.addWidget(cancel_button)
        layout.addLayout(row)
        dialog.exec()

    def _apply_plans(self, dialog: QDialog, plans: list[MovePlan]):
        dialog.accept()
        self.scan_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.status.setText("Applying reviewed moves…")
        self.apply_worker = ApplyThread(plans, self)
        self.apply_worker.progress.connect(lambda count, total, name: self.status.setText(f"Moved {count} of {total} files · {name}"))
        self.apply_worker.completed.connect(self._apply_complete)
        self.apply_worker.start()

    def _apply_complete(self, results):
        moved = 0
        removed_dirs = 0
        errors = []
        for result in results:
            moved += len(result.moved)
            removed_dirs += len(result.removed_dirs)
            errors.extend(result.errors)
        summary = f"Moved {moved} files and removed {removed_dirs} empty disc folders."
        if errors:
            QMessageBox.warning(self, "Consolidation stopped", summary + "\n\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Consolidation complete", summary)
        self.status.setText(summary + " Rescan to refresh the report.")
        self.scan_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.preview_button.setEnabled(True)

    def closeEvent(self, event):
        if self.apply_worker and self.apply_worker.isRunning():
            event.ignore()
            self.status.setText("Finishing file moves before closing…")
            self.apply_worker.finished.connect(self.close)
            return
        if self.worker and self.worker.isRunning():
            self.cancel_scan()
            event.ignore()
            self.status.setText("Cancelling scan before closing…")
            self.worker.finished.connect(self.close)
        else:
            super().closeEvent(event)
