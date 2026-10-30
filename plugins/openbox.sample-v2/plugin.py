"""Sample plugin for plugin API v2 (plan W9-08).

The v2 runner speaks JSON Lines to the host, but the plugin itself stays an ordinary Python
module: the host calls the hook with a payload object and reads the object it returns. A v1
plugin keeps working unchanged; see docs/plugin-api-v2.md for the differences.
"""


def after_session(payload):
    """Mark the payload so a test or the host can see the hook ran."""
    result = dict(payload or {})
    result["sample_v2"] = "after_session"
    return result


def command(payload):
    """Answer the palette command with a short confirmation."""
    result = dict(payload or {})
    result["sample_v2"] = "command"
    return result
