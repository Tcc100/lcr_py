"""
Measurement recording logic for the DE-5000 (no GUI code here).

MeterService reads the meter in a background thread, keeps a short history,
decides whether the reading is stable, records measurements (manually or
automatically into a list of pre-labeled entries) and autosaves them to CSV.
"""

import csv
import io
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import serial

if __package__:
    from .de5000 import DE5000, MEAS_RES, NORMALIZE_RULES
else:
    from de5000 import DE5000, MEAS_RES, NORMALIZE_RULES

DEMO_PORT = "demo"

DEFAULT_SETTINGS = {
    "window": 2.0,      # seconds the reading must stay inside the band
    "tolerance": 0.5,   # max spread, % of reading
    "counts": 2,        # ...or this many display counts, whichever is larger
    "rearm": 10.0,      # % change (or OL / blank) that means "part removed"
    "cmin": 10.0,       # pF; smaller capacitance readings are open-probe noise, not a part
}

CSV_FIELDS = [
    "id", "time", "label", "source", "entry",
    "quantity", "value", "units", "status", "value_si", "si_units",
    "sec_quantity", "sec_value", "sec_units", "sec_status", "sec_value_si", "sec_si_units",
    "freq", "circuit", "stable_for_s",
]


class RecorderError(Exception):
    pass


class SimulatedDE5000:
    """Imitates DE5000.get_meas(): parts are connected, settle, sit still
    for a while and are removed again. Useful for trying auto mode."""

    PARTS = [
        ("Cs", "nF", 100.0, 2, "D", "", 0.012, 3),
        ("Rs", "kOhm", 4.7, 3, "Theta", "deg", 0.4, 1),
        ("Ls", "uH", 220.0, 1, "Q", "", 18.5, 1),
        ("Cs", "uF", 10.0, 3, "ESR", "Ohm", 0.85, 2),
        ("Rs", "Ohm", 330.0, 1, "Theta", "deg", -0.2, 1),
    ]

    def __init__(self):
        self._part = None
        self._value = 0.0
        self._next_phase("open")

    def _next_phase(self, phase):
        self._phase = phase
        span = {"open": (2.0, 4.0), "settle": (1.5, 3.0), "steady": (4.0, 7.0)}[phase]
        self._phase_end = time.time() + random.uniform(*span)
        if phase == "settle":
            spec = random.choice(self.PARTS)
            self._part = (spec, spec[2] * random.uniform(0.95, 1.05))
            self._value = self._part[1] * random.uniform(0.6, 1.4)

    def get_meas(self):
        time.sleep(0.4)
        if time.time() >= self._phase_end:
            self._next_phase({"open": "settle", "settle": "steady", "steady": "open"}[self._phase])
        res = MEAS_RES.copy()
        res.update(freq="1 KHz", hold=False, auto_range=True, lcr_auto=True, data_valid=True)
        if self._phase == "open":
            # Open probes in LCR auto mode: a few pF of stray capacitance.
            res.update(main_quantity="Cs", main_status="normal", main_units="pF",
                       main_val=Decimal(f"{random.uniform(2.0, 4.5):.2f}"),
                       sec_quantity="D", sec_status="----", sec_units="", sec_val=Decimal("0"))
        else:
            (quantity, units, _, dec, sq, sunits, snom, sdec), target = self._part
            if self._phase == "settle":
                self._value += (target - self._value) * 0.45
                noise = target * 0.004
            else:
                self._value, noise = target, target * 0.0003
            res.update(main_quantity=quantity, main_status="normal", main_units=units,
                       main_val=Decimal(f"{self._value + random.gauss(0, noise):.{dec}f}"),
                       sec_quantity=sq, sec_status="normal", sec_units=sunits,
                       sec_val=Decimal(f"{snom + random.gauss(0, abs(snom) * 0.01):.{sdec}f}"))
        for p in ("main", "sec"):
            mult, norm_units = NORMALIZE_RULES[res[f"{p}_units"]]
            res[f"{p}_norm_val"], res[f"{p}_norm_units"] = res[f"{p}_val"] * mult, norm_units
        return res

    def close(self):
        pass


def _lsd_si(value, units):
    """Weight of the last displayed digit, in SI units."""
    if value is None or units not in NORMALIZE_RULES:
        return 0.0
    return float(Decimal(1).scaleb(value.as_tuple().exponent) * NORMALIZE_RULES[units][0])


