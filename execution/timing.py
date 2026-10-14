"""Monotonic, request-local spans. Parallel durations are not additive."""
from contextvars import ContextVar
from contextlib import contextmanager
from time import monotonic

collector=ContextVar('retrieval_timing',default=None)
stage_name=ContextVar('retrieval_stage',default=None)


@contextmanager
def capture():
    value={'version':2,'started':monotonic(),'spans':[]}
    key=collector.set(value)
    try: yield value
    finally: collector.reset(key)


@contextmanager
def span(name,category='stage'):
    value=collector.get(); start=monotonic(); parent=stage_name.get()
    key=stage_name.set(name) if category=='stage' else None
    error=None
    try: yield
    except BaseException as exc:
        error=type(exc).__name__;raise
    finally:
        if key is not None: stage_name.reset(key)
        if value is not None:
            value['spans'].append({'name':name,'category':category,'parent':parent,
                'offset_ms':round((start-value['started'])*1000,3),
                'duration_ms':round((monotonic()-start)*1000,3),'status':'failed' if error else 'success',
                **({'error_type':error} if error else {})})


def snapshot():
    value=collector.get()
    if value is None:return None
    return {'version':2,'total_ms':round((monotonic()-value['started'])*1000,3),'spans':list(value['spans'])}
