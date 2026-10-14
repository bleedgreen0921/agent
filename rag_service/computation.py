"""Pure CPU entry points. No clients, persistence, or executor construction."""
import re
from pathlib import Path


def sparse_terms(content):
    import jieba
    words=re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9]+',content)
    return ' '.join(part.lower() for word in words for part in jieba.cut(word) if part.strip())


def vector_literal(values):
    return '['+','.join(str(value) for value in values)+']'


def prepare_document(path,media_type,title,max_chunks):
    from rag_service.parse import parse_file, ParseFailure
    from rag_service.chunking import make_chunks, embedding_input
    blocks,warnings=parse_file(Path(path),media_type,local_models=True)
    drafts=make_chunks(blocks,title)
    if len(drafts)>max_chunks: raise ParseFailure('TOO_MANY_CHUNKS')
    return ([{'content':d.content,'heading_path':list(d.heading_path),'locator':d.locator,
              'table_html':d.table_html,'search_text':sparse_terms(d.content),
              'embedding_input':embedding_input(title,d)} for d in drafts],warnings)
