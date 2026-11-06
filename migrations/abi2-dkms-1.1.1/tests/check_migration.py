"""Offline only. Host paths and commands are replaced with temporary fixtures."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('migration',HERE/'migrate.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
g=m.g


def elf():
    raw=bytearray(340)
    raw[:6]=b'\x7fELF\x02\x01'
    struct.pack_into('<HH',raw,16,1,62)
    struct.pack_into('<Q',raw,40,64)
    struct.pack_into('<HHH',raw,58,64,3,1)
    names=b'\0.shstrtab\0.note.gnu.build-id\0'
    note=struct.pack('<III',4,20,3)+b'GNU\0'+b'1'*20
    raw[256:256+len(names)]=names
    raw[300:300+len(note)]=note
    struct.pack_into('<IIQQQQIIQQ',raw,128,1,3,0,0,256,len(names),0,0,1,0)
    struct.pack_into('<IIQQQQIIQQ',raw,192,11,7,0,0,300,len(note),0,0,4,0)
    return bytes(raw),note


class GuardTests(unittest.TestCase):
    def test_elf_note(self):
        raw,note=elf();self.assertEqual(g.elf_note(raw),note)
    def test_elf_bounds(self):
        for raw in (b'',b'not ELF',elf()[0][:100]):
            with self.assertRaises(ValueError):g.elf_note(raw)
    def test_kernel_scope(self):
        for k in ('6.8.0-149-generic','6.8.0-150-generic','6.8.0-1000-generic'):
            self.assertIsNotNone(re.fullmatch(g.KERNEL_RE,k))
        for k in ('6.8.0-147-generic','6.8.0-148-generic','6.11.0-149-generic','6.8.0-149-lowlatency'):
            self.assertIsNone(re.fullmatch(g.KERNEL_RE,k))
    def test_secure_rejects_user_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'file';p.write_text('x')
            with self.assertRaises(ValueError):g.secure(p)
    def test_default_plan_has_no_host_access(self):
        with patch.object(sys,'argv',['migrate.py']),patch.object(g,'platform_check',side_effect=AssertionError), \
             patch.object(m,'stage',side_effect=AssertionError),contextlib.redirect_stdout(io.StringIO()) as out:
            m.main()
        self.assertIn('OFFLINE_PLAN_ONLY',out.getvalue())
    def test_package_hashes(self):m.pins()
    def test_no_live_mutating_commands(self):
        import ast
        tree=ast.parse((HERE/'migrate.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='command':
                vals=[a.value for a in node.args if isinstance(a,ast.Constant)]
                self.assertNotIn('modprobe',vals)
                self.assertNotIn('rmmod',vals)
                self.assertNotIn('reboot',vals)
                if vals and vals[0]=='systemctl':self.assertEqual(vals[1],'daemon-reload')
    def test_old_dkms_source_no_hooks(self):
        text=(HERE/'source/dkms.conf').read_text()
        self.assertNotIn('POST_',text);self.assertNotIn('PRE_',text)
        self.assertIn('AUTOINSTALL="yes"',text)
        self.assertIn('${kernel_source_dir}',text)


class StageFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.commands=[];self.fail=None
        # Keep all absolute host file operations inside this fixture.
        def mapped(*args):
            p=Path(*args)
            return self.root/str(p).lstrip('/') if p.is_absolute() and not p.is_relative_to(self.root) else p
        self.stack.enter_context(patch.object(m,'Path',side_effect=mapped))
        for obj,key,value in ((m,'STATE',self.root/'state'),(m,'RECEIPT',self.root/'state/receipt.json'),
                              (m,'DROP',self.root/'etc/new.conf'),(g,'LIB',self.root/'new-runtime'),
                              (g,'SOURCE',self.root/'src')):
            self.stack.enter_context(patch.object(obj,key,value))
        self.stack.enter_context(patch.object(m,'active_147',return_value='5543'))
        self.stack.enter_context(patch.object(m,'known_configuration'))
        self.stack.enter_context(patch.object(m,'protected',return_value={'curve':'unchanged'}))
        self.stack.enter_context(patch.object(m,'unchanged'))
        self.stack.enter_context(patch.object(g,'secure'))
        self.stack.enter_context(patch.object(m.shutil,'which',return_value='/mock/executable'))
        def write(p,data,replace=False):
            p.parent.mkdir(parents=True,exist_ok=True)
            if p.exists() and p.read_bytes()!=data and not replace:raise ValueError('collision')
            p.write_bytes(data)
        self.stack.enter_context(patch.object(m,'write_owned',side_effect=write))
        for name,data in (('/sys/kernel/security/lockdown','[none]'),('/sys/module/module/parameters/sig_enforce','N'),
                          ('/boot/vmlinuz-'+g.TARGET,'image'),('/boot/initrd.img-'+g.TARGET,'initrd')):
            p=mapped(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
        mapped('/lib/modules',g.TARGET,'build').mkdir(parents=True)
        self.old=mapped('/lib/modules',g.TARGET,'updates/dkms/hp_smm_fan.ko')
        self.old.parent.mkdir(parents=True);self.old.write_bytes(b'original')
        self.dkms_status=''
        def read(*args):
            if args[0]=='dkms':
                if '-v' in args:return self.dkms_status
                return 'hp_smm_fan/1.0, '+g.TARGET+', x86_64: installed'
            if args[0]=='modinfo':
                return 'AB609EB0420D9B734DA95B7' if 'srcversion' in args else str(self.old)
            raise AssertionError(args)
        self.stack.enter_context(patch.object(g,'run',side_effect=read))
        self.stack.enter_context(patch.object(g,'disk_check',return_value=(b'note',self.old)))
        self.stack.enter_context(patch.object(m,'check_initrd',return_value=0))
        def cmd(*args):
            self.commands.append(args)
            if len(self.commands)==self.fail:raise subprocess.CalledProcessError(1,args)
            if args[:2]==('dkms','add'):self.dkms_status=g.NAME+'/'+g.VERSION+': added'
            return ''
        self.stack.enter_context(patch.object(m,'command',side_effect=cmd))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    def test_success(self):
        m.stage()
        self.assertEqual(json.loads(m.RECEIPT.read_text())['phase'],'STAGED_FOR_149_REBOOT')
        self.assertEqual(m.DROP.read_bytes(),m.DROP_DATA)
        self.assertEqual([x[:2] for x in self.commands],[('dkms','add'),('dkms','build'),('dkms','install'),
                         ('depmod','-a'),('update-initramfs','-u'),('systemctl','daemon-reload')])
        m.unchanged.assert_called_once_with({'curve':'unchanged'})
    def test_existing_state_refuses(self):
        m.STATE.mkdir()
        with self.assertRaises(ValueError):m.stage()
        self.assertEqual(self.commands,[])
    def test_framework_refusal_before_writes(self):
        m.known_configuration.side_effect=ValueError('custom hooks')
        with self.assertRaises(ValueError):m.stage()
        self.assertFalse(m.STATE.exists())
    def test_wrong_loaded_kernel_before_writes(self):
        m.active_147.side_effect=ValueError('wrong kernel')
        with self.assertRaises(ValueError):m.stage()
        self.assertFalse(m.STATE.exists())
    def test_missing_headers_before_writes(self):
        (self.root/'lib/modules'/g.TARGET/'build').rmdir()
        with self.assertRaises(ValueError):m.stage()
        self.assertFalse(m.STATE.exists())
    def test_source_collision_before_writes(self):
        g.SOURCE.mkdir()
        with self.assertRaises(ValueError):m.stage()
        self.assertEqual(self.commands,[])
    def test_initrd_verification_failure(self):
        m.check_initrd.side_effect=ValueError('wrong module')
        with self.assertRaises(ValueError):m.stage()
        self.assertFalse(m.DROP.exists())
        self.assertNotEqual(json.loads(m.RECEIPT.read_text())['phase'],'STAGED_FOR_149_REBOOT')
    def test_changed_pid_not_reported_success(self):
        m.active_147.side_effect=['5543','9999']
        with self.assertRaises(ValueError):m.stage()
        self.assertNotEqual(json.loads(m.RECEIPT.read_text())['phase'],'STAGED_FOR_149_REBOOT')
    def test_rollback_after_build_failure(self):
        self.fail=2
        with self.assertRaises(subprocess.CalledProcessError):m.stage()
        self.fail=None
        with patch.object(g,'object_bytes',return_value=elf()[0]):m.rollback()
        self.assertEqual(json.loads(m.RECEIPT.read_text())['phase'],'ROLLED_BACK_TO_147_BUNDLE')
        self.assertFalse(g.SOURCE.exists());self.assertFalse(g.LIB.exists())
    def test_rollback_modified_payload_refuses(self):
        m.stage();(g.SOURCE/'hp_smm_fan.c').write_text('changed')
        before=len(self.commands)
        with self.assertRaises(ValueError):m.rollback()
        self.assertEqual(len(self.commands),before)
    def test_rollback_additional_kernel_refuses(self):
        m.stage();self.dkms_status=g.NAME+'/'+g.VERSION+', 6.8.0-150-generic, x86_64: installed'
        before=len(self.commands)
        with self.assertRaises(ValueError):m.rollback()
        self.assertEqual(len(self.commands),before)
    def test_rollback_service_failure_keeps_recovery_state(self):
        m.stage();self.fail=len(self.commands)+4
        with patch.object(g,'object_bytes',return_value=elf()[0]):
            with self.assertRaises(subprocess.CalledProcessError):m.rollback()
        self.assertEqual(json.loads(m.RECEIPT.read_text())['phase'],'ROLLING_BACK')


class BootGuardTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for key,value in (('NOTE',self.root/'note'),('ABI',self.root/'control_abi'),
                          ('LIB',self.root/'new'),('OLDLIB',self.root/'old')):
            self.stack.enter_context(patch.object(g,key,value))
        g.LIB.mkdir();g.OLDLIB.mkdir()
        g.NOTE.write_bytes(b'known-note');g.ABI.write_text('2');g.ABI.with_name('min').write_text('24')
        (g.LIB/'fan-daemon.py').write_bytes((HERE/'source/fan-daemon.py').read_bytes())
        (g.OLDLIB/'start.py').write_bytes((HERE/'original_start.py.txt').read_bytes())
        self.stack.enter_context(patch.object(g,'platform_check'))
        self.stack.enter_context(patch.object(g,'secure'))
        self.stack.enter_context(patch.object(g,'disk_check',return_value=(b'known-note',Path('selected'))))
        self.uname=self.stack.enter_context(patch.object(g.os,'uname',return_value=SimpleNamespace(release=g.TARGET)))
    def test_149_success(self):self.assertEqual(g.identify({}),g.LIB/'fan-daemon.py')
    def test_loaded_mismatch(self):
        g.NOTE.write_bytes(b'old')
        with self.assertRaises(ValueError):g.identify({})
    def test_wrong_abi(self):
        g.ABI.write_text('1')
        with self.assertRaises(ValueError):g.identify({})
    def test_unknown_firmware_result(self):
        g.ABI.with_name('min').write_text('-2')
        with self.assertRaises(ValueError):g.identify({})
        g.disk_check.assert_not_called()
    def test_daemon_modified(self):
        (g.LIB/'fan-daemon.py').write_text('other')
        with self.assertRaises(ValueError):g.identify({})
    def test_platform_rejected_before_disk(self):
        g.platform_check.side_effect=ValueError('BIOS changed')
        with self.assertRaises(ValueError):g.identify({})
        g.disk_check.assert_not_called()
    def test_147_delegates_to_existing_selector(self):
        from types import SimpleNamespace
        self.uname.return_value=SimpleNamespace(release=g.KOLD)
        with patch.object(g,'OLD_NOTE',g.sha(b'known-note')):
            self.assertEqual(g.identify({}),g.OLDLIB/'start.py')
        g.disk_check.assert_not_called()
    def test_147_modified_selector_refused(self):
        from types import SimpleNamespace
        self.uname.return_value=SimpleNamespace(release=g.KOLD)
        (g.OLDLIB/'start.py').write_text('other')
        with self.assertRaises(ValueError):g.identify({})


def failure_test(index):
    def test(self):
        self.fail=index
        with self.assertRaises(subprocess.CalledProcessError):m.stage()
        phase=json.loads(m.RECEIPT.read_text())['phase']
        self.assertNotEqual(phase,'STAGED_FOR_149_REBOOT')
        if index<6:self.assertFalse(m.DROP.exists())
    return test
for i in range(1,7):setattr(StageFixture,'test_command_failure_'+str(i),failure_test(i))

if __name__=='__main__':unittest.main(verbosity=2)
