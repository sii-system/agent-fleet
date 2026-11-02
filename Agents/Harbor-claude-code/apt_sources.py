"""Apply explicit APT mirrors; retain Python 3.6 compatibility for task images."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MIRROR_NAMES = (
    "HARBOR_APT_UBUNTU_MIRROR",
    "HARBOR_APT_DEBIAN_MIRROR",
    "HARBOR_APT_DEBIAN_SECURITY_MIRROR",
)


def rewrite_uri(uri, mirrors):
    parsed = urlsplit(uri)
    if (parsed.scheme not in ("http", "https") or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        return uri
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/")
    name = ""
    if host in ("deb.debian.org", "security.debian.org") and path == "/debian-security":
        name = "HARBOR_APT_DEBIAN_SECURITY_MIRROR"
    elif (host in ("deb.debian.org", "ftp.debian.org") or host.endswith(".ftp.debian.org")) and path == "/debian":
        name = "HARBOR_APT_DEBIAN_MIRROR"
    elif (host in ("archive.ubuntu.com", "security.ubuntu.com")
          or host.endswith(".archive.ubuntu.com")) and path == "/ubuntu":
        name = "HARBOR_APT_UBUNTU_MIRROR"
    return mirrors.get(name, "").rstrip("/") or uri


def rewrite_content(text, mirrors, *, deb822):
    if not deb822:
        return re.sub(
            r"^(\s*deb(?:-src)?\s+(?:\[[^\]\n]*\]\s+)?)(\S+)",
            lambda m: m[1] + rewrite_uri(m[2], mirrors), text, flags=re.MULTILINE,
        )
    lines = []
    uri_field = False
    for line in text.splitlines(keepends=True):
        field = re.match(r"^([\w-]+):", line)
        if field or not line.strip():
            uri_field = bool(field and field[1].lower() == "uris")
        if uri_field and not line.lstrip().startswith("#"):
            line = re.sub(r"https?://[^\s]+", lambda m: rewrite_uri(m[0], mirrors), line)
        lines.append(line)
    return "".join(lines)


def validate_mirrors(mirrors):
    for name, value in mirrors.items():
        if name not in MIRROR_NAMES or not value:
            continue
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(character.isspace() for character in value)):
            raise ValueError(f"{name} must be a credential-free HTTP(S) repository URL")


def configure_sources(root, mirrors):
    validate_mirrors(mirrors)
    # APT auth files can bind credentials to the original repository host/path.
    auth_files = [root / "auth.conf", *(root / "auth.conf.d").glob("*")]
    if any(p.is_file() and re.search(r"^\s*[^#\s]", p.read_text(), re.MULTILINE) for p in auth_files):
        return 0
    sources = [root / "sources.list", *(root / "sources.list.d").glob("*.list"),
               *(root / "sources.list.d").glob("*.sources")]
    changed = 0
    for source in sources:
        if not source.is_file() or source.is_symlink():
            continue
        original = source.read_text()
        rewritten = rewrite_content(original, mirrors, deb822=source.suffix == ".sources")
        if rewritten == original:
            continue
        info = source.stat()
        with tempfile.NamedTemporaryFile(mode="w", dir=source.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(rewritten)
                os.fchmod(output.fileno(), info.st_mode & 0o777)
                if os.geteuid() == 0:
                    os.fchown(output.fileno(), info.st_uid, info.st_gid)
            except BaseException:
                temporary.unlink()
                raise
        try:
            os.replace(temporary, source)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        changed += 1
    return changed


if __name__ == "__main__":
    try:
        count = configure_sources(Path("/etc/apt"), json.loads(sys.argv[1]))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Configured runtime APT mirrors in {count} source file(s)")
