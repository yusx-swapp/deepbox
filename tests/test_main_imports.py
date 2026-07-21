"""Guard: server.app.main must have `asyncio` imported.

Regression test for the latent NameError shipped in 47d06da where the output
hot path called ``asyncio.to_thread`` without importing ``asyncio``; the
connector connected, sent hello, then the first durable NEW output frame
crashed the ws_devbox handler ("no close frame received"). The bug survived a
deploy because /api/ready never drives an output frame. This asserts the name
is bound wherever ``asyncio.`` is referenced in the module source.
"""
import server.app.main as main


def test_main_has_asyncio_bound():
    assert hasattr(main, "asyncio"), "server.app.main must import asyncio"


def test_asyncio_references_are_importable():
    src = open(main.__file__, "rb").read().decode("utf-8")
    if "asyncio." in src:
        assert hasattr(main, "asyncio")
