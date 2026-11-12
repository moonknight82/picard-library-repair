"""Read-only album audit and reviewed, no-overwrite disc consolidation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import ctypes
import errno
import hashlib
import os
import re
import shutil
import sys
import threading
import unicodedata
from typing import Callable

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TPOS, TRCK
from mutagen.mp4 import MP4


AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus",
    ".wv", ".ape", ".wma", ".mp2", ".wav", ".aiff", ".aif",
    ".tak", ".mpc", ".dsf", ".dff",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MOVABLE_EXTRA_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
DISC_DIR = re.compile(r"^(?:cd|disc|disk)[\s._-]*(\d{1,2})$", re.IGNORECASE)
NUMBER_PREFIX = re.compile(r"^(\d{1,3})[\s._-]+(.+)$")
FRONT_NAMES = {"cover", "folder", "front", "albumart", "album art"}
NON_FRONT_NAMES = {"back", "rear", "spine", "booklet", "inlay", "disc", "label"}


@dataclass
class TrackInfo:
    path: Path
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    title: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    front_art: bool = False
    unreadable: str = ""
    issues: list[str] = field(default_factory=list)


@dataclass
class AlbumReport:
    root: Path
    tracks: list[TrackInfo] = field(default_factory=list)
    disc_dirs: list[Path] = field(default_factory=list)
    folder_cover: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def problem(self) -> bool:
        return bool(self.issues or any(track.issues for track in self.tracks))


@dataclass
class ScanResult:
    albums: list[AlbumReport]
    files_seen: int
    cancelled: bool = False


@dataclass
class MoveAction:
    source: Path
    destination: Path
    size: int
    mtime_ns: int
    tag_updates: dict[str, int] = field(default_factory=dict)
    sidecar: Path | None = None
    sidecar_size: int | None = None
    sidecar_mtime_ns: int | None = None


@dataclass
class MovePlan:
    album: Path
    actions: list[MoveAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.actions) and not self.conflicts


@dataclass
class ApplyResult:
    moved: list[Path] = field(default_factory=list)
    removed_sidecars: list[Path] = field(default_factory=list)
    removed_dirs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def disc_number_for_dir(path: Path) -> int | None:
    match = DISC_DIR.fullmatch(path.name)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS and not path.name.startswith("._")


def _plain(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return _plain(value[0]) if value else ""
    if hasattr(value, "text"):
        return _plain(value.text)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip() if value is not None else ""


def _tag(tags: object, *names: str) -> str:
    if tags is None:
        return ""
    normalized_keys = {}
    for key in tags.keys():
        normalized = str(key).split(":", 1)[0].casefold().replace(" ", "").replace("_", "")
        normalized_keys.setdefault(normalized, key)
    for name in names:
        normalized = name.casefold().replace(" ", "").replace("_", "")
        if normalized in normalized_keys:
            return _plain(tags[normalized_keys[normalized]])
    return ""


def _number(value: str) -> int | None:
    match = re.match(r"^\s*(\d+)(?:\s*/\s*\d+)?\s*$", value)
    number = int(match.group(1)) if match else 0
    return number or None


def _mp4_number(tags: object, key: str) -> int | None:
    try:
        value = tags.get(key)
        return int(value[0][0]) or None if value else None
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _front_art(audio: object) -> bool:
    pictures = getattr(audio, "pictures", None)
    if pictures and any(getattr(pic, "type", None) == 3 for pic in pictures):
        return True
    tags = getattr(audio, "tags", None)
    if tags is None:
        return False
    if isinstance(audio, MP4):
        return bool(tags.get("covr"))
    for key in tags.keys():
        if str(key).startswith("APIC"):
            value = tags[key]
            if getattr(value, "type", None) == 3 and bool(getattr(value, "data", None)):
                return True
        if str(key).casefold() == "metadata_block_picture":
            return True
    return False


def read_track(path: Path) -> TrackInfo:
    info = TrackInfo(path=path)
    try:
        audio = MutagenFile(path)
        if audio is None:
            raise ValueError("unsupported or invalid audio file")
        tags = getattr(audio, "tags", None)
        info.artist = _tag(tags, "artist", "TPE1", "©ART")
        info.album_artist = _tag(tags, "albumartist", "album artist", "TPE2", "aART")
        info.album = _tag(tags, "album", "TALB", "©alb")
        info.title = _tag(tags, "title", "TIT2", "©nam")
        info.track_number = _mp4_number(tags, "trkn") if isinstance(audio, MP4) else _number(_tag(tags, "tracknumber", "track", "TRCK"))
        info.disc_number = _mp4_number(tags, "disk") if isinstance(audio, MP4) else _number(_tag(tags, "discnumber", "disc", "TPOS"))
        info.front_art = _front_art(audio)
        for label, value in (("artist", info.artist), ("album artist", info.album_artist), ("album", info.album), ("title", info.title)):
            if not value:
                info.issues.append(f"Missing {label}")
        if info.track_number is None:
            info.issues.append("Missing numeric track number")
        if not info.front_art:
            info.issues.append("Missing embedded front cover")
    except Exception as exc:
        info.unreadable = f"{type(exc).__name__}: {exc}"
        info.issues.append(f"Unreadable audio: {info.unreadable}")
    return info


def _valid_image(path: Path) -> bool:
    if path.suffix.lower() not in IMAGE_EXTENSIONS or path.is_symlink():
        return False
    try:
        with path.open("rb") as stream:
            head = stream.read(12)
        return head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    except OSError:
        return False


def _folder_cover(root: Path) -> bool:
    try:
        images = [p for p in root.iterdir() if p.is_file() and not p.name.startswith("._") and _valid_image(p)]
    except OSError:
        return False
    return any(p.stem.casefold() in FRONT_NAMES for p in images) or (
        len(images) == 1 and images[0].stem.casefold() not in NON_FRONT_NAMES
    )


def scan_library(root: Path, cancel: threading.Event | None = None, progress: Callable[[int, Path], None] | None = None) -> ScanResult:
    """Return problem albums only. Does not write files or contact MusicBrainz."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    albums: dict[Path, AlbumReport] = {}
    files_seen = 0
    cancelled = False
    for directory, dirs, files in os.walk(root, followlinks=False):
        if cancel and cancel.is_set():
            cancelled = True
            break
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink() and not d.startswith("._")]
        parent = Path(directory)
        for name in files:
            if cancel and cancel.is_set():
                cancelled = True
                break
            path = parent / name
            if not is_audio(path) or path.is_symlink():
                continue
            album_root = parent.parent if disc_number_for_dir(parent) else parent
            report = albums.setdefault(album_root, AlbumReport(root=album_root))
            report.tracks.append(read_track(path))
            files_seen += 1
            if progress and files_seen % 25 == 0:
                progress(files_seen, album_root)
        if cancelled:
            break
    if cancelled:
        return ScanResult([], files_seen, True)
    problems = []
    for report in albums.values():
        report.disc_dirs = sorted({track.path.parent for track in report.tracks if disc_number_for_dir(track.path.parent)})
        report.folder_cover = _folder_cover(report.root)
        if not report.folder_cover:
            report.issues.append("Missing album-folder front cover")
        if report.disc_dirs:
            report.issues.append(f"Disc subfolders: {', '.join(p.name for p in report.disc_dirs)}")
        known_discs = {track.disc_number for track in report.tracks if track.disc_number is not None}
        if report.disc_dirs or any(number > 1 for number in known_discs):
            for track in report.tracks:
                if track.disc_number is None:
                    track.issues.append("Missing numeric disc number")
        if report.problem:
            problems.append(report)
    return ScanResult(sorted(problems, key=lambda album: str(album.root).casefold()), files_seen)


