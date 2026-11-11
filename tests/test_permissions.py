import errno
import os
import stat
from pathlib import Path
import pytest
from sqlalchemy import select
from app.models import FileTransfer,Release
from app.services import file_workflow,importer
from app.services.permissions import media_gid,probe_permissions
from test_workflow import configure,report

pytestmark=pytest.mark.skipif(os.name!="posix",reason="POSIX permissions are verified on Linux CI and in the container")


@pytest.mark.parametrize("portable",[False,True])
def test_private_album_becomes_group_writable_and_traversable(setup,monkeypatch,portable):
    configure(setup)
    release=importer.import_report(report(setup))
    source=setup.inbox/"Album"
    child=source/"CD1"
    child.mkdir()
    (child/"other.flac").write_bytes(b"other audio")
    (child/"Empty").mkdir()
    source.chmod(0o700)
    child.chmod(0o700)
    for path in (source/"track.flac",child/"other.flac"):
        path.chmod(0o600)
    if portable:
        def unsupported(*args):
            raise OSError(errno.EINVAL,"unsupported")
        monkeypatch.setattr(file_workflow,"rename_exclusive",unsupported)
    original_umask=os.umask(0o077)
    try:
        destination=file_workflow.move_release(release.id,"STAGING")
    finally:
        os.umask(original_umask)
    for path in (destination,destination/"CD1",destination/"CD1"/"Empty"):
        assert stat.S_IMODE(path.stat().st_mode)==0o2775
        assert path.stat().st_gid==(setup.root/"staging").stat().st_gid
    for path in (destination/"track.flac",destination/"CD1"/"other.flac"):
        assert stat.S_IMODE(path.stat().st_mode)==0o664
    assert (destination/"track.flac").read_bytes()==b"test audio"


def test_permission_failure_retains_source_and_can_resume(setup,monkeypatch):
    configure(setup)
    release=importer.import_report(report(setup))
    destination=setup.root/"staging"/"Album"
    original=file_workflow.apply_permissions
    def fail(path,*args,**kwargs):
        if Path(path)==destination:
            raise PermissionError("cannot apply group permissions")
        return original(path,*args,**kwargs)
    monkeypatch.setattr(file_workflow,"apply_permissions",fail)
    with pytest.raises(file_workflow.FileOperationError,match="permissions"):
        file_workflow.move_release(release.id,"STAGING")
    assert (setup.inbox/"Album"/"track.flac").read_bytes()==b"test audio"
    with setup.db() as session:
        transfer=session.scalar(select(FileTransfer))
        assert transfer.state=="NEEDS_ATTENTION"
        assert session.get(Release,release.id).source_path==str(setup.inbox/"Album")
    monkeypatch.setattr(file_workflow,"apply_permissions",original)
    file_workflow.resume_transfer(transfer.id)
    assert not (setup.inbox/"Album").exists()
    assert stat.S_IMODE((destination/"track.flac").stat().st_mode)==0o664


def test_permission_probe_uses_destination_group_and_cleans_only_its_own_probe(setup,monkeypatch):
    root=setup.root/"staging"
    monkeypatch.delenv("SONIC_MEDIA_GID",raising=False)
    assert probe_permissions(root)==root.stat().st_gid
    assert list(root.iterdir())==[]
    monkeypatch.setenv("SONIC_MEDIA_GID",str(root.stat().st_gid))
    assert media_gid(root)==root.stat().st_gid
    monkeypatch.setenv("SONIC_MEDIA_GID","media")
    with pytest.raises(ValueError,match="numeric"):
        media_gid(root)


def test_new_output_ancestors_are_shared_without_changing_existing_parent(setup,monkeypatch):
    from app.services.permissions import create_output_root
    parent=setup.root/"Existing"
    parent.mkdir()
    parent.chmod(0o750)
    output=parent/"validator"/"staging"
    old_umask=os.umask(0o077)
    try:
        create_output_root(output)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(parent.stat().st_mode)==0o750
    assert stat.S_IMODE((parent/"validator").stat().st_mode)==0o2775
    assert stat.S_IMODE(output.stat().st_mode)==0o2775
