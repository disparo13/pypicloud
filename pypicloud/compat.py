"""
Inline replacements for pyramid_duh.argify, pyramid_duh.addslash, and
pyramid_duh.settings.asdict.

These were provided by pyramid_duh (abandoned 2014) which used
inspect.getargspec, removed in Python 3.11.  This module provides equivalent
functionality using inspect.signature (Python 3.3+).
"""
import functools
import inspect

from pyramid.httpexceptions import HTTPBadRequest, HTTPFound
from pyramid.settings import asbool


def _is_request(obj):
    """Duck-type check for Pyramid request objects."""
    return hasattr(obj, "params") and hasattr(obj, "method") and hasattr(obj, "matchdict")


def _params_from_request(request):
    """Return (params_dict, from_form) for the request.

    Uses request.json_body for application/json; request.params otherwise.
    """
    content_type = request.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type == "application/json":
        return dict(request.json_body), False
    return dict(request.params), True


def argify(fn):
    """
    View decorator: auto-injects request parameters as kwargs matching the
    function signature.  Replacement for pyramid_duh.argify.

    Rules
    -----
    * ``self``, ``request``, ``context`` are resolved from the call context,
      not from request params.
    * Required parameters (no default) raise HTTPBadRequest if absent.
    * Optional parameters (have a default) fall back to their default.
    * If the default is a ``bool``, the raw string value is coerced with
      ``asbool`` when the source is form-encoded data.
    * Calls that don't look like Pyramid dispatch (e.g. unit tests passing
      explicit args) are passed through untouched.
    """
    sig = inspect.signature(fn)
    param_specs = list(sig.parameters.items())

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        self_obj = None
        context = None
        request = None

        if args and hasattr(args[0], "request") and not _is_request(args[0]):
            # Class-based view method: pyramid calls as (self,)
            self_obj = args[0]
            request = self_obj.request
            context = getattr(self_obj, "context", None)
            if len(args) != 1 or kwargs:
                # Extra args → likely a unit test; pass through
                return fn(*args, **kwargs)
        elif len(args) == 2 and _is_request(args[1]):
            context, request = args[0], args[1]
            if kwargs:
                return fn(*args, **kwargs)
        elif len(args) == 1 and _is_request(args[0]):
            request = args[0]
            if kwargs:
                return fn(*args, **kwargs)
        else:
            # Doesn't look like Pyramid dispatch → pass through (unit test)
            return fn(*args, **kwargs)

        raw, from_form = _params_from_request(request)

        bound = {}
        for name, spec in param_specs:
            if name == "self":
                bound[name] = self_obj
            elif name == "request":
                bound[name] = request
            elif name == "context":
                bound[name] = context
            elif name in raw:
                val = raw[name]
                # Coerce bool when the default signals bool type and the
                # value arrived as a form string rather than JSON
                if (
                    from_form
                    and spec.default is not inspect.Parameter.empty
                    and isinstance(spec.default, bool)
                ):
                    val = asbool(val)
                bound[name] = val
            elif spec.default is not inspect.Parameter.empty:
                bound[name] = spec.default
            else:
                raise HTTPBadRequest("Missing argument '%s'" % name)

        return fn(**bound)

    _wrapper.__argify__ = True
    return _wrapper


def addslash(fn):
    """
    View decorator: redirect to the same URL with a trailing slash appended
    when the current path lacks one.  Replacement for pyramid_duh.addslash.
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if len(args) == 2 and _is_request(args[1]):
            request = args[1]
        elif len(args) == 1 and _is_request(args[0]):
            request = args[0]
        else:
            return fn(*args, **kwargs)

        if not request.path_url.endswith("/"):
            new_url = request.path_url + "/"
            if request.query_string:
                new_url += "?" + request.query_string
            return HTTPFound(location=new_url)

        return fn(*args, **kwargs)

    return _wrapper


def asdict(setting, value_type=lambda x: x):
    """
    Parse a multi-line INI setting into a dict.

    Each non-blank line must be of the form ``key = value``.  Replacement for
    pyramid_duh.settings.asdict.

    Parameters
    ----------
    setting : str or dict or None
    value_type : callable, optional
        Applied to each value string.

    Returns
    -------
    dict
    """
    if setting is None:
        return {}
    if isinstance(setting, dict):
        return setting
    result = {}
    for line in (ln.strip() for ln in setting.splitlines()):
        if not line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value_type(value.strip())
    return result