def expand_labels(text):
    """One label per line (commas also split). 'C{1-12}' expands to C1..C12;
    'R{01-10}' keeps the zero padding."""
    labels = []
    for chunk in re.split(r"[\n,;]+", text or ""):
        pending = [chunk.strip()] if chunk.strip() else []
        while pending:
            item = pending.pop(0)
            m = re.search(r"\{(\d+)\s*-\s*(\d+)\}", item)
            if not m:
                labels.append(item)
                continue
            a, b = m.group(1), m.group(2)
            start, end = int(a), int(b)
            if abs(end - start) > 999:
                raise RecorderError(f"Range {m.group(0)} is too long (max 1000 entries).")
            width = len(a) if a.startswith("0") and len(a) > 1 else 0
            step = 1 if end >= start else -1
            pending = [item[:m.start()] + str(n).zfill(width) + item[m.end():]
                       for n in range(start, end + step, step)] + pending
    return labels


class MeterService:
    def __init__(self, port=None, out_dir="measurements"):
        self.lock = threading.RLock()
        self.port = port
        self._reconnect = threading.Event()
        self._stop = threading.Event()
        self.port_open = False
        self.error = None
        self.latest = None
        self.history = deque(maxlen=1200)
        self.stability = {"state": "none"}
        self.settings = dict(DEFAULT_SETTINGS)
        self.auto = {"enabled": False, "state": "off", "last_label": None, "finished": False,
                     "last_value": None, "last_key": None, "last_lsd": 0.0}
        self.recordings = []
        self.plan = []              # [{"id", "label", "rec_id"}]
        self.cursor = None          # id of the entry to fill next (None = first empty)
        self._next_id = 1
        self.live_version = 0       # bumps on every reading / status change
        self.data_version = 0       # bumps when recordings or entries change
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = (out_dir / f"de5000_{datetime.now():%Y%m%d_%H%M%S}.csv").resolve()

    # -- reader thread ------------------------------------------------------

    def start(self):
        threading.Thread(target=self._run, name="de5000-reader", daemon=True).start()

    def stop(self):
        self._stop.set()

    def connect(self, port):
        with self.lock:
            self.port = port or None
            self.error = None
            self._reconnect.set()
            self.live_version += 1

    def _run(self):
        meter = None
        while not self._stop.is_set():
            if self._reconnect.is_set():
                self._reconnect.clear()
                if meter is not None:
                    meter.close()
                    meter = None
                with self.lock:
                    self.history.clear()
                    self.latest = None
                self._set_link(False, None)
            if meter is None:
                port = self.port
                if not port:
                    self._stop.wait(0.3)
                    continue
                try:
                    meter = SimulatedDE5000() if port == DEMO_PORT else DE5000(port)
                    self._set_link(True, None)
                except (serial.SerialException, OSError, ValueError) as exc:
                    self._set_link(False, f"Could not open {port}: {exc}")
                    self._stop.wait(2.0)
                    continue
            try:
                data = meter.get_meas()
            except (serial.SerialException, OSError) as exc:
                meter.close()
                meter = None
                self._set_link(False, f"Lost connection to {self.port}: {exc}")
                self._ingest(MEAS_RES.copy())
                self._stop.wait(2.0)
                continue
            if not self._reconnect.is_set():
                self._ingest(data)
        if meter is not None:
            meter.close()

    def _set_link(self, port_open, error):
        with self.lock:
            if (self.port_open, self.error) != (port_open, error):
                self.port_open, self.error = port_open, error
                self.live_version += 1

    # -- processing ---------------------------------------------------------

    def _ingest(self, data):
        now = time.time()
        reading = {
            "t": now,
            "valid": bool(data.get("data_valid")),
            "freq": data.get("freq"),
            "tolerance": data.get("tolerance"),
            "hold": bool(data.get("hold")),
            "delta": bool(data.get("delta_mode")),
            "cal": bool(data.get("cal_mode")),
            "sorting": bool(data.get("sorting_mode")),
            "lcr_auto": bool(data.get("lcr_auto")),
            "auto_range": bool(data.get("auto_range")),
            "parallel": bool(data.get("parallel")),
        }
        for p in ("main", "sec"):
            val, si = data.get(f"{p}_val"), data.get(f"{p}_norm_val")
            reading[p] = {
                "quantity": data.get(f"{p}_quantity"),
                "value": None if val is None else format(val, "f"),
                "units": data.get(f"{p}_units"),
                "status": data.get(f"{p}_status"),
                "si": None if si is None else float(si),
                "si_units": data.get(f"{p}_norm_units"),
            }
        main = reading["main"]
        ok = reading["valid"] and main["status"] == "normal" and main["si"] is not None
        sample = {
            "t": now,
            "v": main["si"] if ok else None,
            "key": f"{main['quantity']}|{main['si_units']}|{reading['freq']}" if ok else None,
            "lsd": _lsd_si(data.get("main_val"), main["units"]) if ok else 0.0,
        }
        with self.lock:
            self.latest = reading
            self.history.append(sample)
            self.stability = self._compute_stability()
            self._auto_step()
            self.live_version += 1

    def _is_part(self, h):
        """False for no reading and for open-probe noise (tiny capacitance)."""
        if h is None or h["v"] is None:
            return False
        return not (h["key"].startswith("C") and abs(h["v"]) < self.settings["cmin"] * 1e-12)

    def _compute_stability(self):
        s = self.settings
        last = self.history[-1] if self.history else None
        if not self._is_part(last):
            below = last is not None and last["v"] is not None
            return {"state": "none", "reason": "below_cmin" if below else "no_value"}
        ref = last["v"]
        allowed = max(abs(ref) * s["tolerance"] / 100.0, s["counts"] * last["lsd"])
        lo = hi = ref
        start, samples = last["t"], 0
        # Longest run of recent readings (same quantity/range/freq) whose spread fits the band.
        for h in reversed(self.history):
            if not self._is_part(h) or h["key"] != last["key"]:
                break
            nlo, nhi = min(lo, h["v"]), max(hi, h["v"])
            if nhi - nlo > allowed:
                break
            lo, hi, start = nlo, nhi, h["t"]
            samples += 1
        stable_for = last["t"] - start
        recent = [h["v"] for h in self.history
                  if self._is_part(h) and h["key"] == last["key"] and last["t"] - h["t"] <= s["window"]]
        spread = max(recent) - min(recent) if recent else 0.0
        return {
            "state": "stable" if stable_for >= s["window"] and samples >= 3 else "settling",
            "stable_for": round(stable_for, 2),
            "progress": min(1.0, stable_for / s["window"]),
            "spread_pct": spread / abs(ref) * 100.0 if ref else None,
            "limit_pct": allowed / abs(ref) * 100.0 if ref else None,
        }

    def _entry(self, entry_id):
        return next((e for e in self.plan if e["id"] == entry_id), None)

    def next_entry(self):
        """The entry the next auto / triggered recording goes into."""
        with self.lock:
            entry = self._entry(self.cursor)
            if entry is not None:
                return entry
            return next((e for e in self.plan if e["rec_id"] is None), None)

    def _advance(self, entry):
        """Move the cursor to the next empty entry after `entry`, wrapping around."""
        i = self.plan.index(entry)
        order = self.plan[i + 1:] + self.plan[:i]
        self.cursor = next((e["id"] for e in order if e["rec_id"] is None), None)

    def _auto_step(self):
        a = self.auto
        if not a["enabled"]:
            a["state"] = "off"
            return
        last = self.history[-1] if self.history else None
        if a["state"] == "saved":
            # Wait until the part is removed (OL/blank/noise, different quantity, or a clear change).
            removed = not self._is_part(last) or last["key"] != a["last_key"]
            if not removed:
                ref = a["last_value"]
                limit = max(abs(ref) * self.settings["rearm"] / 100.0, 10 * a["last_lsd"])
                removed = abs(last["v"] - ref) > limit
            if not removed:
                return
            a["state"] = "armed"
        target = self.next_entry()
        if target is None:
            a.update(enabled=False, state="off", finished=True)
        elif self.stability["state"] == "stable":
            self._record_entry(target, "auto")
        else:
            a["state"] = "settling" if self.stability["state"] == "settling" else "armed"

    def _record_entry(self, entry, source):
        last = self.history[-1] if self.history else None
        rec = self._record(entry["label"], entry, source)
        self._advance(entry)
        a = self.auto
        a["last_label"] = rec["label"]
        if self.next_entry() is None:
            if a["enabled"]:
                a.update(enabled=False, state="off", finished=True)
        elif a["enabled"]:
            if self._is_part(last):
                a.update(state="saved", last_value=last["v"], last_key=last["key"], last_lsd=last["lsd"])
            else:
                a["state"] = "armed"
        self.data_version += 1
        return rec

    def record_entry_now(self):
        """Record the current reading into the next entry, ignoring the stability check."""
        with self.lock:
            target = self.next_entry()
            if target is None:
                raise RecorderError("There is no entry to record into. Add entries first, "
                                    "or select one and click 'Measure this next'.")
            rec = self._record_entry(target, "triggered")
            self.live_version += 1
            return rec

    # -- recordings ---------------------------------------------------------

    def _record(self, label, entry, source):
        r = self.latest
        if not r or not r["valid"]:
            raise RecorderError("No reading from the meter to record.")
        rec = {
            "id": self._next_id,
            "time": datetime.now().isoformat(timespec="seconds"),
            "label": (label or "").strip(),
            "source": source,
            "entry": entry["id"] if entry else None,
            "main": dict(r["main"]),
            "sec": dict(r["sec"]),
            "freq": r["freq"],
            "circuit": "parallel" if r["parallel"] else "serial",
            "stable_for": self.stability.get("stable_for"),
        }
        self._next_id += 1
        if entry is not None:
            if entry["rec_id"] is not None:
                self.recordings = [x for x in self.recordings if x["id"] != entry["rec_id"]]
            entry["rec_id"] = rec["id"]
        self.recordings.append(rec)
        self._changed()
        return rec

    def record(self, label=""):
        with self.lock:
            return self._record(label, None, "manual")

    def delete_recordings(self, rec_ids):
        rec_ids = set(rec_ids)
        with self.lock:
            self.recordings = [x for x in self.recordings if x["id"] not in rec_ids]
            for e in self.plan:
                if e["rec_id"] in rec_ids:
                    e["rec_id"] = None
            self._changed()

    def relabel(self, rec_id, label):
        with self.lock:
            for rec in self.recordings:
                if rec["id"] == rec_id:
                    rec["label"] = (label or "").strip()
            self._changed()

    def clear_recordings(self):
        self.delete_recordings([r["id"] for r in self.recordings])

    def set_plan(self, text):
        labels = expand_labels(text)
        with self.lock:
            for rec in self.recordings:
                rec["entry"] = None
            self.plan = []
            for label in labels:
                self.plan.append({"id": self._next_id, "label": label, "rec_id": None})
                self._next_id += 1
            self.cursor = None
            self.auto["finished"] = False
            self._changed()

    def select_entry(self, entry_id):
        """Make this entry the next one. A filled entry keeps its value until re-measured."""
        with self.lock:
            if self._entry(entry_id) is not None:
                self.cursor = entry_id
                self.auto["finished"] = False
                self.data_version += 1
                self.live_version += 1

    def set_auto(self, enabled=None, **settings):
        with self.lock:
            for key, value in settings.items():
                self.settings[key] = float(value)
            if enabled is not None:
                if enabled and not self.auto["enabled"]:
                    self.auto.update(state="armed", finished=False, last_label=None,
                                     last_value=None, last_key=None)
                self.auto["enabled"] = bool(enabled)
            self.stability = self._compute_stability()
            self._auto_step()
            self.live_version += 1

    # -- persistence --------------------------------------------------------

    def _changed(self):
        self.data_version += 1
        try:
            self.save_csv(self.csv_path)
        except RecorderError:
            pass  # reported through self.error

    def csv_text(self):
        with self.lock:
            labels = {e["id"]: e["label"] for e in self.plan}
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
            w.writeheader()
            for rec in self.recordings:
                m, s = rec["main"], rec["sec"]
                w.writerow({
                    "id": rec["id"], "time": rec["time"], "label": rec["label"],
                    "source": rec["source"], "entry": labels.get(rec["entry"], ""),
                    "quantity": m["quantity"], "value": m["value"], "units": m["units"],
                    "status": m["status"], "value_si": m["si"] if m["status"] == "normal" else "",
                    "si_units": m["si_units"],
                    "sec_quantity": s["quantity"], "sec_value": s["value"], "sec_units": s["units"],
                    "sec_status": s["status"], "sec_value_si": s["si"] if s["status"] == "normal" else "",
                    "sec_si_units": s["si_units"],
                    "freq": rec["freq"], "circuit": rec["circuit"],
                    "stable_for_s": "" if rec["stable_for"] is None else rec["stable_for"],
                })
            return buf.getvalue()

    def save_csv(self, path):
        path = Path(path)
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(self.csv_text(), encoding="utf-8", newline="")
            tmp.replace(path)
        except OSError as exc:
            self.error = f"Could not write {path}: {exc}"
            raise RecorderError(self.error)
