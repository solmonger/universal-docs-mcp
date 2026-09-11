"""Synthetic pydantic 2.6.1 API contract for offline benchmarking."""
PACKAGE = 'pydantic'
VERSION = '2.6.1'

def model_dump(*args, **kwargs):
    return {"api": 'model_dump', "args": args, "kwargs": kwargs}
