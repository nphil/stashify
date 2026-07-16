"""In-Stash raw plugin entrypoint.

Stash launches this with the plugin input JSON on stdin (server_connection +
task args). It reads this plugin's settings out of the Stash configuration,
overlays task args, and hands off to core.run(). Logging/progress route to
Stash's native log module.

Note: this path runs the ML backends *inside the Stash container/host*, so
DeepMosaics/Real-ESRGAN and torch must be installed there. On unRAID (or any
Docker setup) prefer the standalone worker container instead — see worker.py.
"""

import sys
import json

import stashapi.log as slog
from stashapi.stashapp import StashInterface

import core

# Plugin id == yml filename stem; used to read settings back out of the config.
PLUGIN_ID = "stashify"


def resolve_config(stash, args):
    """DEFAULTS < plugin settings < task args (task args win)."""
    cfg = dict(core.DEFAULTS)
    try:
        result = stash.call_GQL("query Configuration { configuration { plugins } }")
        plugins = (result or {}).get("configuration", {}).get("plugins") or {}
        for key, value in (plugins.get(PLUGIN_ID) or {}).items():
            if value is not None and value != "":
                cfg[key] = value
    except Exception as exc:  # noqa: BLE001 - settings are best-effort
        slog.warning(f"Could not read plugin settings, using defaults: {exc}")
    for key, value in (args or {}).items():
        if value is not None:
            cfg[key] = value
    return cfg


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else "{}"
    payload = json.loads(raw or "{}")

    server_connection = payload.get("server_connection")
    if not server_connection:
        slog.error("No server_connection provided — run this as a Stash plugin task.")
        raise SystemExit(1)

    core.set_log(slog)  # route core logging/progress to Stash
    stash = StashInterface(server_connection)
    args = payload.get("args", {}) or {}
    cfg = resolve_config(stash, args)

    try:
        core.run(
            stash, cfg,
            mode=args.get("mode", "tagged"),
            scene_ids=args.get("scene_ids") or args.get("sceneIds"),
        )
    except ValueError as exc:  # config validation failed
        slog.error(f"{exc} Fix under Settings > Plugins > Stashify.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
