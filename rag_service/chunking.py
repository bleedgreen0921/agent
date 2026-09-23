from dataclasses import dataclass
from functools import lru_cache
import os

from rag_service.parse import Block, ParseFailure


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    heading_path: tuple[str, ...]
    locator: dict
    table_html: str | None = None


@lru_cache(maxsize=1)
def tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(os.environ.get("RAG_TOKENIZER_PATH", "Qwen/Qwen3-Embedding-0.6B"), use_fast=True)


def embedding_input(title: str, chunk: ChunkDraft) -> str:
    heading = " / ".join(chunk.heading_path)
    return f"Title: {title}\nHeading: {heading}\nContent: {chunk.content}"


def merge_locator(first: dict, last: dict) -> dict:
    kind = first["kind"]
    if kind != last["kind"]:
        raise ValueError("mixed document formats")
    if kind == "pdf":
        return {"kind": "pdf", "page_start": first.get("page_start"), "page_end": last.get("page_end")}
    if kind == "docx":
        return {"kind": "docx", "heading_path": first.get("heading_path", []), "block_start": first.get("block_start"), "block_end": last.get("block_end")}
    return {"kind": kind, "line_start": first.get("line_start"), "line_end": last.get("line_end")}


def make_chunks(blocks: list[Block], title: str, tok=None, target: int = 512, overlap: int = 128) -> list[ChunkDraft]:
    if not 0 <= overlap < target:
        raise ValueError("invalid chunk size")
    tok = tok or tokenizer()
    chunks: list[ChunkDraft] = []
    group: list[Block] = []

    def flush() -> None:
        if not group:
            return
        content = "\n".join(b.text for b in group)
        encoded = tok(content, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded["offset_mapping"]
        if not offsets:
            group.clear()
            return
        spans = []
        position = 0
        for block in group:
            spans.append((position, position + len(block.text), block))
            position += len(block.text) + 1
        start = 0
        while start < len(offsets):
            block_start = next((b for left, right, b in spans if left <= offsets[start][0] < right), group[0])
            prefix = f"Title: {title}\nHeading: {' / '.join(block_start.heading_path)}\nContent: "
            capacity = target - len(tok.encode(prefix, add_special_tokens=False))
            if capacity <= 0:
                raise ParseFailure("EMBEDDING_INPUT_TOO_LONG")
            end = min(len(offsets), start + capacity)
            char_start, char_end = offsets[start][0], offsets[end - 1][1]
            text = content[char_start:char_end].strip()
            first = next((b for left, right, b in spans if left <= char_start < right), group[0])
            last = next((b for left, right, b in reversed(spans) if left < char_end <= right), group[-1])
            if text:
                chunks.append(ChunkDraft(text, first.heading_path, merge_locator(first.locator, last.locator)))
            if end == len(offsets):
                break
            start = end - min(overlap, capacity - 1)
        group.clear()

    for block in blocks:
        if block.kind == "table":
            flush()
            chunks.append(ChunkDraft(block.text, block.heading_path, block.locator, block.table_html))
        else:
            if group and group[-1].heading_path != block.heading_path:
                flush()
            group.append(block)
    flush()
    if not chunks:
        raise ParseFailure("EMPTY_CONTENT")
    return chunks
