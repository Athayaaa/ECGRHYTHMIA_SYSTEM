"""Inject firmware source/build metadata into every PlatformIO build."""

from datetime import datetime, timezone
import subprocess

Import("env")  # type: ignore[name-defined]  # supplied by PlatformIO/SCons


def _git(project_dir, *args):
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def _quoted(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '\\"{}\\"'.format(escaped)


project_dir = env.subst("$PROJECT_DIR")
commit = _git(project_dir, "rev-parse", "--short=12", "HEAD") or "unknown"
# Untracked source, headers, or build scripts can change the binary just as
# tracked edits can.  Gitignored generated directories (notably ``.pio``) stay
# excluded by Git itself, while every build-relevant untracked file marks the
# fingerprint dirty.
dirty = bool(_git(project_dir, "status", "--porcelain"))
build_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
    "+00:00", "Z"
)
build_environment = env.subst("$PIOENV") or "unknown"

env.Append(
    CPPDEFINES=[
        ("FW_GIT_COMMIT", _quoted(commit)),
        ("FW_BUILD_ENVIRONMENT", _quoted(build_environment)),
        ("FW_BUILD_TIME_UTC", _quoted(build_time)),
        ("FW_GIT_DIRTY", 1 if dirty else 0),
    ]
)
