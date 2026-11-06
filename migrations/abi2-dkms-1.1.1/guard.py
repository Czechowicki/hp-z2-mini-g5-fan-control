"""Read-only qualification for the ABI 2 DKMS client. No firmware commands."""
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import stat
import struct
import subprocess

KOLD = '6.8.0-147-generic'
TARGET = '6.8.0-149-generic'
NAME = 'hp_smm_fan'
VERSION = '1.1.1'
LIB = Path('/usr/local/lib/hp-z2-fan-dkms-1.1.1')
SOURCE = Path('/usr/src/hp_smm_fan-1.1.1')
OLDLIB = Path('/usr/local/lib/hp-z2-fan-abi2-147')
NOTE = Path('/sys/module/hp_smm_fan/notes/.note.gnu.build-id')
ABI = Path('/sys/kernel/debug/hp_smm_fan/control_abi')
OLD_NOTE = '32524abdaa1fa31d6e6de6c1063dd6126998e182fdb6bc705e9ff66c2f691c77'
OLD_START = 'c9723d1cbb74afe77588d31a10fbc0f09d25830e77ce18b526cc14d9091a123f'
DAEMON_SHA = 'fa57684564312bb11484327aa445fa8a740bc774b64b03f6383e6df2ef5fbd88'
DSDT_SHA = '88067a83dc0a13b1b07d4e16d17ddfb0f97e2b3002454e8ca9985daf4d040e97'
KERNEL_RE = r'6\.8\.0-(149|1[5-9][0-9]|[2-9][0-9][0-9]|[1-9][0-9]{3,})-generic'
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL':'C'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def secure(path):
    path = Path(path)
    s = path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
            'unsafe file: '+str(path))
    for d in path.parents:
        s = d.stat()
        require(stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
                'unsafe parent: '+str(d))


def run(*args):
    p = subprocess.run(args, env=ENV, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    return p.stdout.strip()


def platform_check():
    require(os.uname().machine == 'x86_64', 'architecture mismatch')
    for key, value in {'sys_vendor':'HP', 'product_name':'HP Z2 Mini G5 Workstation',
                       'board_name':'8755', 'bios_version':'S50 Ver. 01.04.20'}.items():
        require((Path('/sys/class/dmi/id')/key).read_text().strip() == value, 'DMI '+key)
    require(sha(Path('/sys/firmware/acpi/tables/DSDT').read_bytes()) == DSDT_SHA, 'DSDT changed')
    require(not Path('/run/hp-smm-policy-trial-unresolved').exists(), 'unresolved trial')
    require(not ABI.with_name('protocol').exists(), 'experimental module loaded')


def object_bytes(path):
    path = Path(path)
    secure(path)
    raw = path.read_bytes()
    if path.suffix == '.zst':
        raw = subprocess.run(['/usr/bin/zstd', '-q', '-d', '-c', str(path)],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    elif path.suffix == '.xz':
        raw = lzma.decompress(raw)
    elif path.suffix == '.gz':
        raw = gzip.decompress(raw)
    require(raw.startswith(b'\x7fELF'), 'not an ELF module')
    return raw


def elf_note(raw):
    # ELF64 little-endian relocatable x86-64 kernel module, bounds checked.
    require(len(raw) >= 64 and raw[:6] == b'\x7fELF\x02\x01', 'ELF format')
    require(struct.unpack_from('<HH',raw,16) == (1,62), 'ELF type/machine')
    off = struct.unpack_from('<Q',raw,40)[0]
    size,count,strings = struct.unpack_from('<HHH',raw,58)
    require(size == 64 and 0 < strings < count and off+size*count <= len(raw), 'ELF sections')
    sections = [struct.unpack_from('<IIQQQQIIQQ',raw,off+i*size) for i in range(count)]
    def data(s):
        require(s[4]+s[5] <= len(raw), 'ELF section bounds')
        return raw[s[4]:s[4]+s[5]]
    names = data(sections[strings])
    found = []
    for s in sections:
        require(s[0] < len(names), 'ELF name bounds')
        if names[s[0]:].split(b'\0',1)[0] == b'.note.gnu.build-id':
            found.append(data(s))
    require(len(found) == 1 and len(found[0]) >= 16, 'missing/duplicate build ID')
    return found[0]


def disk_check(kernel, pins):
    require(re.fullmatch(KERNEL_RE,kernel), 'kernel outside 6.8 generic >=149')
    for n in ('hp_smm_fan.c','Makefile','dkms.conf'):
        p = SOURCE/n
        secure(p)
        require(sha(p.read_bytes()) == pins['source/'+n], 'DKMS source changed: '+n)
    base = Path('/var/lib/dkms')/NAME/VERSION/kernel/'x86_64/module'
    candidates = [p for p in base.glob(NAME+'.ko*') if p.name in
                  (NAME+'.ko',NAME+'.ko.zst',NAME+'.ko.xz',NAME+'.ko.gz')]
    require(len(candidates) == 1, 'missing/ambiguous DKMS module')
    built = candidates[0]
    selected = Path(run('modinfo','-k',kernel,'-n',NAME)).resolve()
    require(selected.is_relative_to(Path('/lib/modules',kernel).resolve()), 'module outside kernel tree')
    raw = object_bytes(built)
    require(raw == object_bytes(selected), 'selected module differs from DKMS build')
    require(run('modinfo','-F','vermagic',str(selected)).split()[0] == kernel, 'vermagic mismatch')
    return elf_note(raw), selected


def identify(pins):
    platform_check()
    kernel = os.uname().release
    require(ABI.with_name('min').read_text().strip() != '-2', 'unknown firmware outcome')
    if kernel == KOLD:
        secure(OLDLIB/'start.py')
        require(sha((OLDLIB/'start.py').read_bytes()) == OLD_START, 'old selector changed')
        require(sha(NOTE.read_bytes()) == OLD_NOTE and ABI.read_text().strip() == '2', '147 loaded module changed')
        return OLDLIB/'start.py'
    note,_ = disk_check(kernel,pins)
    require(note == NOTE.read_bytes(), 'loaded module differs from built/selected module')
    require(ABI.read_text().strip() == '2', 'loaded ABI is not 2')
    require(ABI.with_name('min').read_text().strip() != '-2', 'unknown firmware outcome')
    secure(LIB/'fan-daemon.py')
    require(sha((LIB/'fan-daemon.py').read_bytes()) == DAEMON_SHA, 'daemon changed')
    return LIB/'fan-daemon.py'
