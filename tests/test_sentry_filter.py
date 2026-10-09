import types

from cabotage.server import _sentry_before_send


def _make_traceback(filename):
    """Create a minimal traceback-like object with a single frame."""
    frame = types.SimpleNamespace(
        f_code=types.SimpleNamespace(co_filename=filename),
    )
    return types.SimpleNamespace(tb_next=None, tb_frame=frame)


def _make_nested_traceback(filenames):
    """Create a chained traceback from a list of filenames (outermost first)."""
    tb = None
    for filename in reversed(filenames):
        frame = types.SimpleNamespace(
            f_code=types.SimpleNamespace(co_filename=filename),
        )
        tb = types.SimpleNamespace(tb_next=tb, tb_frame=frame)
    return tb


class TestSentryBeforeSend:
    def test_filters_stop_iteration_from_simple_websocket(self):
        tb = _make_traceback(
            "/lib/python3.13/site-packages/simple_websocket/__init__.py"
        )
        hint = {"exc_info": (StopIteration, StopIteration(), tb)}
        assert _sentry_before_send({"event": "data"}, hint) is None

    def test_passes_stop_iteration_from_other_source(self):
        tb = _make_traceback("/app/cabotage/server/views.py")
        event = {"event": "data"}
        hint = {"exc_info": (StopIteration, StopIteration(), tb)}
        assert _sentry_before_send(event, hint) is event

    def test_passes_other_exceptions(self):
        tb = _make_traceback(
            "/lib/python3.13/site-packages/simple_websocket/__init__.py"
        )
        event = {"event": "data"}
        hint = {"exc_info": (ValueError, ValueError("oops"), tb)}
        assert _sentry_before_send(event, hint) is event

    def test_passes_events_without_exc_info(self):
        event = {"message": "hello"}
        assert _sentry_before_send(event, {}) is event

    def test_walks_to_innermost_frame(self):
        tb = _make_nested_traceback(
            [
                "/app/cabotage/server/__init__.py",
                "/lib/python3.13/site-packages/flask/app.py",
                "/lib/python3.13/site-packages/simple_websocket/__init__.py",
            ]
        )
        hint = {"exc_info": (StopIteration, StopIteration(), tb)}
        assert _sentry_before_send({"event": "data"}, hint) is None

    def test_does_not_filter_when_innermost_is_not_websocket(self):
        tb = _make_nested_traceback(
            [
                "/lib/python3.13/site-packages/simple_websocket/__init__.py",
                "/app/cabotage/server/views.py",
            ]
        )
        event = {"event": "data"}
        hint = {"exc_info": (StopIteration, StopIteration(), tb)}
        assert _sentry_before_send(event, hint) is event