def _safe_name(text: str) -> str:
    name = re.sub(r"[\x00-\x1f/:\\]", "-", text).strip(" .")
    return name[:180].rstrip(" .") or "Untitled"


def _path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", str(path)).casefold()


def plan_consolidation(report: AlbumReport) -> MovePlan:
    plan = MovePlan(album=report.root)
    if not report.disc_dirs:
        plan.conflicts.append("No disc subfolders in this album")
        return plan
    destinations: set[str] = set()
    positions = {
        (track.disc_number or 1, track.track_number)
        for track in report.tracks
        if track.path.parent == report.root and track.track_number is not None
    }
    for disc_dir in report.disc_dirs:
        disc = disc_number_for_dir(disc_dir)
        tracks = {track.path: track for track in report.tracks if track.path.parent == disc_dir}
        for source in sorted(disc_dir.iterdir(), key=lambda p: p.name.casefold()):
            if source.is_symlink() or source.is_dir():
                plan.retained.append(f"Leave: {source}")
                continue
            if source.name.startswith("._"):
                continue
            if source in tracks:
                track = tracks[source]
                if track.unreadable:
                    plan.conflicts.append(f"Unreadable: {source}")
                    continue
                if track.disc_number is not None and track.disc_number != disc:
                    plan.conflicts.append(f"Disc tag disagrees with {disc_dir.name}: {source}")
                    continue
                match = NUMBER_PREFIX.match(source.stem)
                number = track.track_number or (int(match.group(1)) if match else None)
                if not number:
                    plan.conflicts.append(f"Track number is ambiguous: {source}")
                    continue
                if match and track.track_number is not None and int(match.group(1)) != track.track_number:
                    plan.conflicts.append(f"Filename and track tag disagree: {source}")
                    continue
                if (disc, number) in positions:
                    plan.conflicts.append(f"Duplicate disc/track position {disc}.{number:02d}: {source}")
                    continue
                positions.add((disc, number))
                title = track.title or (match.group(2) if match else source.stem)
                filename = f"{disc}.{number:02d} {_safe_name(title)}{source.suffix}"
                updates = {}
                if track.disc_number is None:
                    updates["discnumber"] = disc
                if track.track_number is None:
                    updates["tracknumber"] = number
            elif source.suffix.lower() in MOVABLE_EXTRA_EXTENSIONS:
                filename = source.name
                updates = {}
            else:
                plan.retained.append(f"Leave: {source}")
                continue
            destination = report.root / filename
            key = _path_key(destination)
            if key in destinations or destination.exists() or any(_path_key(p) == key for p in report.root.iterdir()):
                plan.conflicts.append(f"Destination collision: {destination}")
                continue
            destinations.add(key)
            stat = source.stat()
            sidecar = source.with_name("._" + source.name)
            sidecar_stat = sidecar.stat() if sidecar.is_file() and not sidecar.is_symlink() else None
            plan.actions.append(MoveAction(
                source, destination, stat.st_size, stat.st_mtime_ns, updates,
                sidecar if sidecar_stat else None,
                sidecar_stat.st_size if sidecar_stat else None,
                sidecar_stat.st_mtime_ns if sidecar_stat else None,
            ))
    return plan


