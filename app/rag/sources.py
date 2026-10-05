"""Where policy documents come from.

`LocalFolderSource` reads a folder (the git-ignored `data/private_policies`). A SharePoint, Google
Drive or S3 source would implement the same protocol using a service credential.
"""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourceDocument:
    uri: str  # stable identifier, e.g. "local://Revised Leave Policy - I2I.pdf"
    filename: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class DocumentSource(Protocol):
    def iter_documents(self) -> Iterator[SourceDocument]: ...


class LocalFolderSource:
    SUPPORTED_SUFFIXES = (".pdf",)

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def iter_documents(self) -> Iterator[SourceDocument]:
        if not self._folder.is_dir():
            return
        for path in sorted(self._folder.iterdir()):
            if path.is_file() and path.suffix.lower() in self.SUPPORTED_SUFFIXES:
                yield SourceDocument(
                    uri=f"local://{path.name}", filename=path.name, content=path.read_bytes()
                )
