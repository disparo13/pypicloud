"""
Inline replacements for pyramid_duh view predicates and request.param.

Provides:
  SubpathPredicate  — named wildcard/regex subpath matching, sets
                      request.named_subpaths on match.
  param()           — request method for typed parameter extraction.
  includeme()       — Pyramid config include that wires both up.

This replaces both config.include("pyramid_duh") and
config.include("pyramid_duh.auth") from the abandoned pyramid_duh package.
"""
import fnmatch
import re

from pyramid.httpexceptions import HTTPBadRequest
from pyramid.settings import asbool

_MISSING = object()


# ---------------------------------------------------------------------------
# request.param helper (was pyramid_duh.params)
# ---------------------------------------------------------------------------

def param(request, name, default=_MISSING, type=None, validate=None):
    """
    Access a request parameter with optional type coercion.

    For ``application/json`` bodies uses ``request.json_body``; otherwise uses
    ``request.params``.  Raises ``HTTPBadRequest`` if the param is required but
    absent.
    """
    content_type = request.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type == "application/json":
        params = request.json_body
    else:
        params = request.params

    try:
        val = params[name]
    except KeyError:
        if default is _MISSING:
            raise HTTPBadRequest("Missing argument '%s'" % name)
        return default

    if type is bool:
        return asbool(val)
    if type is not None:
        return type(val)
    return val


# ---------------------------------------------------------------------------
# SubpathPredicate (was pyramid_duh.view.SubpathPredicate)
# ---------------------------------------------------------------------------

def _match(pattern, path, flags):
    """
    Match *path* against *pattern* with optional *flags*.

    Flags
    -----
    r   Use PCRE (default is glob/fnmatch).
    i   Case-insensitive (requires r).
    ?   Path is optional — return True when path is None.
    """
    if path is None:
        return "?" in flags
    if "r" in flags:
        re_flags = re.I if "i" in flags else 0
        return re.match("^%s$" % pattern, path, re_flags)
    return fnmatch.fnmatchcase(path, pattern)


class SubpathPredicate:
    """
    Custom Pyramid view predicate that matches named subpath patterns and
    populates ``request.named_subpaths``.

    Each element of the *paths* tuple may take one of three forms::

        'literal'            – exact match, no capture
        'name/glob'          – wildcard capture as named_subpaths['name']
        'name/pattern/flags' – regex capture as named_subpaths['name']

    A leading slash with no name means flags-only::

        '/foo.*/r'           – regex with no named capture

    Examples used in pypicloud::

        subpath=("username/*",)
        subpath=("username/*", "approve")
        subpath=("package/*", "type/user|group/r", "name/*",
                 "permission/read|write/r")
    """

    def __init__(self, paths, config):
        if isinstance(paths, str):
            paths = (paths,)
        self.paths = paths
        self.config = config

    def text(self):
        return "subpath = %s" % (self.paths,)

    phash = text

    def __call__(self, context, request):
        named_subpaths = {}

        # More subpath segments than we expect → no match
        if len(request.subpath) > len(self.paths):
            return False

        for i, spec in enumerate(self.paths):
            pieces = spec.split("/", 2)
            if len(pieces) == 1:
                name, pattern, flags = None, pieces[0], ""
            elif len(pieces) == 2:
                name, pattern, flags = pieces[0], pieces[1], ""
            else:
                name, pattern, flags = pieces

            path = request.subpath[i] if i < len(request.subpath) else None
            result = _match(pattern, path, flags)
            if not result:
                return False

            if name and path is not None:
                named_subpaths[name] = path
            # Regex groups (e.g. named captures) are merged in too
            if hasattr(result, "groupdict"):
                named_subpaths.update(result.groupdict())

        request.named_subpaths = named_subpaths
        return True


# ---------------------------------------------------------------------------
# Pyramid config include
# ---------------------------------------------------------------------------

def includeme(config):
    """Register SubpathPredicate and request.param with Pyramid."""
    config.add_view_predicate("subpath", SubpathPredicate)
    config.add_request_method(param, name="param")
