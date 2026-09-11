"""Synthetic packaging 24.0 API contract for offline benchmarking."""
PACKAGE = 'packaging'
VERSION = '24.0'

def Version(*args, **kwargs):
    return {"api": 'Version', "args": args, "kwargs": kwargs}
