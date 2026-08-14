"""Handheld performance profiles: per-profile TDP limits via ryzenadj.

Dependency-free and never raises: failures degrade to a logged no-op.
"""

from __future__ import annotations

import glob
import logging
import shutil
import subprocess

from parity_gamescope import is_gamescope_guest

LOGGER = logging.getLogger("openbox")


def effective_profile_name(game, profiles):
    """Resolve the launch-profile name a game uses, matching web_app.start_game.

    A per-game ``launch_profile`` wins when it names an existing profile, else the platform.
    """
    selected = str(game.get("launch_profile", "")).strip()
    if selected and selected in profiles:
        return selected
    return str(game.get("platform", "")).strip()


def _has_battery():
    """True when the host exposes a battery (handheld or laptop)."""
    return bool(glob.glob("/sys/class/power_supply/BAT*"))


def perf_should_apply(state, environ=None):
    """Decide whether limits apply: ``off``/``auto``/``always``; ``auto`` only on gamescope guests or battery power."""
    mode = str(state.get("settings", {}).get("apply_perf", "auto")).strip().casefold()
    if mode == "off":
        return False
    if mode == "always":
        return True
    return bool(is_gamescope_guest(environ=environ) or _has_battery())


def _run_ryzenadj(extra_args, which=shutil.which, run=subprocess.run):
    """Run ryzenadj with the given extra args; return (ok, message)."""
    binary = which("ryzenadj")
    if not binary:
        return False, "ryzenadj not installed"
    try:
        proc = run([binary, *extra_args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip() or "ryzenadj exited nonzero"
    return True, ""


def _tdp_args(tdp_w):
    """Build ryzenadj args for a TDP limit in watts (mW internally)."""
    mw = int(round(float(tdp_w) * 1000))
    return ["-stapm-limit=" + str(mw)], mw


def apply_perf_profile(profile_name, state, environ=None):
    """Apply the profile's TDP limit; never raises. Returns a result dict."""
    result = {"profile": profile_name, "applied": False, "reason": "", "tdp_w": 0}
    if not perf_should_apply(state, environ):
        mode = str(state.get("settings", {}).get("apply_perf", "auto")).strip().casefold()
        result["reason"] = "disabled" if mode == "off" else "auto-skipped"
        return result
    perf = state.get("perf_profiles", {}).get(profile_name)
    if not perf or not perf.get("enabled") or not perf.get("tdp_w"):
        result["reason"] = "no-profile" if not perf else ("disabled" if not perf.get("enabled") else "bad-tdp")
        return result
    try:
        args, mw = _tdp_args(perf["tdp_w"])
    except (TypeError, ValueError):
        result["reason"] = "bad-tdp"
        return result
    ok, message = _run_ryzenadj(args)
    result["applied"] = ok
    result["reason"] = message or "ok"
    result["tdp_w"] = float(perf["tdp_w"])
    if not ok:
        LOGGER.warning("apply_perf: %s (%s W): %s", profile_name, perf["tdp_w"], message)
    return result


def restore_perf_profile(profile_name, state):
    """Restore the saved TDP limit after a session; only when limits were eligible on this host, never raises."""
    result = {"profile": profile_name, "applied": False, "reason": "", "tdp_w": 0}
    if not perf_should_apply(state):
        result["reason"] = "auto-skipped"
        return result
    perf = state.get("perf_profiles", {}).get(profile_name)
    if not perf or not perf.get("restore_tdp_w"):
        result["reason"] = "no-restore"
        return result
    try:
        args, mw = _tdp_args(perf["restore_tdp_w"])
    except (TypeError, ValueError):
        result["reason"] = "bad-tdp"
        return result
    ok, message = _run_ryzenadj(args)
    result["applied"] = ok
    result["reason"] = message or "ok"
    result["tdp_w"] = float(perf["restore_tdp_w"])
    if not ok:
        LOGGER.warning("restore_perf: %s (%s W): %s", profile_name, perf["restore_tdp_w"], message)
    return result
