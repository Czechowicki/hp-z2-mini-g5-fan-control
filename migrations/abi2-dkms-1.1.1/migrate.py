#!/usr/bin/python3 -I
"""Stage ABI 2 DKMS for kernel 149, keeping the running 147 bundle intact."""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guard',HERE/'guard.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
STATE = Path('/var/lib/hp-z2-fan-dkms-migration-1.1.1')
RECEIPT = STATE/'receipt.json'
DROP = Path('/etc/systemd/system/hp-fan.service.d/82-hp-z2-dkms.conf')
OLD_DROP = DROP.with_name('81-hp-z2-fan-abi2.conf')
CONFIG = Path('/etc/hp-fan.conf')
DROP_DATA = b'''[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I -B /usr/local/lib/hp-z2-fan-dkms-1.1.1/start.py\nRestartPreventExitStatus=79\n'''
OLD_DROP_DATA = b'''[Unit]\nAfter=systemd-modules-load.service\n[Service]\nExecStartPre=\nExecStartPre=/sbin/modprobe -q hp_smm_fan\nExecStart=\nExecStart=/usr/bin/python3 -I -B /usr/local/lib/hp-z2-fan-abi2-147/start.py\nRestartPreventExitStatus=79\n'''
OLD_RULE = Path('/etc/depmod.d/81-hp-z2-fan-abi2.conf')
RULE_DATA = b'override hp_smm_fan ^6[.]8[.]0-147-generic$ hp-z2-abi2\n'


