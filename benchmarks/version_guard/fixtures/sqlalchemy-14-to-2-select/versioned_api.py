"""Synthetic sqlalchemy 2.0.29 API contract for offline benchmarking."""
PACKAGE = 'sqlalchemy'
VERSION = '2.0.29'

def select(*args, **kwargs):
    return {"api": 'select', "args": args, "kwargs": kwargs}
