"""Dateien im Datenverzeichnis sicher schreiben."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Erst in eine Nachbardatei, dann umbenennen. Bricht der Strom mitten im
    Schreiben ab, bleibt die alte Datei ganz. Rechte 0600: nur der Benutzer des
    Containers darf lesen."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
