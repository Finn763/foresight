# sources.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class Document:
    source: str
    url: str
    title: str
    content: str
    published_at: datetime | None
    fetched_at: datetime | None
    id: int | None = field(default=None, init=False)  # 入库后由 retrieve_and_store 填充


class BaseSource(Protocol):
    name: str

    def fetch(self, search_term: str) -> list[Document]: ...
