"""Container-only regression: SonicSentry and Picard use different UIDs in media GID 110000."""
import os
import subprocess
import tempfile
from pathlib import Path

assert os.geteuid()==0,"Run this test container with --user 0:0; production remains non-root"

WORKER = r"""
import os,json,stat,subprocess,wave
from pathlib import Path
root=Path(os.environ['PERMISSIONS_ROOT'])
os.umask(0o077)
from app.database import engine,SessionLocal
from app.migrations import initialize_database
from app.services.importer import import_report
from app.services import file_workflow
from app.models import FileTransfer
from sqlalchemy import select
initialize_database(engine)
source=root/'inbox'/'Album'
(source/'CD1'/'Empty').mkdir(parents=True)
with wave.open(str(source/'CD1'/'track.wav'),'wb') as audio:
    audio.setparams((1,2,44100,0,'NONE','not compressed'))
    audio.writeframes(b'\0\0'*4410)
subprocess.run(['flac','--silent',str(source/'CD1'/'track.wav')],check=True)
report=root/'report.json'
report.write_text(json.dumps({'schema_version':1,'release':str(source),'files':[{'path':str(source/'CD1'/'track.flac'),'status':'PASS','integrity':{'ok':True}}]}))
release=import_report(report)
for directory in (source,source/'CD1',source/'CD1'/'Empty'):
    directory.chmod(0o700)
for file in (source/'CD1').iterdir():
    if file.is_file():file.chmod(0o600)
# Named source ACL must not be imported into the destination.
subprocess.run(['setfacl','-m','u:30003:r--,g::---,m::r--',str(source/'CD1'/'track.flac')],check=True)
if os.environ['PUBLICATION']=='portable':
    import errno
    def unsupported(*args):raise OSError(errno.EINVAL,'simulated unsupported rename')
    file_workflow.rename_exclusive=unsupported
try:
    destination=file_workflow.move_release(release.id,'STAGING')
except file_workflow.FileOperationError:
    if os.environ.get('EXPECT_FAILURE')!='yes':raise
    assert (source/'CD1'/'track.flac').exists()
    with SessionLocal() as session:assert session.scalar(select(FileTransfer)).state=='NEEDS_ATTENTION'
    print('Unassignable GID refused with originals retained',flush=True)
else:
    assert os.environ.get('EXPECT_FAILURE')!='yes'
    for directory in (destination,destination/'CD1',destination/'CD1'/'Empty'):
        assert stat.S_IMODE(directory.stat().st_mode)==0o2775,(directory,oct(directory.stat().st_mode))
        assert directory.stat().st_gid==110000
    track=destination/'CD1'/'track.flac'
    assert stat.S_IMODE(track.stat().st_mode)==0o664 and track.stat().st_gid==110000
    acl=subprocess.check_output(['getfacl','-cp',str(track)],text=True)
    assert 'user:30003:' not in acl
    assert 'user:30004:' in acl  # Keep named destination ACLs.
    assert not source.exists()
"""

PICARD = r"""
import os,subprocess
from pathlib import Path
root=Path(os.environ['PERMISSIONS_ROOT'])
folder=root/'staging'/'Album'
track=folder/'CD1'/'track.flac'
with track.open('rb') as file:assert file.read(4)==b'fLaC'
subprocess.run(['metaflac','--set-tag=TITLE=Edited by Picard user',str(track)],check=True)
subprocess.run(['flac','--silent','--test',str(track)],check=True)
(folder/'CD1').rename(folder/'Disc 1')
(folder/'Picard created').mkdir()
assert (folder/'Picard created').stat().st_gid==110000
assert (folder/'Disc 1'/'track.flac').stat().st_gid==110000
print('Separate Picard UID read, tagged, traversed and renamed successfully',flush=True)
"""


def identity(uid,gid,groups):
    def drop():
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
    return drop

for publication in ('native','portable','denied'):
    with tempfile.TemporaryDirectory(prefix='sonic-permissions-test-') as temporary:
        root=Path(temporary)
        root.chmod(0o755)
        for name in ('data','reports','uploads','inbox','staging','quarantine','rejected','trash'):
            path=root/name
            path.mkdir()
            os.chown(path,10001,110000 if name=='staging' else 10001)
            path.chmod(0o2775 if name=='staging' else 0o755)
        # Inherited ACL initially denies the owning group, despite other named entries.
        subprocess.run(['setfacl','-d','-m','u::rwx,g::---,m::rwx,o::r-x,u:30004:r-x',str(root/'staging')],check=True)
        env={**os.environ,'PERMISSIONS_ROOT':str(root),'PUBLICATION':publication,
            'SONIC_MEDIA_GID':'119999' if publication=='denied' else '110000',
            'LOSSLESS_VALIDATOR_DATA_DIR':str(root/'data'),'LOSSLESS_REPORT_ROOT':str(root/'reports'),
            'LOSSLESS_UPLOAD_ROOT':str(root/'uploads'),'LOSSLESS_SOURCE_ROOTS':str(root/'inbox'),
            'LOSSLESS_MOVABLE_ROOTS':str(root/'inbox'),'LOSSLESS_STAGING_ROOT':str(root/'staging'),
            'LOSSLESS_QUARANTINE_ROOT':str(root/'quarantine'),'LOSSLESS_REJECTED_ROOT':str(root/'rejected'),
            'LOSSLESS_TRASH_ROOT':str(root/'trash'),'LOSSLESS_FILE_OPERATIONS':'true'}
        if publication=='denied':env['EXPECT_FAILURE']='yes'
        subprocess.run(['python','-c',WORKER],env=env,preexec_fn=identity(10001,10001,[110000]),check=True)
        if publication!='denied':
            subprocess.run(['python','-c',PICARD],env=env,preexec_fn=identity(20002,20002,[110000]),check=True)
print('Permissions regression passed: private source, umask 0077, ACLs, both publication modes and separate Picard UID')