def _write_numbers(path: Path, updates: dict[str, int]) -> None:
    if not updates:
        return
    audio = MutagenFile(path)
    if audio is None:
        raise ValueError(f"Cannot write tags: {path}")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(audio, MP4):
        if "discnumber" in updates:
            total = tags.get("disk", [(0, 0)])[0][1]
            tags["disk"] = [(updates["discnumber"], total)]
        if "tracknumber" in updates:
            total = tags.get("trkn", [(0, 0)])[0][1]
            tags["trkn"] = [(updates["tracknumber"], total)]
    elif isinstance(tags, ID3):
        if "discnumber" in updates:
            tags.add(TPOS(encoding=3, text=str(updates["discnumber"])))
        if "tracknumber" in updates:
            tags.add(TRCK(encoding=3, text=str(updates["tracknumber"])))
    else:
        for key, value in updates.items():
            tags[key] = str(value)
    audio.save()
    reread = read_track(path)
    for key, value in updates.items():
        if getattr(reread, "disc_number" if key == "discnumber" else "track_number") != value:
            raise ValueError(f"Tag verification failed for {path}: {key}")


def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _rename_exclusive(source: Path, destination: Path) -> bool:
    """Rename without replacement on macOS; return False if unavailable.

    A regular os.rename can overwrite the destination, so unsupported volumes
    must use the exclusive-create and verified-copy path instead.
    """
    if sys.platform != "darwin":
        return False
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renamex_np", None)
    if rename is None:
        return False
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) == 0:  # RENAME_EXCL
        return True
    code = ctypes.get_errno()
    if code in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL, errno.EXDEV}:
        return False
    raise OSError(code, os.strerror(code), str(destination))


