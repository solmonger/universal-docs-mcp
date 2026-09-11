"""Synthetic urllib3 2.2.1 API contract for offline benchmarking."""
PACKAGE = 'urllib3'
VERSION = '2.2.1'

def timeout(*args, **kwargs):
    return {"api": 'timeout', "args": args, "kwargs": kwargs}
