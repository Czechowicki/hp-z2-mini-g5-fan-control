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
import math
import os
import signal
import sys
import time

SET = "/sys/kernel/debug/hp_smm_fan/set"
CONFIG = "/etc/hp-fan.conf"
EXIT_REVIEW = 79  # hp-fan.service suppresses automatic restart on this exit.

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
                if len(parts) != 2:
                    return None
                t, p = int(parts[0]), int(parts[1])
                if not (0 <= t <= 120 and 0 <= p <= 100):
                    return None
                pts.append((t, p))
    except (OSError, ValueError):
        return None
    pts.sort()
    if any(t2 == t1 or p2 < p1 for (t1, p1), (t2, p2) in zip(pts, pts[1:])):
        return None
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
        with open(path) as sensor:
            raw = int(sensor.read().strip())
        return raw / 1000.0 if 0 <= raw <= 120000 else None
    except (OSError, ValueError):
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
    payload = str(value).encode("ascii")
    fd = os.open(SET, os.O_WRONLY | os.O_CLOEXEC)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("short write to fan control")
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=3.0, help="okres petli [s]")
    ap.add_argument("--step", type=int, default=5, help="histereza przy obnizaniu minimum [%%]")
    ap.add_argument("--config", default=CONFIG, help="plik krzywej")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0 or not 1 <= args.step <= 100:
        ap.error("require positive finite interval and step 1..100")

    if not os.path.exists(SET):
        sys.exit(f"brak {SET} - zaladuj modul: sudo modprobe hp_smm_fan")
    try:
        with open(os.path.join(os.path.dirname(SET), "control_abi")) as marker:
            compatible = marker.read().strip() == "2"
    except OSError:
        compatible = False
    if not compatible:
        print("Niezgodny zaladowany modul hp_smm_fan (wymagane control_abi=2).", file=sys.stderr)
        return EXIT_REVIEW
    tpath = coretemp_path()
    if not tpath:
        sys.exit("brak coretemp")

    applied = None
    try:
        cfg_mtime = os.stat(args.config).st_mtime_ns
    except FileNotFoundError:
        cfg_mtime = None
    curve = load_curve(args.config) if cfg_mtime is not None else DEFAULT_CURVE
    if not curve:
        print("Niepoprawna konfiguracja; sterowanie zatrzymane.", file=sys.stderr)
        return EXIT_REVIEW
    print(f"krzywa: {curve} (z {args.config if cfg_mtime else 'wbudowana'})", flush=True)

    stopping = False
    def request_stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    can_restore = False
    status = 0
    try:
        while not stopping:
            # hot-reload krzywej gdy plik sie zmienil
            try:
                m = os.stat(args.config).st_mtime_ns
            except OSError:
                m = None
            if m != cfg_mtime:
                new = load_curve(args.config)
                if new:
                    curve = new
                    print(f"wczytano nowa krzywa: {curve}", flush=True)
                elif m is not None:
                    print("plik krzywej nieprawidlowy - zostaje poprzednia", file=sys.stderr, flush=True)
                cfg_mtime = m

            temp = read_temp(tpath)
            if temp is None:
                print("Brak poprawnej temperatury CPU.", file=sys.stderr)
                status = EXIT_REVIEW
                break
            want = curve_percent(temp, curve)
            if stopping:
                break
            if applied is None or want > applied or applied - want >= args.step:
                can_restore = False  # Until this write has a known result.
                try:
                    write_set(want)
                except OSError as exc:
                    print(f"Sterowanie zatrzymane po bledzie zapisu: {exc}", file=sys.stderr)
                    status = EXIT_REVIEW
                    break
                can_restore = True
                applied = want
                if args.verbose:
                    print(f"{temp:.0f}C -> min {want}%", flush=True)
            if args.once:
                break
            due = time.monotonic() + args.interval
            while not stopping and (remaining := due - time.monotonic()) > 0:
                time.sleep(min(remaining, 0.25))
    finally:
        if can_restore and (not args.once or stopping):
            try:
                write_set("auto")
            except OSError as exc:
                print(f"Przywrocenie niepotwierdzone: {exc}", file=sys.stderr)
                status = EXIT_REVIEW
    return status


if __name__ == "__main__":
    sys.exit(main())
