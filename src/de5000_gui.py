#!/usr/bin/env python3
"""
Desktop UI for the DER DE-5000 LCR meter (Tkinter, standard library only).

    python3 src/de5000_gui.py [port]      e.g. /dev/ttyUSB0 or COM3
    python3 src/de5000_gui.py --demo      simulated meter

- Live reading with stability indicator
- Record measurements with an optional label (Enter in the label field)
- Auto mode: records into a list of pre-labeled entries whenever the
  reading has been stable, then waits for the part to be removed
- Recordings are autosaved to CSV; Export saves a copy elsewhere
"""

import argparse
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import serial.tools.list_ports as list_ports
except ImportError:
    list_ports = None

if __package__:
    from .de5000_recorder import DEMO_PORT, MeterService, RecorderError
else:
    from de5000_recorder import DEMO_PORT, MeterService, RecorderError

UNITS = {"Ohm": "Ω", "kOhm": "kΩ", "MOhm": "MΩ", "uH": "µH", "uF": "µF", "deg": "°"}
QUANTITIES = {"Theta": "θ", "RP": "Rp"}
POLL_MS = 150


def fmt_value(part):
    """'Cs  100.23 nF' style text for one display of a reading or recording."""
    if not part or part.get("status") is None:
        return ""
    quantity = QUANTITIES.get(part["quantity"], part["quantity"] or "")
    if part["status"] == "normal":
        text = f"{part['value']} {UNITS.get(part['units'], part['units'] or '')}".strip()
    elif part["status"] == "blank":
        text = ""
    else:
        text = part["status"]
    return f"{quantity}  {text}".strip()


def next_label(label):
    """R7 -> R8, C09 -> C10; labels without a trailing number stay unchanged."""
    m = re.search(r"(\d+)$", label)
    if not m:
        return label
    n = str(int(m.group(1)) + 1).zfill(len(m.group(1)))
    return label[:m.start()] + n


