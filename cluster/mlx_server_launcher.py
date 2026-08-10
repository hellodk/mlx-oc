#!/usr/bin/env python3
"""
mlx_server_launcher.py
======================
Entry point for the distributed mlx_lm.server that makes the ring mesh stable.

mlx_lm.server calls mx.distributed.init() several times during startup
(TimeBudget, ModelProvider, ResponseGenerator, run). Each call makes ring.cpp
construct a NEW RingGroup that rebinds the same TCP ports and opens a fresh
set of peer connections; the groups used only to read size()/rank() are
dropped immediately and their destructors close their sockets, so the peer's
surviving group sees EPIPE / mismatched pairings. Startup fails intermittently
with EADDRINUSE, ETIMEDOUT or "Too many send/recv errors" as a result.
Standalone scripts that call init() once (ringtest.py / loadtest.py) never hit
this.

This launcher wraps mx.distributed.init() to return the first group it creates,
so exactly one RingGroup exists per rank, then hands off to mlx_lm.server.main()
with argv unchanged.

Usage (driven by mlx.launch from start_server.sh; the same absolute path must
exist on every rank):
  <venv>/bin/python <repo>/cluster/mlx_server_launcher.py <mlx_lm.server args...>
"""

import sys

import mlx.core as mx

_ORIG_INIT = mx.distributed.init
_GROUP_CACHE = {}


def _cached_init(*args, **kwargs):
    key = (args, tuple(sorted(kwargs.items())))
    if key not in _GROUP_CACHE:
        _GROUP_CACHE[key] = _ORIG_INIT(*args, **kwargs)
    return _GROUP_CACHE[key]


mx.distributed.init = _cached_init

from mlx_lm import server  # noqa: E402

if __name__ == "__main__":
    server.main()
