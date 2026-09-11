"""Synthetic sqlalchemy 2.0.29 API contract for offline benchmarking."""
PACKAGE = 'sqlalchemy'
VERSION = '2.0.29'

def execute(*args, **kwargs):
    return {"api": 'execute', "args": args, "kwargs": kwargs}
