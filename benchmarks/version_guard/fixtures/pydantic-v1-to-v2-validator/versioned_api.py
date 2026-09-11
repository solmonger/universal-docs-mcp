"""Synthetic pydantic 2.6.1 API contract for offline benchmarking."""
PACKAGE = 'pydantic'
VERSION = '2.6.1'

def field_validator(*args, **kwargs):
    return {"api": 'field_validator', "args": args, "kwargs": kwargs}
