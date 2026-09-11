"""Synthetic click 8.1.7 API contract for offline benchmarking."""
PACKAGE = 'click'
VERSION = '8.1.7'

def parameter(*args, **kwargs):
    return {"api": 'parameter', "args": args, "kwargs": kwargs}
