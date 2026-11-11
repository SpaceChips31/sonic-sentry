"""Destination-owned sharing policy. Source modes and ACLs are never imported."""
import errno
import os
import stat
import subprocess
import tempfile
from pathlib import Path

DIRECTORY_MODE=0o2775
FILE_MODE=0o664


def media_gid(root: Path) -> int | None:
    if os.name != "posix":
        return None
    value=os.getenv("SONIC_MEDIA_GID", "").strip()
    if value:
        if not value.isdecimal() or not 0 <= int(value) < 2**32-1:
            raise ValueError("SONIC_MEDIA_GID must be a numeric host group ID")
        return int(value)
    return root.stat().st_gid


def _acl(path: Path, required: str):
    # chmod updates the ACL mask, not necessarily the owning-group entry.
    # Retain named destination ACL entries, but ensure the owning media group can work.
    try:
        os.getxattr(path,"system.posix_acl_access",follow_symlinks=False)
    except AttributeError:
        return
    except OSError as exc:
        if exc.errno in {errno.ENODATA,errno.ENOTSUP,errno.EOPNOTSUPP}:
            return
        raise
    result=subprocess.run(["getfacl","-cp","--",str(path)],capture_output=True,text=True,check=True)
    entries=dict(line.split(":",2)[::2] for line in result.stdout.splitlines() if line.startswith(("group::","mask::")))
    group=entries.get("group", "").split()[0]
    mask=entries.get("mask", "rwx").split()[0]
    if not all(char in group and char in mask for char in required):
        subprocess.run(["setfacl","-m",f"g::{required},m::{required}","--",str(path)],capture_output=True,text=True,check=True)


def _default_acl(path: Path):
    try:
        os.getxattr(path,"system.posix_acl_default",follow_symlinks=False)
    except AttributeError:
        return
    except OSError as exc:
        if exc.errno in {errno.ENODATA,errno.ENOTSUP,errno.EOPNOTSUPP}:
            return
        raise
    result=subprocess.run(["getfacl","-cp","--",str(path)],capture_output=True,text=True,check=True)
    entries={line.split(":",3)[1]:line.split(":",3)[3].split()[0] for line in result.stdout.splitlines() if line.startswith(("default:group::","default:mask::"))}
    if entries.get("group")!="rwx" or entries.get("mask")!="rwx":
        subprocess.run(["setfacl","-m","d:g::rwx,d:m::rwx","--",str(path)],capture_output=True,text=True,check=True)


def apply_permissions(path: Path, gid: int | None, *, directory: bool):
    if os.name != "posix":
        return
    current=path.lstat()
    if stat.S_ISLNK(current.st_mode) or not (stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(current.st_mode)):
        raise ValueError(f"Refusing permissions on a linked or nonregular path: {path}")
    mode=DIRECTORY_MODE if directory else FILE_MODE
    try:
        if current.st_gid!=gid:
            os.chown(path,-1,gid,follow_symlinks=False)
        if stat.S_IMODE(path.stat().st_mode)!=mode:
            path.chmod(mode)
        _acl(path,"rwx" if directory else "rw-")
        if directory:
            _default_acl(path)
        actual=path.stat()
        if actual.st_gid!=gid or stat.S_IMODE(actual.st_mode)!=mode:
            raise PermissionError("filesystem did not retain the requested group/mode")
    except (OSError,subprocess.SubprocessError) as exc:
        raise PermissionError(f"Cannot set shared permissions on {path}: expected GID {gid}, mode {mode:04o}. "
            "Use the host media GID in SONIC_MEDIA_GID and PGID/group_add; check filesystem ACL/mount support. "
            f"Original files are retained. {exc}") from exc


def ensure_directory(path: Path, root: Path, gid: int | None):
    if not path.is_relative_to(root):
        raise ValueError("Permission directory is outside the owned destination")
    for directory in [root]+[root.joinpath(*path.relative_to(root).parts[:i]) for i in range(1,len(path.relative_to(root).parts)+1)]:
        if not directory.exists():
            directory.mkdir()
        apply_permissions(directory,gid,directory=True)


def prepare_root(root: Path, gid: int | None, settings: dict):
    # Only configured output roots may have their existing permissions adjusted.
    managed={Path(settings[key]).resolve() for key in ("staging","quarantine","rejected","trash") if settings[key]}
    if root.resolve() in managed:
        apply_permissions(root,gid,directory=True)


def probe_permissions(root: Path):
    gid=media_gid(root)
    with tempfile.TemporaryDirectory(prefix=".sonic-permissions-",dir=root) as temporary:
        directory=Path(temporary)
        apply_permissions(directory,gid,directory=True)
        child=directory/"nested"
        ensure_directory(child,directory,gid)
        file=child/"probe"
        file.write_bytes(b"probe")
        apply_permissions(file,gid,directory=False)
    return gid


def create_output_root(root: Path):
    """Normalize only ancestors created by this operation; existing parents are untouched."""
    missing=[]
    existing=root
    while not existing.exists():
        missing.append(existing)
        existing=existing.parent
    gid=media_gid(existing)
    for path in reversed(missing):
        path.mkdir()
        apply_permissions(path,gid,directory=True)
    return root
