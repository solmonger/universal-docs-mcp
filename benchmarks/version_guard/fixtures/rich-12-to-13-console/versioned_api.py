"""Synthetic rich 13.7.1 API contract for offline benchmarking."""
PACKAGE = 'rich'
VERSION = '13.7.1'

def console(*args, **kwargs):
    return {"api": 'console', "args": args, "kwargs": kwargs}
