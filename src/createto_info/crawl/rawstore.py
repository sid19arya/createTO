"""Content-addressed storage for raw fetched bytes: data/raw/ab/abcdef….ext"""

from __future__ import annotations

import hashlib
from pathlib import Path

_EXT = {"page": ".html", "pdf": ".pdf", "sitemap": ".xml"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore:
    def __init__(self, root: Path):
        self.root = root

    def path_for(self, digest: str, kind: str) -> Path:
        return self.root / digest[:2] / f"{digest}{_EXT.get(kind, '.bin')}"

    def put(self, data: bytes, kind: str) -> tuple[str, Path]:
        """Write bytes if not already stored. Returns (sha256, absolute path)."""
        digest = sha256(data)
        path = self.path_for(digest, kind)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest, path

    def get(self, relpath: str) -> bytes:
        return (self.root / relpath).read_bytes()

    def relpath(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()
