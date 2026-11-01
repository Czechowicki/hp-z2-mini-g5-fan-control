#!/usr/bin/env python3
"""fan-daemon - demon krzywej wentylatora CPU dla HP Z2 Mini G5.

Polityka (krzywa temperatura->minimum %) w userspace; mechanizm (ustawienie
minimum na zywo przez SMM) w module jadra hp_smm_fan. Demon czyta temperature
CPU, wylicza docelowe minimum z krzywej i zapisuje je do debugfs tylko gdy sie
zmienia. Na wyjsciu oddaje AUTO.

KRZYWA: plik /etc/hp-fan.conf (prosty, edytowalny). Format - linie "temp %":
    50 20
    60 35
    ...
Zmiany w pliku wchodza automatycznie w ciagu kilku sekund, BEZ restartu demona.
Jesli pliku nie ma, uzywana jest krzywa domyslna ponizej.

Wymaga zaladowanego hp_smm_fan (/sys/kernel/debug/hp_smm_fan/set). Uruchom jako root.
"""
import argparse
import glob
import os
import signal
import sys
import time

SET = "/sys/kernel/debug/hp_smm_fan/set"
CONFIG = "/etc/hp-fan.conf"

# uzywana gdy brak /etc/hp-fan.conf. temp C -> min %. Miedzy punktami interpolacja.
DEFAULT_CURVE = [(50, 20), (60, 35), (70, 52), (80, 72), (87, 90), (93, 100)]


def load_curve(path):
    """Zwroc liste (temp, %) z pliku, albo None gdy brak/blad."""
    pts = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                t, p = int(parts[0]), int(parts[1])
                if 0 <= t <= 120 and 0 <= p <= 100:
                    pts.append((t, p))
    except (OSError, ValueError):
        return None
    pts.sort()
    return pts if len(pts) >= 2 else None


def coretemp_path():
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if open(os.path.join(h, "name")).read().strip() == "coretemp":
                return os.path.join(h, "temp1_input")
        except OSError:
            pass
    return None


def read_temp(path):
    try:
        return int(open(path).read().strip()) / 1000.0
    except OSError:
        return None


def curve_percent(temp, curve):
    if temp <= curve[0][0]:
        return curve[0][1]
    for i in range(1, len(curve)):
        t0, p0 = curve[i - 1]
        t1, p1 = curve[i]
        if temp < t1:
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return curve[-1][1]


def write_set(value):
    with open(SET, "w") as fh:
        fh.write(str(value))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0, help="okres petli [s]")
    ap.add_argument("--step", type=int, default=5, help="min. zmiana %% by przepisac (histereza)")
    ap.add_argument("--config", default=CONFIG, help="plik krzywej")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(SET):
        sys.exit(f"brak {SET} - zaladuj modul: sudo modprobe hp_smm_fan")
    tpath = coretemp_path()
    if not tpath:
        sys.exit("brak coretemp")

    applied = -999
    curve = load_curve(args.config) or DEFAULT_CURVE
    cfg_mtime = None
    try:
        cfg_mtime = os.path.getmtime(args.config)
    except OSError:
        pass
    print(f"krzywa: {curve} (z {args.config if cfg_mtime else 'wbudowana'})", flush=True)

    def restore(*_):
        try:
            write_set("auto")
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, restore)
    signal.signal(signal.SIGINT, restore)

    try:
        while True:
            # hot-reload krzywej gdy plik sie zmienil
            try:
                m = os.path.getmtime(args.config)
            except OSError:
                m = None
            if m != cfg_mtime:
                new = load_curve(args.config)
                if new:
                    curve = new
                    applied = -999  # wymus ponowne zastosowanie
                    print(f"wczytano nowa krzywa: {curve}", flush=True)
                elif m is not None:
                    print("plik krzywej nieprawidlowy - zostaje poprzednia", file=sys.stderr, flush=True)
                cfg_mtime = m

            temp = read_temp(tpath)
            if temp is not None:
                want = curve_percent(temp, curve)
                if abs(want - applied) >= args.step or (args.once and applied < 0):
                    try:
                        write_set(want)
                        applied = want
                        if args.verbose:
                            print(f"{temp:.0f}C -> min {want}%", flush=True)
                    except OSError as exc:
                        print(f"zapis nieudany: {exc}", file=sys.stderr, flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        if not args.once:
            try:
                write_set("auto")
            except OSError:
                pass


if __name__ == "__main__":
    main()
