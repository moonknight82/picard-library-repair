"""Write a missing album-folder JPEG from Picard's front artwork after save."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QPainter


def _jpeg_bytes(data: bytes) -> bytes | None:
    image = QImage.fromData(data)
    if image.isNull():
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return data
    opaque = QImage(image.size(), QImage.Format.Format_RGB32)
    opaque.fill(Qt.GlobalColor.white)
    painter = QPainter(opaque)
    try:
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not opaque.save(buffer, "JPEG", 90):
        return None
    return bytes(buffer.data())


def ensure_folder_cover(file) -> Path | None:
    """Create cover.jpg beside a successfully saved file, if front art exists.

    Picard's normal external-image saver uses the new image list. Matched
    releases with no provider art can still have a usable original embedded
    cover; this fills that specific gap without changing audio or tags.
    """
    target = Path(file.filename).parent / "cover.jpg"
    if target.exists():
        return None
    for metadata in (file.metadata, file.orig_metadata):
        front = metadata.images.get_front_image()
        if front is None or not front.data:
            continue
        jpeg = _jpeg_bytes(front.data)
        if jpeg is None:
            continue
        created = False
        try:
            with target.open("xb") as stream:
                created = True
                stream.write(jpeg)
        except FileExistsError:
            return None
        except OSError:
            if created:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise
        return target
    return None