class App(ttk.Frame):
    def __init__(self, root, service):
        super().__init__(root, padding=8)
        self.root = root
        self.svc = service
        self._live_seen = -1
        self._data_seen = -1
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_connection()
        self._build_reading()
        self._build_controls()
        self._build_lists()

        root.bind("<Control-r>", lambda e: self.record())
        self.refresh_ports()
        if service.port:
            self.port_var.set(service.port)
        self.after(POLL_MS, self.poll)

    # -- layout -------------------------------------------------------------

    def _build_connection(self):
        f = ttk.Frame(self)
        f.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(f, text="Port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(f, textvariable=self.port_var, width=24)
        self.port_box.pack(side="left", padx=4)
        ttk.Button(f, text="Refresh", command=self.refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(f, text="Connect", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=4)
        self.link_var = tk.StringVar()
        ttk.Label(f, textvariable=self.link_var).pack(side="left", padx=8)

    def _build_reading(self):
        f = ttk.LabelFrame(self, text="Reading", padding=8)
        f.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        f.columnconfigure(0, weight=1)
        base = tkfont.nametofont("TkDefaultFont")
        big = base.copy()
        big.configure(size=max(28, base.cget("size") * 3), weight="bold")
        mid = base.copy()
        mid.configure(size=max(14, int(base.cget("size") * 1.5)))
        self.main_var = tk.StringVar(value="—")
        self.sec_var = tk.StringVar()
        self.info_var = tk.StringVar()
        self.stab_var = tk.StringVar()
        ttk.Label(f, textvariable=self.main_var, font=big).grid(row=0, column=0, sticky="w")
        ttk.Label(f, textvariable=self.sec_var, font=mid).grid(row=1, column=0, sticky="w")
        ttk.Label(f, textvariable=self.info_var).grid(row=2, column=0, sticky="w", pady=(4, 8))
        self.stab_bar = ttk.Progressbar(f, maximum=100)
        self.stab_bar.grid(row=3, column=0, sticky="ew")
        ttk.Label(f, textvariable=self.stab_var).grid(row=4, column=0, sticky="w")

    def _build_controls(self):
        f = ttk.Frame(self)
        f.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        f.columnconfigure(0, weight=1)

        rec = ttk.LabelFrame(f, text="Record", padding=8)
        rec.grid(row=0, column=0, sticky="ew")
        rec.columnconfigure(1, weight=1)
        ttk.Label(rec, text="Label (optional):").grid(row=0, column=0, sticky="w")
        self.label_var = tk.StringVar()
        self.label_entry = ttk.Entry(rec, textvariable=self.label_var)
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.label_entry.bind("<Return>", lambda e: self.record())
        ttk.Button(rec, text="Record", command=self.record).grid(row=0, column=2)
        self.autoinc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec, text="Increment trailing number after recording",
                        variable=self.autoinc_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        auto = ttk.LabelFrame(f, text="Auto mode", padding=8)
        auto.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(auto, text="Record into entries when the reading is stable",
                        variable=self.auto_var, command=self.apply_auto).grid(row=0, column=0, columnspan=4, sticky="w")
        self.auto_status = tk.StringVar(value="Off")
        ttk.Label(auto, textvariable=self.auto_status, wraplength=380).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 6))

        self.setting_vars = {}
        specs = [("window", "Stable for (s)", 0.2, 60, 0.5),
                 ("tolerance", "Max spread (%)", 0.01, 50, 0.1),
                 ("counts", "or display counts", 0, 1000, 1),
                 ("rearm", "Re-arm on change (%)", 0.1, 1000, 1)]
        for i, (key, text, lo, hi, inc) in enumerate(specs):
            var = tk.StringVar(value=f"{self.svc.settings[key]:g}")
            self.setting_vars[key] = (var, lo, hi)
            ttk.Label(auto, text=text).grid(row=2 + i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 4))
            sb = ttk.Spinbox(auto, textvariable=var, from_=lo, to=hi, increment=inc, width=7,
                             command=self.apply_auto)
            sb.grid(row=2 + i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 12), pady=1)
            sb.bind("<Return>", lambda e: self.apply_auto())
            sb.bind("<FocusOut>", lambda e: self.apply_auto())

    def _build_lists(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

        # Entries for auto mode
        ent = ttk.LabelFrame(pane, text="Entries", padding=6)
        ent.columnconfigure(0, weight=1)
        ent.rowconfigure(3, weight=1)
        ttk.Label(ent, text="One label per line. C{1-12} expands to C1 … C12.").grid(row=0, column=0, sticky="w")
        self.plan_text = tk.Text(ent, height=4, width=28, undo=True)
        self.plan_text.grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(ent, text="Set entries", command=self.set_plan).grid(row=2, column=0, sticky="w")
        self.plan_tree = ttk.Treeview(ent, columns=("next", "label", "value"), show="headings",
                                      selectmode="browse", height=8)
        for col, text, width in (("next", "", 24), ("label", "Label", 90), ("value", "Measured", 140)):
            self.plan_tree.heading(col, text=text)
            self.plan_tree.column(col, width=width, stretch=col != "next")
        self.plan_tree.grid(row=3, column=0, sticky="nsew", pady=(6, 2))
        self.plan_tree.bind("<Double-1>", lambda e: self.select_entry(redo=False))
        btns = ttk.Frame(ent)
        btns.grid(row=4, column=0, sticky="w")
        ttk.Button(btns, text="Measure next", command=lambda: self.select_entry(False)).pack(side="left")
        ttk.Button(btns, text="Redo", command=lambda: self.select_entry(True)).pack(side="left", padx=4)
        pane.add(ent, weight=1)

        # Recordings
        recs = ttk.LabelFrame(pane, text="Recordings", padding=6)
        recs.columnconfigure(0, weight=1)
        recs.rowconfigure(0, weight=1)
        cols = (("id", "#", 40), ("time", "Time", 70), ("label", "Label", 90), ("main", "Primary", 140),
                ("sec", "Secondary", 110), ("freq", "Freq", 60), ("circuit", "Circuit", 60),
                ("source", "Source", 60))
        self.rec_tree = ttk.Treeview(recs, columns=[c[0] for c in cols], show="headings")
        for col, text, width in cols:
            self.rec_tree.heading(col, text=text)
            self.rec_tree.column(col, width=width, stretch=col in ("label", "main", "sec"))
        scroll = ttk.Scrollbar(recs, orient="vertical", command=self.rec_tree.yview)
        self.rec_tree.configure(yscrollcommand=scroll.set)
        self.rec_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.rec_tree.bind("<Double-1>", lambda e: self.edit_label())
        self.rec_tree.bind("<Delete>", lambda e: self.delete_selected())
        btns = ttk.Frame(recs)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(btns, text="Edit label", command=self.edit_label).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.delete_selected).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear all", command=self.clear_all).pack(side="left")
        ttk.Button(btns, text="Export CSV…", command=self.export_csv).pack(side="left", padx=4)
        self.csv_var = tk.StringVar(value=f"Autosaving to {self.svc.csv_path}")
        ttk.Label(recs, textvariable=self.csv_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        pane.add(recs, weight=3)

    # -- actions ------------------------------------------------------------

    def refresh_ports(self):
        ports = [p.device for p in sorted(list_ports.comports(), key=lambda p: p.device)] if list_ports else []
        self.port_box["values"] = ports + [DEMO_PORT]
        if not self.port_var.get() and ports:
            self.port_var.set(ports[0])

    def toggle_connect(self):
        if self.svc.port:
            self.svc.connect(None)
        else:
            port = self.port_var.get().strip()
            if not port:
                messagebox.showinfo("Connect", "Pick or type a serial port first (or 'demo').")
                return
            self.svc.connect(port)

    def record(self):
        label = self.label_var.get()
        try:
            self.svc.record(label)
        except RecorderError as exc:
            messagebox.showwarning("Record", str(exc))
            return
        if label and self.autoinc_var.get():
            self.label_var.set(next_label(label))
        self.label_entry.select_range(0, "end")

    def apply_auto(self):
        values = {}
        for key, (var, lo, hi) in self.setting_vars.items():
            try:
                v = float(var.get())
            except ValueError:
                v = self.svc.settings[key]
            v = min(max(v, lo), hi)
            var.set(f"{v:g}")
            values[key] = v
        self.svc.set_auto(enabled=self.auto_var.get(), **values)

    def set_plan(self):
        text = self.plan_text.get("1.0", "end")
        if any(e["rec_id"] for e in self.svc.plan) and not messagebox.askyesno(
                "Set entries", "Replace the current entries? Their recordings stay in the list."):
            return
        try:
            self.svc.set_plan(text)
        except RecorderError as exc:
            messagebox.showwarning("Set entries", str(exc))

    def select_entry(self, redo):
        sel = self.plan_tree.selection()
        if sel:
            self.svc.select_entry(int(sel[0]), redo=redo)

    def _selected_recs(self):
        return [int(i) for i in self.rec_tree.selection()]

    def edit_label(self):
        ids = self._selected_recs()
        if not ids:
            return
        rec = next((r for r in self.svc.recordings if r["id"] == ids[0]), None)
        if rec is None:
            return
        label = simpledialog.askstring("Edit label", f"Label for recording #{rec['id']}:",
                                       initialvalue=rec["label"], parent=self.root)
        if label is not None:
            self.svc.relabel(rec["id"], label)

    def delete_selected(self):
        ids = self._selected_recs()
        if ids:
            self.svc.delete_recordings(ids)

    def clear_all(self):
        if self.svc.recordings and messagebox.askyesno(
                "Clear all", f"Delete all {len(self.svc.recordings)} recordings?"):
            self.svc.clear_recordings()

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=self.svc.csv_path.name)
        if path:
            try:
                self.svc.save_csv(path)
            except RecorderError as exc:
                messagebox.showerror("Export CSV", str(exc))

    # -- updates ------------------------------------------------------------

    def poll(self):
        if self.svc.live_version != self._live_seen:
            self._live_seen = self.svc.live_version
            self.update_live()
        if self.svc.data_version != self._data_seen:
            self._data_seen = self.svc.data_version
            self.update_data()
        self.after(POLL_MS, self.poll)

    def update_live(self):
        svc = self.svc
        with svc.lock:
            r, stab, auto = svc.latest, dict(svc.stability), dict(svc.auto)
            target = svc.next_entry()
            port, port_open, error = svc.port, svc.port_open, svc.error
            plan_len = len(svc.plan)

        self.connect_btn["text"] = "Disconnect" if port else "Connect"
        receiving = bool(r and r["valid"])
        if error:
            self.link_var.set(error)
        elif not port:
            self.link_var.set("Not connected")
        elif receiving:
            self.link_var.set(f"Receiving from {port}")
        elif port_open:
            self.link_var.set(f"{port} open, no data — check that the meter is on and the IR adapter is aligned")
        else:
            self.link_var.set(f"Opening {port}…")

        if receiving:
            self.main_var.set(fmt_value(r["main"]) or "—")
            self.sec_var.set(fmt_value(r["sec"]))
            flags = [r["freq"] or "", "parallel" if r["parallel"] else "serial"]
            flags += [name for key, name in (("hold", "HOLD"), ("auto_range", "auto range"),
                                             ("lcr_auto", "LCR auto"), ("delta", "Δ%"),
                                             ("cal", "calibration")) if r[key]]
            if r["sorting"]:
                flags.append(f"sorting {r['tolerance'] or ''}".strip())
            self.info_var.set(",  ".join(f for f in flags if f))
        else:
            self.main_var.set("—")
            self.sec_var.set("")
            self.info_var.set("")

        if stab["state"] == "none":
            self.stab_bar["value"] = 0
            self.stab_var.set("No value to check for stability")
        else:
            self.stab_bar["value"] = stab["progress"] * 100
            spread = "" if stab["spread_pct"] is None else (
                f" — spread {stab['spread_pct']:.2f} % (limit {stab['limit_pct']:.2f} %)")
            word = "Stable" if stab["state"] == "stable" else "Settling"
            self.stab_var.set(f"{word} for {stab['stable_for']:.1f} s{spread}")

        state = auto["state"]
        name = target["label"] if target else None
        self.auto_status.set({
            "off": "Off",
            "armed": f"Waiting for a part. Next entry: {name}",
            "settling": f"Settling… Next entry: {name}",
            "saved": f"Saved {auto['last_label']}. Remove the part to continue.",
            "done": "All entries measured." if plan_len else "No entries yet. Add labels under Entries.",
        }.get(state, state))

    def update_data(self):
        svc = self.svc
        with svc.lock:
            recs = list(svc.recordings)
            plan = [dict(e) for e in svc.plan]
            target = svc.next_entry()
            error = svc.error
        by_id = {r["id"]: r for r in recs}

        sel = self.plan_tree.selection()
        self.plan_tree.delete(*self.plan_tree.get_children())
        for e in plan:
            rec = by_id.get(e["rec_id"])
            mark = "▶" if target and e["id"] == target["id"] else ("✓" if rec else "")
            self.plan_tree.insert("", "end", iid=str(e["id"]),
                                  values=(mark, e["label"], fmt_value(rec["main"]) if rec else ""))
        sel = [s for s in sel if self.plan_tree.exists(s)]
        if sel:
            self.plan_tree.selection_set(sel)
        if target:
            self.plan_tree.see(str(target["id"]))

        sel = self.rec_tree.selection()
        self.rec_tree.delete(*self.rec_tree.get_children())
        for r in reversed(recs):
            self.rec_tree.insert("", "end", iid=str(r["id"]), values=(
                r["id"], r["time"][11:], r["label"], fmt_value(r["main"]), fmt_value(r["sec"]),
                r["freq"] or "", r["circuit"], r["source"]))
        sel = [s for s in sel if self.rec_tree.exists(s)]
        if sel:
            self.rec_tree.selection_set(sel)
        self.csv_var.set(error if error and "write" in error else f"Autosaving to {svc.csv_path}")


def main():
    parser = argparse.ArgumentParser(description="Desktop UI for the DER DE-5000 LCR meter.")
    parser.add_argument("port", nargs="?", help="serial port, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--demo", action="store_true", help="use a simulated meter")
    parser.add_argument("--out", default="measurements", help="folder for the autosaved CSV (default: %(default)s)")
    args = parser.parse_args()

    service = MeterService(DEMO_PORT if args.demo else args.port, args.out)
    service.start()
    root = tk.Tk()
    root.title("DE-5000")
    root.minsize(820, 560)
    App(root, service)
    try:
        root.mainloop()
    finally:
        service.stop()


if __name__ == "__main__":
    main()
