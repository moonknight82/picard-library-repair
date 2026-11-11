"""Picard 3 interface for the library audit and reviewed folder repair."""

from __future__ import annotations

from pathlib import Path
import threading

from PyQt6.QtCore import QItemSelectionModel, QThread, Qt, pyqtSignal
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


class RefreshThread(QThread):
    refreshed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, roots: list[Path], parent=None):
        super().__init__(parent)
        self.roots = roots
        self.cancel = threading.Event()

    def run(self):
        try:
            refreshed = {}
            for root in self.roots:
                if self.cancel.is_set():
                    return
                # A scan of one album also sees its CD folders. Keep only the
                # report for that album, not unrelated nested folders.
                result = scan_library(root, self.cancel)
                if result.cancelled:
                    return
                refreshed[root] = next((r for r in result.albums if r.root == root), None)
            self.refreshed.emit(refreshed)
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


class ConsolidationReviewDialog(QDialog):
    def __init__(self, plans: list[MovePlan], parent=None):
        super().__init__(parent)
        self.plans = plans
        self.selected_plans: list[MovePlan] = []
        self.setWindowTitle("Review disc consolidations")
        self.resize(1050, 670)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Check the albums to consolidate. Conflicts stay in the audit list for review."))
        self.table = QTableWidget(len(plans), 3)
        self.table.setHorizontalHeaderLabels(["Apply", "Album", "Status"])
        self.table.setColumnWidth(0, 65)
        self.table.setColumnWidth(1, 700)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, plan in enumerate(plans):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if plan.ready else Qt.CheckState.Unchecked)
            if not plan.ready:
                check.setFlags(Qt.ItemFlag.ItemIsUserCheckable)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(str(plan.album)))
            self.table.setItem(row, 2, QTableWidgetItem(
                f"Ready · {len(plan.actions)} moves" if plan.ready else f"Conflict · {len(plan.conflicts)} issues"
            ))
        self.table.itemSelectionChanged.connect(self.show_selected_preview)
        layout.addWidget(self.table, 2)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 3)
        if plans:
            self.table.selectRow(0)
        row = QHBoxLayout()
        apply_button = QPushButton("Apply checked albums")
        apply_button.clicked.connect(self.accept_checked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(apply_button)
        row.addWidget(cancel_button)
        layout.addLayout(row)

    def show_selected_preview(self):
        rows = [index.row() for index in self.table.selectionModel().selectedRows()]
        if not rows and self.plans:
            rows = [0]
        lines = ["Existing files are never overwritten. Review each move and tag change.\n"]
        for row in rows:
            plan = self.plans[row]
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
        self.preview.setPlainText("\n".join(lines))

    def accept_checked(self):
        self.selected_plans = [
            plan for row, plan in enumerate(self.plans)
            if plan.ready and self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        if not self.selected_plans:
            QMessageBox.information(self, "Nothing selected", "Check at least one ready album to consolidate.")
            return
        self.accept()


class LibraryRepairDialog(QDialog):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.reports: list[AlbumReport] = []
        self.worker: ScanThread | None = None
        self.refresh_worker: RefreshThread | None = None
        self.apply_worker: ApplyThread | None = None
        self._last_apply_summary = ""
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

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Album folder", "Tracks", "Folder layout", "Metadata / artwork"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 520)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 170)
        self.table.itemSelectionChanged.connect(self.show_details)
        layout.addWidget(self.table, 3)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select an album to see the failing tracks and reasons.")
        layout.addWidget(self.details, 2)

        action_row = QHBoxLayout()
        self.select_discs_button = QPushButton("Select all disc-folder albums")
        self.select_discs_button.clicked.connect(self.select_disc_albums)
        self.select_tags_button = QPushButton("Select tag/artwork albums")
        self.select_tags_button.clicked.connect(self.select_tag_albums)
        self.open_button = QPushButton("Open selected in Picard")
        self.open_button.clicked.connect(self.open_in_picard)
        self.preview_button = QPushButton("Review consolidations…")
        self.preview_button.clicked.connect(self.preview_moves)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        action_row.addWidget(self.select_discs_button)
        action_row.addWidget(self.select_tags_button)
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
        if (self.worker and self.worker.isRunning()) or (self.refresh_worker and self.refresh_worker.isRunning()) or (self.apply_worker and self.apply_worker.isRunning()):
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
        self._set_album_actions_enabled(False)
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
        self._set_album_actions_enabled(True)

    def scan_failed(self, message: str):
        self.status.setText("Scan failed")
        QMessageBox.critical(self, "Scan failed", message)

    def scan_complete(self, result):
        if result.cancelled:
            self.status.setText(f"Cancelled after {result.files_seen} tracks")
            return
        self.reports = result.albums
        self.render_reports()
        self.status.setText(f"{len(self.reports)} problem albums from {result.files_seen} tracks")

    def render_reports(self):
        self.reports.sort(key=lambda album: str(album.root).casefold())
        self.table.blockSignals(True)
        try:
            self.table.clearSelection()
            self.table.setRowCount(len(self.reports))
            for row, report in enumerate(self.reports):
                self.table.setItem(row, 0, QTableWidgetItem(str(report.root)))
                self.table.setItem(row, 1, QTableWidgetItem(str(len(report.tracks))))
                self.table.setItem(row, 2, QTableWidgetItem(
                    ", ".join(path.name for path in report.disc_dirs) if report.disc_dirs else "—"
                ))
                problems = {issue for issue in report.issues if not issue.startswith("Disc subfolders:")}
                problems.update(issue for track in report.tracks for issue in track.issues)
                self.table.setItem(row, 3, QTableWidgetItem("; ".join(sorted(problems)) or "—"))
        finally:
            self.table.blockSignals(False)
        self.details.clear()

    def _set_album_actions_enabled(self, enabled: bool):
        self.open_button.setEnabled(enabled)
        self.preview_button.setEnabled(enabled)
        self.select_discs_button.setEnabled(enabled)
        self.select_tags_button.setEnabled(enabled)

    def select_disc_albums(self):
        selection = self.table.selectionModel()
        selection.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row, report in enumerate(self.reports):
            if report.disc_dirs:
                selection.select(self.table.model().index(row, 0), flags)
        count = sum(bool(report.disc_dirs) for report in self.reports)
        self.status.setText(f"Selected {count} disc-folder albums")

    def select_tag_albums(self):
        selection = self.table.selectionModel()
        selection.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        count = 0
        for row, report in enumerate(self.reports):
            if not report.disc_dirs and report.problem:
                selection.select(self.table.model().index(row, 0), flags)
                count += 1
        self.status.setText(f"Selected {count} tag/artwork albums")

    def selected_reports(self) -> list[AlbumReport]:
        return [self.reports[index.row()] for index in self.table.selectionModel().selectedRows() if index.row() < len(self.reports)]

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
        dialog = ConsolidationReviewDialog(plans, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_plans(dialog.selected_plans)

    def _apply_plans(self, plans: list[MovePlan]):
        self.scan_button.setEnabled(False)
        self._set_album_actions_enabled(False)
        self.status.setText("Applying reviewed moves…")
        self.apply_worker = ApplyThread(plans, self)
        self.apply_worker.progress.connect(lambda count, total, name: self.status.setText(f"Moved {count} of {total} files · {name}"))
        self.apply_worker.completed.connect(lambda results: self._apply_complete(plans, results))
        self.apply_worker.start()

    def _apply_complete(self, plans, results):
        moved = 0
        removed_dirs = 0
        errors = []
        for result in results:
            moved += len(result.moved)
            removed_dirs += len(result.removed_dirs)
            errors.extend(result.errors)
        attempted = len(results)
        completed = sum(not result.errors for result in results)
        summary = (
            f"Completed {completed} of {len(plans)} albums; moved {moved} files "
            f"and removed {removed_dirs} empty disc folders."
        )
        if errors:
            failed_album = plans[attempted - 1].album if attempted else plans[0].album
            summary += f" Stopped at {failed_album}."
            QMessageBox.warning(self, "Consolidation stopped", summary + "\n\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Consolidation complete", summary)
        self._last_apply_summary = summary
        self.status.setText(summary + " Refreshing affected albums…")
        self.refresh_worker = RefreshThread([plan.album for plan in plans[:attempted]], self)
        self.refresh_worker.refreshed.connect(self._refresh_complete)
        self.refresh_worker.failed.connect(self._refresh_failed)
        self.refresh_worker.finished.connect(self._refresh_finished)
        self.refresh_worker.start()

    def _refresh_complete(self, refreshed):
        kept = {report.root: report for report in self.reports}
        for root, report in refreshed.items():
            if report is None:
                kept.pop(root, None)
            else:
                kept[root] = report
        self.reports = list(kept.values())
        self.render_reports()
        self.status.setText(
            f"{self._last_apply_summary} Refreshed {len(refreshed)} albums; "
            f"{len(self.reports)} problem albums remain"
        )

    def _refresh_failed(self, message):
        self.status.setText("Refresh failed; run a full scan")
        QMessageBox.warning(self, "Refresh failed", message + "\n\nRun a full scan to update the list.")

    def _refresh_finished(self):
        self.scan_button.setEnabled(True)
        self._set_album_actions_enabled(True)

    def closeEvent(self, event):
        if self.apply_worker and self.apply_worker.isRunning():
            event.ignore()
            self.status.setText("Finishing file moves before closing…")
            self.apply_worker.finished.connect(self.close)
            return
        if self.refresh_worker and self.refresh_worker.isRunning():
            self.refresh_worker.cancel.set()
            event.ignore()
            self.status.setText("Finishing refresh before closing…")
            self.refresh_worker.finished.connect(self.close)
            return
        if self.worker and self.worker.isRunning():
            self.cancel_scan()
            event.ignore()
            self.status.setText("Cancelling scan before closing…")
            self.worker.finished.connect(self.close)
        else:
            super().closeEvent(event)
