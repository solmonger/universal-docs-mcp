"""Synthetic attrs 23.2.0 API contract for offline benchmarking."""
PACKAGE = 'attrs'
VERSION = '23.2.0'

def define(*args, **kwargs):
    return {"api": 'define', "args": args, "kwargs": kwargs}
