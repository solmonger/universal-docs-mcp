"""Synthetic httpx 0.27.0 API contract for offline benchmarking."""
PACKAGE = 'httpx'
VERSION = '0.27.0'

def client(*args, **kwargs):
    return {"api": 'client', "args": args, "kwargs": kwargs}
