"""Synthetic pytest 8.2.0 API contract for offline benchmarking."""
PACKAGE = 'pytest'
VERSION = '8.2.0'

def raises(*args, **kwargs):
    return {"api": 'raises', "args": args, "kwargs": kwargs}