def _copy_verified(action: MoveAction) -> None:
    """Preserve the source until its exclusive destination copy is verified."""
    created = False
    try:
        source_digest = hashlib.sha256()
        with action.source.open("rb") as incoming, action.destination.open("xb") as outgoing:
            created = True
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                source_digest.update(chunk)
                outgoing.write(chunk)
        if _sha256_file(action.destination) != source_digest.digest():
            raise IOError(f"Copy checksum mismatch: {action.destination}")
        current_stat = action.source.stat()
        if current_stat.st_size != action.size or current_stat.st_mtime_ns != action.mtime_ns:
            raise ValueError(f"Source changed during copy: {action.source}")
        shutil.copystat(action.source, action.destination)
        if action.destination.stat().st_size != action.size:
            raise IOError(f"Incomplete copy: {action.destination}")
        _write_numbers(action.destination, action.tag_updates)
        action.source.unlink()
    except Exception:
        if created and action.source.exists() and action.destination.exists():
            try:
                action.destination.unlink()
            except OSError:
                pass
        raise


def apply_consolidation(plan: MovePlan, progress: Callable[[int, int, Path], None] | None = None) -> ApplyResult:
    """Apply an approved plan. Each source is retained until its destination is complete."""
    result = ApplyResult()
    if not plan.ready:
        result.errors.append("Plan has conflicts or no moves")
        return result
    for action in plan.actions:
        try:
            if action.source.is_symlink():
                raise ValueError(f"Source became a symlink: {action.source}")
            stat = action.source.stat()
            if stat.st_size != action.size or stat.st_mtime_ns != action.mtime_ns:
                raise ValueError(f"Source changed since preview: {action.source}")
            if action.destination.exists():
                raise FileExistsError(f"Destination now exists: {action.destination}")
        except (OSError, ValueError) as exc:
            result.errors.append(str(exc))
    if result.errors:
        return result
    for action in plan.actions:
        renamed = False
        try:
            destination_sidecar = action.destination.with_name("._" + action.destination.name)
            destination_sidecar_existed = destination_sidecar.exists()
            current_stat = action.source.stat()
            if current_stat.st_size != action.size or current_stat.st_mtime_ns != action.mtime_ns:
                raise ValueError(f"Source changed since preview: {action.source}")
            # Files needing tag edits retain the verified-copy path so a tag
            # write failure cannot alter the only copy of the source.
            if not action.tag_updates:
                renamed = _rename_exclusive(action.source, action.destination)
            if not renamed:
                _copy_verified(action)
            result.moved.append(action.destination)
            if progress:
                progress(len(result.moved), len(plan.actions), action.destination)
            if action.sidecar and action.sidecar.is_file() and not action.sidecar.is_symlink():
                try:
                    sidecar_stat = action.sidecar.stat()
                    if sidecar_stat.st_size == action.sidecar_size and sidecar_stat.st_mtime_ns == action.sidecar_mtime_ns:
                        action.sidecar.unlink()
                        result.removed_sidecars.append(action.sidecar)
                    else:
                        result.errors.append(f"Sidecar changed since preview; retained {action.sidecar}")
                except OSError as exc:
                    result.errors.append(f"Could not remove sidecar {action.sidecar}: {exc}")
            # macOS can create a new AppleDouble sidecar on FAT32 while
            # copying timestamps or writing tags. Remove only one created
            # during this move, after the audio has been verified and moved.
            if not destination_sidecar_existed and destination_sidecar.is_file() and not destination_sidecar.is_symlink():
                try:
                    destination_sidecar.unlink()
                    result.removed_sidecars.append(destination_sidecar)
                except OSError as exc:
                    result.errors.append(f"Could not remove new sidecar {destination_sidecar}: {exc}")
        except Exception as exc:
            result.errors.append(f"Could not move {action.source}: {exc}")
            break
    for disc_dir in sorted({a.source.parent for a in plan.actions}):
        try:
            disc_dir.rmdir()
            result.removed_dirs.append(disc_dir)
        except OSError:
            pass
    return result
