"""Where the app looks for drawings.

The page used to list two places: its own uploads folder and the repository
root. The set itself lives in `Drawings/`, so once the uploads were cleared the
app showed one drawing while eleven sat next to it unseen — and every test that
found its drawing through the list started failing for the same reason.

Three places are searched by default, in this order:

  * the uploads folder — what people dropped into the page;
  * `Drawings/` — the project's drawing set;
  * the project root — where a drawing lands when someone saves it by hand.

Both lists can be moved by environment variable, which is what a server
deployment wants (drawings on a mounted volume, not inside the checkout) and
what lets a test give the page a folder of its own:

  * ``CIVIL_ESTIMATOR_UPLOAD_DIR``   — the uploads folder;
  * ``CIVIL_ESTIMATOR_DRAWING_DIRS`` — the other folders, separated by ``:``.

Only uploads are ever offered for deletion. A drawing somebody put in the
project folder is not this app's to remove.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

UPLOAD_ENV = "CIVIL_ESTIMATOR_UPLOAD_DIR"
DIRS_ENV = "CIVIL_ESTIMATOR_DRAWING_DIRS"

UPLOAD_DIR = Path("output/uploads")
DEFAULT_DIRS = (Path("Drawings"), Path("."))


def upload_dir() -> Path:
    """The folder uploads are written to. Read at call time, like the others."""
    return Path(os.environ.get(UPLOAD_ENV) or UPLOAD_DIR)


def drawing_dirs() -> list[Path]:
    """Every folder searched, uploads first."""
    raw = os.environ.get(DIRS_ENV)
    extra = ([Path(p) for p in raw.split(os.pathsep) if p.strip()]
             if raw else list(DEFAULT_DIRS))
    return [upload_dir()] + extra


def folder_label(folder: Path) -> str:
    """What to call a folder on screen."""
    if _same_dir(folder, upload_dir()):
        return "uploaded"
    if folder.resolve() == Path(".").resolve():
        return "project root"
    if folder.name.lower() == "drawings":
        return "Drawings folder"
    return f"{folder.name} folder"


@dataclass(frozen=True)
class Drawing:
    path: Path
    folder: str             # human label for where it is
    deletable: bool         # only uploads
    size_bytes: int
    modified_ns: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_mb(self) -> float:
        return self.size_bytes / 1024 / 1024


def find(dirs: list[Path] | None = None) -> list[Drawing]:
    """Every drawing, each file once, in folder order then by name.

    De-duplicated by the file itself (device and inode) rather than by path
    text. On macOS `Drawings` and `drawings` are the same folder spelled two
    ways, and a symlink is the same file under another name; either would
    otherwise list every drawing twice.
    """
    folders = dirs if dirs is not None else drawing_dirs()
    uploads = upload_dir()
    found: list[Drawing] = []
    seen: set[tuple[int, int]] = set()
    for folder in folders:
        if not folder.is_dir():
            continue
        label = folder_label(folder)
        deletable = _same_dir(folder, uploads)
        for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower()):
            if pdf.name.startswith(".") or not pdf.is_file():
                continue
            st = pdf.stat()
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            found.append(Drawing(path=pdf, folder=label, deletable=deletable,
                                 size_bytes=st.st_size, modified_ns=st.st_mtime_ns))
    return found


def labels(drawings: list[Drawing]) -> dict[Path, str]:
    """A display name per drawing, with its folder added only where needed.

    The same file name can sit in two folders as two different files. Two rows
    with an identical label is a trap, so the folder is folded in exactly when
    the names collide — and left out otherwise, where it would only be noise.
    """
    counts: dict[str, int] = {}
    for d in drawings:
        counts[d.name] = counts.get(d.name, 0) + 1
    return {d.path: (d.name if counts[d.name] == 1 else f"{d.name}  ·  {d.folder}")
            for d in drawings}


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.samefile(b)
    except OSError:
        return False
