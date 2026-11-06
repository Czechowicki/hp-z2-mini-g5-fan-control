"""Exercise the SMM command and daemon error paths with RAM-backed fakes."""
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from unittest.mock import patch

source = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fan_daemon", source / "fan-daemon.py")
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


def kernel_check(temp):
    driver = (source / "hp_smm_fan.c").read_text()
    functions = []
    for name in ("smm_cmd_locked", "apply_setting", "hp_smm_fan_exit"):
        start = re.search(r"^static (?:int |void __exit )" + name + r"\(", driver, re.M)
        end = driver.index("\n}", start.start())
        functions.append(driver[start.start():end+2])
    stubs = """
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#define __exit
typedef unsigned char u8;
typedef unsigned short u16;
#define MIN_AUTO (-1)
#define MIN_UNKNOWN (-2)
#define OP_MIN 9
#define OP_RESTORE 10
#define HPSF_COUNT 0x26
#define HPSF_PAYLOAD 0x28
#define HPSF_PAYLOAD_MAX 126
#define HPSF_LEN 0xb0
#define HWM_LEN 0x100
#define HWM_BASE 0x900
#define REG_DOORBELL 0xfc0
#define pr_err(...) ((void)0)
#define udelay(us) ((void)0)
#define wmb() ((void)0)
#define debugfs_remove_recursive(dir) ((void)0)
#define acpi_os_unmap_memory(mem, len) ((void)0)
#define release_region(addr, len) ((void)0)
static u8 payload[HPSF_LEN], doorbell;
static int submissions, busy, acknowledge, locked;
static struct { u8 *hpsf; int cur_min, region, dir; } g = {payload, MIN_AUTO, 1, 1};
static void writeb(u8 v, u8 *p) { assert(locked); *p=v; }
static void writew(u16 v, u8 *p) { assert(locked); *p=v; p[1]=v>>8; }
static u8 hwm_r(int r) { assert(locked && r==REG_DOORBELL); return doorbell; }
static void doorbell_w(u8 op) { assert(locked); ++submissions; doorbell=op; }
static u8 sio_f7_read(void) { assert(locked); return 0; }
static void sio_f7_write(u8 v) { assert(locked); if (v==0 && acknowledge) doorbell=0; }
static int lock_hw(void) { assert(!locked); if (busy) return -EBUSY; locked=1; return 0; }
static void unlock_hw(void) { assert(locked); locked=0; }
"""
    cases = """
int main(void) {
    acknowledge=1;
    assert(apply_setting(60)==0 && submissions==1 && g.cur_min==60);
    assert(payload[HPSF_PAYLOAD]==60);
    busy=1;
    assert(apply_setting(50)==-EBUSY && submissions==1 && g.cur_min==60);
    busy=0; doorbell=8;
    assert(apply_setting(50)==-EBUSY && doorbell==8 && submissions==1 && g.cur_min==60);
    doorbell=0;
    assert(apply_setting(MIN_AUTO)==0 && submissions==2 && g.cur_min==MIN_AUTO);
    assert(payload[HPSF_PAYLOAD]==1);
    hp_smm_fan_exit(); assert(submissions==2);
    acknowledge=0;
    assert(apply_setting(50)==-ETIMEDOUT && submissions==3 && g.cur_min==MIN_UNKNOWN);
    assert(doorbell==OP_MIN);
    doorbell=0; acknowledge=1; /* Late firmware completion. */
    assert(apply_setting(60)==-EUCLEAN && apply_setting(MIN_AUTO)==-EUCLEAN);
    hp_smm_fan_exit(); assert(submissions==3);
    g.cur_min=50; acknowledge=0; /* Independent restore-timeout case. */
    assert(apply_setting(MIN_AUTO)==-ETIMEDOUT && doorbell==OP_RESTORE);
    assert(g.cur_min==MIN_UNKNOWN && submissions==4);
    hp_smm_fan_exit(); assert(submissions==4 && !locked);
    return 0;
}
"""
    code = temp / "transaction.c"
    binary = temp / "transaction"
    code.write_text(stubs + "\n".join(functions) + cases)
    subprocess.run(["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(code), "-o", str(binary)],check=True)
    subprocess.run([str(binary)],check=True)


def daemon_case(temp, temps, *, fail=None, stop_after=None, once=False, abi="2"):
    control = temp / "set"
    control.touch()
    (temp / "control_abi").write_text(abi)
    conf = temp / "curve"
    conf.write_text("50 50\n60 60\n")
    calls, handlers = [], {}
    def write(value):
        calls.append(value)
        if value == fail:
            raise OSError("injected failure")
        if len(calls) == stop_after:
            handlers[signal.SIGTERM](None, None)
    # All device and sensor I/O is directed to mocks or this temporary directory.
    options = ["fan-daemon.py", "--config", str(conf), "--interval", "0.00001"]
    if once:
        options.append("--once")
    with patch.object(daemon, "SET", str(control)), patch.object(daemon, "write_set", write), \
         patch.object(daemon, "coretemp_path", return_value="fixture"), \
         patch.object(daemon, "read_temp", side_effect=temps), patch.object(sys, "argv", options), \
         patch.object(signal, "signal", side_effect=lambda sig, cb:handlers.update({sig:cb})), \
         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        status = daemon.main()
    return status, calls


with tempfile.TemporaryDirectory() as d:
    temp = Path(d)
    kernel_check(temp)
    assert daemon_case(temp,[50,51,50,55],stop_after=3)==(0,[50,51,55,"auto"])
    assert daemon_case(temp,[50,51],fail=51)==(79,[50,51])
    assert daemon_case(temp,[50,None])==(79,[50,"auto"])
    assert daemon_case(temp,[50,None],fail="auto")==(79,[50,"auto"])
    assert daemon_case(temp,[50],once=True)==(0,[50])
    assert daemon_case(temp,[],abi="1")==(79,[])
    for text in ("40 20\ninvalid\n90 80\n", "40 20\n40 80\n", "40 80\n80 40\n"):
        conf=temp/"invalid"
        conf.write_text(text)
        assert daemon.load_curve(conf) is None
    assert "RestartPreventExitStatus=79" in (source/"hp-fan.service").read_text().splitlines()
print("PASS: compiled driver transactions in RAM, daemon error and signal paths, ABI, curve and service exit policy")
