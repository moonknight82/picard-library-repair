"""Load audited folders into distinct Picard clusters without auto matching."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from picard.cluster import Cluster
from picard.util import normpath, resolve_fs_path

from .core import AlbumReport


@contextmanager
def _manual_load_callbacks(tagger):
    """Mark only this load's callbacks as manual, leaving global options alone.

    Picard 3 normally auto-links files with MusicBrainz IDs and can analyze
    other newly loaded files. Its _file_loaded callback skips both during a
    session restore. Capture a wrapper for this batch's async callbacks and
    restore the original method immediately after dispatch.
    """
    original = tagger._file_loaded

    def manual_loaded(*args, **kwargs):
        restoring = tagger._restoring_session
        tagger._restoring_session = True
        try:
            return original(*args, **kwargs)
        finally:
            tagger._restoring_session = restoring

    tagger._file_loaded = manual_loaded
    try:
        yield
    finally:
        tagger._file_loaded = original


def _folder_cluster(tagger, folder: Path) -> Cluster:
    """Find only a cluster made for this exact folder; never merge on title."""
    folder = folder.resolve()
    for cluster in tagger.clusters:
        if getattr(cluster, "_library_repair_folder", None) == folder:
            return cluster
        # A cluster restored from a Picard session loses plugin-only attrs.
        # Reuse it only when every member comes from this exact album folder.
        member_folders = {Path(file.filename).parent.resolve() for file in cluster.files}
        if member_folders == {folder}:
            cluster._library_repair_folder = folder
            return cluster
    cluster = Cluster(folder.name, folder.parent.name)
    cluster._library_repair_folder = folder
    tagger.clusters.append(cluster)
    tagger.cluster_added.emit(cluster)
    return cluster


def add_reports_as_clusters(tagger, reports: list[AlbumReport]) -> tuple[int, int, int]:
    """Return (albums, files, pending_skipped) loaded into folder clusters."""
    albums = files = pending_skipped = 0
    for report in reports:
        paths = [
            normpath(resolve_fs_path(track.path))
            for track in report.tracks if track.path.exists()
        ]
        if not paths:
            continue
        pending = [
            path for path in paths
            if (existing := tagger.files.get(path)) is not None
            and getattr(getattr(existing, "state", None), "name", None) == "PENDING"
        ]
        pending_skipped += len(pending)
        paths = [path for path in paths if path not in pending]
        if not paths:
            continue
        cluster = _folder_cluster(tagger, report.root)
        albums += 1
        new_paths = []
        for path in paths:
            existing = tagger.files.get(path)
            if existing is None:
                new_paths.append(path)
            else:
                existing.move(cluster)
                files += 1
        if new_paths:
            with _manual_load_callbacks(tagger):
                tagger.add_files(new_paths, target=cluster)
            files += sum(path in tagger.files for path in new_paths)
    return albums, files, pending_skipped