def command(*args):
    # Deliberately no modprobe/rmmod, service restart/stop, reboot or hardware writer.
    print('COMMAND '+json.dumps(args),flush=True)
    p = subprocess.run(args,env=g.ENV,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    print(p.stdout,flush=True)
    if STATE.exists():
        with (STATE/'commands.log').open('a') as log:
            log.write(json.dumps(args)+'\n'+p.stdout+'\nexit='+str(p.returncode)+'\n')
    p.check_returncode()
    return p.stdout.strip()


def write_owned(path,data,replace=False):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    for parent in (path.parent,*path.parent.parents):
        s=parent.stat()
        g.require(s.st_uid==0 and not s.st_mode & 0o022,'unsafe parent '+str(parent))
    if os.path.lexists(path):
        g.secure(path)
        if path.read_bytes()==data:
            return
        g.require(replace,'file collision '+str(path))
    fd,temp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            os.fchmod(f.fileno(),0o644)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(temp,path)
        else:
            os.link(temp,path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(state):
    write_owned(RECEIPT,(json.dumps(state,indent=2)+'\n').encode(),replace=True)


def pins():
    data=json.loads((HERE/'pins.json').read_text())
    for name,digest in data.items():
        g.require(g.sha((HERE/name).read_bytes())==digest,'package changed '+name)
    return data


def active_147():
    g.platform_check()
    g.require(os.uname().release==g.KOLD,'Run this action on the verified kernel '+g.KOLD)
    g.require(g.sha(g.NOTE.read_bytes())==g.OLD_NOTE and g.ABI.read_text().strip()=='2','loaded 147 ABI2 changed')
    cache=int(g.ABI.with_name('min').read_text())
    g.require(0<=cache<=100,'no acknowledged minimum, or unknown firmware result')
    for p,digest in ((g.OLDLIB/'start.py',g.OLD_START),(g.OLDLIB/'fan-daemon.py',g.DAEMON_SHA)):
        g.secure(p)
        g.require(g.sha(p.read_bytes())==digest,'147 runtime changed '+str(p))
    for p,data in ((OLD_DROP,OLD_DROP_DATA),(OLD_RULE,RULE_DATA)):
        g.secure(p)
        g.require(p.read_bytes()==data,'147 installation changed '+str(p))
    chosen=Path(g.run('modinfo','-k',g.KOLD,'-n',g.NAME)).resolve()
    g.require(chosen==Path('/lib/modules',g.KOLD,'hp-z2-abi2/hp_smm_fan.ko').resolve(),'147 selection changed')
    g.require(g.sha(chosen.read_bytes())=='90543670ac00821f8954c2a96d19f5e37ec12935870a578aca8fc54e79d2c765','147 object changed')
    g.require(g.run('systemctl','show','--value','--property=ActiveState','hp-fan.service')=='active','service inactive')
    pid=g.run('systemctl','show','--value','--property=MainPID','hp-fan.service')
    g.require(pid.isdecimal() and int(pid)>0,'invalid service PID')
    cmd=(Path('/proc')/pid/'cmdline').read_bytes().split(b'\0')
    g.require(os.fsencode(g.OLDLIB/'fan-daemon.py') in cmd,'unexpected running daemon')
    return pid


def known_configuration():
    # DKMS sources shell configuration. Refuse custom hooks / live-load settings.
    configs=[Path('/etc/dkms/framework.conf'),*Path('/etc/dkms/framework.conf.d').glob('*.conf')]
    for p in configs:
        if p.exists():
            g.secure(p)
            active=[s for s in p.read_text().splitlines() if s.strip() and not s.lstrip().startswith('#')]
            g.require(not active,'review active DKMS framework settings first: '+str(p))
    g.require(not list(Path('/etc/dkms').glob('hp_smm_fan*.conf')),'custom DKMS module overrides')
    expected=json.loads((HERE/'legacy-pins.json').read_text())
    for name,digest in expected.items():
        p=Path('/usr/src/hp_smm_fan-1.0')/name
        g.secure(p)
        g.require(g.sha(p.read_bytes())==digest,'legacy source changed '+str(p))
    # Version is restricted to the implementation reviewed offline.
    g.require(g.run('dkms','--version')=='dkms-3.0.11','unreviewed DKMS version')
    allowed={str(OLD_DROP)}
    if RECEIPT.exists():
        allowed.add(str(DROP))
    found=[]
    for d in ('/etc/systemd/system','/run/systemd/system','/usr/lib/systemd/system'):
        found.extend(str(p) for p in (Path(d)/'hp-fan.service.d').glob('*.conf'))
    g.require(set(found)<=allowed,'unreviewed service drop-in')
    g.secure(Path('/etc/systemd/system/hp-fan.service'))
    g.require(g.sha(Path('/etc/systemd/system/hp-fan.service').read_bytes())==
              'bbfd8fd255a57cad50496c1cea8ae54ea1e3fdf46bf9186cc4da0201d18360c1','base unit changed')
    g.secure(CONFIG)
    curve=[]
    for line in CONFIG.read_text().splitlines():
        parts=line.split('#',1)[0].split()
        if not parts:
            continue
        g.require(len(parts)==2,'invalid curve line')
        t,p=map(int,parts)
        g.require(0<=t<=120 and 0<=p<=100,'invalid curve range')
        curve.append((t,p))
    curve.sort()
    g.require(len(curve)>=2 and all(a[0]<b[0] and a[1]<=b[1] for a,b in zip(curve,curve[1:])), 'invalid curve')


def protected():
    files=[CONFIG,OLD_DROP,OLD_RULE,g.OLDLIB/'start.py',g.OLDLIB/'fan-daemon.py',
           Path('/usr/local/sbin/fan-daemon.py'),Path('/etc/systemd/system/hp-fan.service'),
           Path('/lib/modules',g.KOLD,'hp-z2-abi2/hp_smm_fan.ko'),
           Path('/boot/initrd.img-'+g.KOLD)]
    return {str(p):g.sha(p.read_bytes()) for p in files}


def unchanged(snapshot):
    for name,digest in snapshot.items():
        g.require(g.sha(Path(name).read_bytes())==digest,'protected file changed '+name)


def check_initrd(kernel,p):
    # Extract only into a fresh directory. Never execute extracted files.
    with tempfile.TemporaryDirectory(prefix='initrd-',dir=STATE) as d:
        command('unmkinitramfs','/boot/initrd.img-'+kernel,d)
        objects=list(Path(d).rglob(g.NAME+'.ko*'))
        for obj in objects:
            # Temp directory has mode 0700, root-owned when installed.
            raw=g.object_bytes(obj)
            g.require(g.elf_note(raw)==p,'initramfs contains a different fan module')
    return len(objects)


def stage():
    payload=pins()
    g.require(not STATE.exists(),'existing migration state; use status or rollback, do not repeat stage')
    pid=active_147()
    known_configuration()
    g.require('[none]' in Path('/sys/kernel/security/lockdown').read_text() and
              Path('/sys/module/module/parameters/sig_enforce').read_text().strip()=='N',
              'signature enforcement/lockdown changed; no security settings will be modified')
    for exe in ('dkms','make','gcc','modinfo','depmod','update-initramfs','unmkinitramfs','zstd'):
        g.require(shutil.which(exe,path=g.ENV['PATH']), 'missing tool '+exe)
    g.require(Path('/lib/modules',g.TARGET,'build').is_dir() and
              Path('/boot/vmlinuz-'+g.TARGET).is_file() and
              Path('/boot/initrd.img-'+g.TARGET).is_file(),'kernel 149/image/headers missing')
    for p in (g.LIB,g.SOURCE,DROP,Path('/var/lib/dkms')/g.NAME/g.VERSION):
        g.require(not os.path.lexists(p),'preexisting destination '+str(p))
    all_status=g.run('dkms','status','-m',g.NAME)
    g.require(all(line.startswith('hp_smm_fan/1.0, ') for line in all_status.splitlines()),'unexpected DKMS version/state')
    g.require('hp_smm_fan/1.0, '+g.TARGET+', x86_64: installed' in all_status.splitlines(),'149 legacy DKMS not installed')
    old149=Path(g.run('modinfo','-k',g.TARGET,'-n',g.NAME)).resolve()
    g.require(old149.is_relative_to(Path('/lib/modules',g.TARGET,'updates/dkms').resolve()),'unexpected 149 selection')
    g.require(g.run('modinfo','-F','srcversion',str(old149))=='AB609EB0420D9B734DA95B7','unexpected legacy 149 module')
    snapshot=protected()
    state={'phase':'PREPARING','target':g.TARGET,'pid_before':pid,'protected':snapshot,
           'old149_path':str(old149),'old149_sha256':g.sha(old149.read_bytes())}
    save(state)  # Persistent recovery information precedes mutations.
    write_owned(STATE/'original-149-module.bin',old149.read_bytes())
    for name in ('hp_smm_fan.c','Makefile','dkms.conf'):
        write_owned(g.SOURCE/name,(HERE/'source'/name).read_bytes())
    for name in ('guard.py','start.py','pins.json'):
        write_owned(g.LIB/name,(HERE/name).read_bytes())
    write_owned(g.LIB/'fan-daemon.py',(HERE/'source/fan-daemon.py').read_bytes())
    state['phase']='BUILDING';save(state)
    command('dkms','add','-m',g.NAME,'-v',g.VERSION)
    command('dkms','build','-m',g.NAME,'-v',g.VERSION,'-k',g.TARGET,'-j','2')
    state['phase']='INSTALLING';save(state)
    command('dkms','install','-m',g.NAME,'-v',g.VERSION,'-k',g.TARGET,'--force')
    command('depmod','-a',g.TARGET)
    note,selected=g.disk_check(g.TARGET,payload)
    command('update-initramfs','-u','-k',g.TARGET)
    count=check_initrd(g.TARGET,note)
    # The new selector is published last. Its 147 branch delegates to the old one.
    write_owned(DROP,DROP_DATA)
    command('systemctl','daemon-reload')
    unchanged(snapshot)
    g.require(active_147()==pid,'running service PID changed')
    state.update(phase='STAGED_FOR_149_REBOOT',selected=str(selected),initrd_fan_objects=count)
    save(state)
    print('STAGED_FOR_149_REBOOT\nRUNNING_147_UNCHANGED\nCURVE_UNCHANGED')


def rollback():
    # Rollback never manipulates a loaded 149 module. Boot the preserved 147 first.
    pid=active_147()
    known_configuration()
    g.secure(RECEIPT)
    state=json.loads(RECEIPT.read_text())
    g.require(state['target']==g.TARGET,'unexpected receipt target')
    unchanged(state['protected'])
    payload=pins()
    for p in (g.SOURCE,g.LIB):
        if p.exists():
            expected=({'hp_smm_fan.c':payload['source/hp_smm_fan.c'],'Makefile':payload['source/Makefile'],
                       'dkms.conf':payload['source/dkms.conf']} if p==g.SOURCE else
                      {'guard.py':payload['guard.py'],'start.py':payload['start.py'],
                       'pins.json':g.sha((HERE/'pins.json').read_bytes()),'fan-daemon.py':g.DAEMON_SHA})
            for f in p.iterdir():
                g.require(f.name in expected,'unowned file in migration directory '+str(f))
                g.secure(f)
                g.require(g.sha(f.read_bytes())==expected[f.name],'modified migration file '+str(f))
    text=g.run('dkms','status','-m',g.NAME,'-v',g.VERSION)
    for line in text.splitlines():
        g.require(line.startswith(g.NAME+'/'+g.VERSION+':') or
                  line.startswith(g.NAME+'/'+g.VERSION+', '+g.TARGET+', x86_64:'),
                  'migration used on additional kernels; review rollback coverage first')
    if DROP.exists():
        g.secure(DROP)
        g.require(DROP.read_bytes()==DROP_DATA,'changed migration drop-in')
    state['phase']='ROLLING_BACK';save(state)
    if text:
        command('dkms','remove','-m',g.NAME,'-v',g.VERSION,'--all')
    command('dkms','install','-m',g.NAME,'-v','1.0','-k',g.TARGET,'--force')
    command('depmod','-a',g.TARGET)
    legacy=Path(g.run('modinfo','-k',g.TARGET,'-n',g.NAME)).resolve()
    g.require(g.run('modinfo','-F','srcversion',str(legacy))=='AB609EB0420D9B734DA95B7','legacy selection not restored')
    command('update-initramfs','-u','-k',g.TARGET)
    check_initrd(g.TARGET,g.elf_note(g.object_bytes(legacy)))
    if DROP.exists():
        DROP.unlink()
    command('systemctl','daemon-reload')
    for p in (g.SOURCE,g.LIB):
        if p.exists():
            for f in p.iterdir():
                f.unlink()
            p.rmdir()
    unchanged(state['protected'])
    g.require(active_147()==pid,'running service PID changed')
    state['phase']='ROLLED_BACK_TO_147_BUNDLE';save(state)
    print('ROLLED_BACK_TO_147_BUNDLE\n149_HAS_LEGACY_1_0_AGAIN\nKEEP_USING_147_FOR_ABI2')


def status():
    payload=pins()
    g.platform_check()
    kernel=os.uname().release
    client=g.identify(payload)
    pid=g.run('systemctl','show','--value','--property=MainPID','hp-fan.service')
    g.require(pid.isdecimal() and int(pid)>0,'service has no process')
    expected=g.OLDLIB/'fan-daemon.py' if kernel==g.KOLD else client
    g.require(os.fsencode(expected) in (Path('/proc')/pid/'cmdline').read_bytes().split(b'\0'),'wrong daemon process')
    g.require(g.run('systemctl','show','--value','--property=ActiveState','hp-fan.service')=='active','service inactive')
    cache=int(g.ABI.with_name('min').read_text())
    g.require(0<=cache<=100,'no acknowledged minimum')
    report={'kernel':kernel,'pid':pid,'client':str(expected),'control_abi':2,'minimum_command_cache':cache,
            'status':'ABI2_147_ACTIVE' if kernel==g.KOLD else 'DKMS_ABI2_ACTIVE',
            'firmware_or_rpm_verification':'NOT_PERFORMED'}
    if RECEIPT.exists():
        report['migration_phase']=json.loads(RECEIPT.read_text())['phase']
    print(json.dumps(report,indent=2))


@contextlib.contextmanager
def locks():
    with contextlib.ExitStack() as stack:
        for name in ('/run/lock/hp-smm-handoff-trial.lock','/var/lib/dpkg/lock-frontend',
                     '/var/lib/dpkg/lock','/var/cache/apt/archives/lock'):
            f=stack.enter_context(open(name,'a'))
            if name.startswith('/run/'):
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action',nargs='?',default='plan',choices=('plan','stage','status','rollback'))
    action=ap.parse_args().action
    if action=='plan':
        print('OFFLINE_PLAN_ONLY\nStage ABI2 DKMS 1.1.1 for 149 and subsequent 6.8 generic kernels.\n'
              'Keep the 147 bundle and curve. No live reload or firmware commands.\n'
              'Read README-PL.txt. Run stage only on the HP currently running verified 147.')
        return
    g.require(os.geteuid()==0,'use sudo /usr/bin/python3 -IB migrate.py '+action)
    if action=='status':
        status()
    else:
        with locks():
            {'stage':stage,'rollback':rollback}[action]()


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,subprocess.SubprocessError,KeyboardInterrupt) as e:
        sys.exit('STOP: '+str(e)+'\nDo not restart the service or reboot after a failed stage. '
                 'Keep /var/lib/hp-z2-fan-dkms-migration-1.1.1 and send the output. See README-PL.txt.')
