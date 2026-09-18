#!/usr/bin/env python3

import csv
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from shutil import which


# ============================================================
# CONFIG
# ============================================================

REFRESH = 1.0
ADAPTER_REFRESH = 5.0
WHATCABLE_REFRESH = 5.0

FLAP_WINDOW_SECONDS = 30
FLAP_WARNING_COUNT = 4

BATTERY_FLOW_EPSILON_W = 0.3

AC_DRAIN_WARNING_W = -2.0
AC_DRAIN_WARNING_SECONDS = 10.0

CHARGING_PENDING_WARNING_SECONDS = 90.0

PD_WATT_TOLERANCE = 1.0

LOG_DIR = os.path.expanduser("~/Library/Logs/mac-power-monitor")
os.makedirs(LOG_DIR, exist_ok=True)

SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_PATH = os.path.join(LOG_DIR, f"power_{SESSION_ID}.csv")
EVENT_PATH = os.path.join(LOG_DIR, f"events_{SESSION_ID}.log")


# ============================================================
# HELPERS
# ============================================================

def run(cmd, timeout=5):
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except Exception:
        return ""


def clear_screen():
    print("\033[2J\033[H", end="")


def fmt(value, suffix="", digits=2):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def log_event(message):
    timestamp = datetime.now().isoformat(timespec="seconds")
    with open(EVENT_PATH, "a", encoding="utf-8") as f:
        f.write(f"{timestamp}  {message}\n")


def get_mac_model():
    return run(["sysctl", "-n", "hw.model"], timeout=2) or "unknown"


def get_ui_language():
    """Detect locale only for physical-port display labels."""
    apple_locale = run(
        ["defaults", "read", "-g", "AppleLocale"],
        timeout=2,
    )
    if apple_locale:
        return "ko" if apple_locale.lower().startswith("ko") else "en"

    lang = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    ).lower()
    return "ko" if lang.startswith("ko") else "en"


MAC_MODEL = get_mac_model()
UI_LANGUAGE = get_ui_language()


# ============================================================
# WHATBATTERY
# ============================================================

def whatbattery_info():
    if which("whatbattery") is None:
        return None

    output = run(["whatbattery", "--json"], timeout=5)
    if not output:
        return None

    try:
        data = json.loads(output)
    except Exception:
        return None

    adapter = data.get("adapter") or {}

    return {
        "source": "WhatBattery",
        "capacity": data.get("currentChargePercent"),
        "charging_state": data.get("chargingState"),
        "voltage_mv": data.get("voltageMillivolts"),
        "current_ma": data.get("amperageMilliamps"),
        "instant_current_ma": data.get("instantAmperageMilliamps"),
        "power_w": data.get("powerWatts"),
        "temperature_c": data.get("temperatureCelsius"),
        "health_percent": data.get("healthPercent"),
        "cycle_count": data.get("cycleCount"),
        "critical": data.get("atCriticalLevel"),
        "time_to_full": data.get("timeToFullMinutes"),
        "time_to_empty": data.get("timeToEmptyMinutes"),
        "adapter_voltage_mv": adapter.get("voltageMV"),
        "adapter_current_ma": adapter.get("currentMA"),
        "adapter_watts": adapter.get("watts"),
        "adapter_wireless": adapter.get("isWireless"),
    }


# ============================================================
# IOREG FALLBACK
# ============================================================

def signed64(value):
    if value is None:
        return None
    if value >= 2**63:
        value -= 2**64
    return value


def get_ioreg_value(text, name):
    match = re.search(rf'"{re.escape(name)}"\s*=\s*(\d+)', text)
    return int(match.group(1)) if match else None


def ioreg_battery_info():
    s = run(["ioreg", "-r", "-c", "AppleSmartBattery", "-w0"])

    voltage_mv = get_ioreg_value(s, "Voltage")
    instant_ma = signed64(get_ioreg_value(s, "InstantAmperage"))
    capacity = get_ioreg_value(s, "CurrentCapacity")
    cycles = get_ioreg_value(s, "CycleCount")

    watts = None
    if voltage_mv is not None and instant_ma is not None:
        watts = voltage_mv * instant_ma / 1_000_000

    return {
        "source": "IORegistry fallback",
        "capacity": capacity,
        "charging_state": None,
        "voltage_mv": voltage_mv,
        "current_ma": None,
        "instant_current_ma": instant_ma,
        "power_w": watts,
        "temperature_c": None,
        "health_percent": None,
        "cycle_count": cycles,
        "critical": None,
        "time_to_full": None,
        "time_to_empty": None,
        "adapter_voltage_mv": None,
        "adapter_current_ma": None,
        "adapter_watts": None,
        "adapter_wireless": None,
    }


def battery_info():
    return whatbattery_info() or ioreg_battery_info()


# ============================================================
# PMSET
# ============================================================

def pmset_info():
    s = run(["pmset", "-g", "batt"])

    source = None
    state = None

    match = re.search(r"Now drawing from '([^']+)'", s)
    if match:
        source = match.group(1)

    lower = s.lower()

    # Order matters: "discharging" contains "charging".
    if "not charging" in lower:
        state = "NOT CHARGING"
    elif "discharging" in lower:
        state = "DISCHARGING"
    elif "charging" in lower:
        state = "CHARGING"
    elif "charged" in lower:
        state = "CHARGED"
    elif "finishing charge" in lower:
        state = "FINISHING CHARGE"

    return {"source": source, "state": state, "raw": s}


# ============================================================
# SYSTEM PROFILER ADAPTER
# ============================================================

def adapter_info():
    s = run(["system_profiler", "SPPowerDataType"], timeout=8)

    lines = s.splitlines()
    block = []
    inside = False

    for line in lines:
        if "AC Charger Information:" in line:
            inside = True
            continue

        if inside:
            if re.match(r"^\s{4}\S.*:$", line) and "AC Charger" not in line:
                break
            block.append(line)

    text = "\n".join(block)

    def field(pattern):
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None

    watts = field(r"Wattage \(W\):\s*(\d+)")

    return {
        "watts": int(watts) if watts else None,
        "connected": field(r"Connected:\s*(\w+)"),
        "charging": field(r"Charging:\s*(\w+)"),
        "id": field(r"ID:\s*([^\n]+)"),
        "family": field(r"Family:\s*([^\n]+)"),
    }


# ============================================================
# PHYSICAL PORT MAPPING
# ============================================================

def normalize_port(raw_port):
    if not raw_port:
        return None

    match = re.match(r"^(Port-(?:USB-C|MagSafe 3)@\d+)", raw_port)
    return match.group(1) if match else raw_port


def physical_port_name(raw_port):
    normalized = normalize_port(raw_port)

    # Confirmed physical mapping only for MacBookPro18,3.
    if MAC_MODEL == "MacBookPro18,3":
        if UI_LANGUAGE == "ko":
            mapping = {
                "Port-MagSafe 3@1": "왼쪽 MagSafe 3",
                "Port-USB-C@1": "왼쪽 USB-C 1",
                "Port-USB-C@2": "왼쪽 USB-C 2",
                "Port-USB-C@3": "오른쪽 USB-C",
            }
        else:
            mapping = {
                "Port-MagSafe 3@1": "Left MagSafe 3",
                "Port-USB-C@1": "Left USB-C 1",
                "Port-USB-C@2": "Left USB-C 2",
                "Port-USB-C@3": "Right USB-C",
            }

        return mapping.get(normalized, normalized or "unknown")

    # Do not guess physical orientation on unverified Mac models.
    return normalized or "unknown"


# ============================================================
# WHATCABLE
# ============================================================

def whatcable_info():
    if which("whatcable") is None:
        return {}

    s = run(["whatcable"], timeout=5)
    if not s:
        return {}

    sections = re.split(r"(?=^=== .+ ===$)", s, flags=re.M)
    candidates = []

    for section in sections:
        header = re.search(r"^=== (.+) ===$", section, re.M)
        if not header or "Nothing connected" in section:
            continue

        raw_port = header.group(1).strip()
        lower = section.lower()
        score = 0

        if "currently negotiated" in lower:
            score += 100
        if "charger" in lower:
            score += 50
        if "power is flowing" in lower:
            score += 20
        if "charging" in lower:
            score += 20
        if "plugged in" in lower:
            score += 5

        if score > 0:
            candidates.append((score, raw_port, section))

    if not candidates:
        return {}

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, raw_port, active_section = candidates[0]

    result = {
        "raw_port": raw_port,
        "normalized_port": normalize_port(raw_port),
        "physical_port": physical_port_name(raw_port),
    }

    negotiated = re.search(
        r"Currently negotiated:\s*"
        r"([0-9.]+)V\s*@\s*"
        r"([0-9.]+)A\s*"
        r"\((\d+)W\)",
        active_section,
    )

    advertised = re.search(
        r"Charger advertises up to\s*(\d+)W",
        active_section,
    )

    device = re.search(r"Connected device:\s*(.+)", active_section)

    if negotiated:
        result["voltage_v"] = float(negotiated.group(1))
        result["current_a"] = float(negotiated.group(2))
        result["watts"] = float(negotiated.group(3))

    if advertised:
        result["advertised"] = float(advertised.group(1))

    if device:
        result["device"] = device.group(1).strip()

    lower = active_section.lower()

    if "charging on hold" in lower:
        result["state"] = "HOLD"
    elif "charging well" in lower:
        result["state"] = "CHARGING WELL"
    elif "power is flowing" in lower:
        result["state"] = "POWER FLOWING"
    elif "plugged in" in lower:
        result["state"] = "PLUGGED IN"

    if "no e-marker detected" in lower:
        result["emarker"] = "not detected"

    return result


# ============================================================
# POWER SOURCE FLAP DETECTOR
# ============================================================
power_source_history = deque()
last_power_source = None


def update_flap_detector(source):
    global last_power_source

    now = time.monotonic()
    previous_source = last_power_source

    changed = bool(
        source
        and previous_source
        and source != previous_source
    )

    if changed:
        power_source_history.append((now, previous_source, source))
        log_event(
            "POWER SOURCE CHANGE: "
            f"{previous_source} -> {source}"
        )

    if source:
        last_power_source = source

    while (
        power_source_history
        and now - power_source_history[0][0] > FLAP_WINDOW_SECONDS
    ):
        power_source_history.popleft()

    return len(power_source_history), previous_source, changed


# ============================================================
# PD CROSS CHECK
# ============================================================

def pd_agreement(wc, battery, adapter, pm_source):
    # Never report PD agreement while macOS says it is on battery.
    if pm_source != "AC Power":
        return None, []

    values = []

    if wc.get("watts") is not None:
        values.append(("WhatCable", float(wc["watts"])))

    if battery.get("adapter_watts") is not None:
        values.append(("WhatBattery", float(battery["adapter_watts"])))

    if adapter.get("watts") is not None:
        values.append(("macOS", float(adapter["watts"])))

    if len(values) < 2:
        return None, values

    wattages = [value for _, value in values]
    agreed = max(wattages) - min(wattages) <= PD_WATT_TOLERANCE
    return agreed, values


# ============================================================
# CHARGING PHASE MODEL
# ============================================================

charging_pending_started = None
last_logged_phase = None


def determine_charging_phase(pm, battery, wc):
    global charging_pending_started
    global last_logged_phase

    now = time.monotonic()

    source = pm.get("source")
    pm_state = pm.get("state")
    wb_state = (battery.get("charging_state") or "").lower()
    bw = battery.get("power_w")
    capacity = battery.get("capacity")

    pd_present = (
        wc.get("watts") is not None
        or battery.get("adapter_watts") is not None
    )

    charging_signal = (
        wb_state == "charging"
        or pm_state in ("CHARGING", "FINISHING CHARGE")
    )

    obvious_hold = (
        pm_state == "CHARGED"
        or (capacity is not None and capacity >= 99)
        or wc.get("state") == "HOLD"
        or (
            capacity is not None
            and capacity >= 79
            and (
                pm_state == "NOT CHARGING"
                or wb_state == "acnocharge"
            )
        )
    )

    if source != "AC Power":
        charging_pending_started = None
        phase = "ON_BATTERY"

    elif bw is not None and bw < -BATTERY_FLOW_EPSILON_W:
        charging_pending_started = None
        phase = "AC_DISCHARGING"

    elif bw is not None and bw > BATTERY_FLOW_EPSILON_W:
        charging_pending_started = None
        phase = "CHARGING_ACTIVE"

    elif obvious_hold:
        charging_pending_started = None
        phase = "CHARGE_HOLD"

    elif (
        source == "AC Power"
        and pd_present
        and (
            charging_signal
            or (capacity is not None and capacity < 79)
        )
    ):
        if charging_pending_started is None:
            charging_pending_started = now
        phase = "CHARGING_PENDING"

    elif source == "AC Power" and pd_present:
        charging_pending_started = None
        phase = "AC_CONNECTED"

    else:
        charging_pending_started = None
        phase = "UNKNOWN"

    pending_seconds = 0.0
    if phase == "CHARGING_PENDING" and charging_pending_started is not None:
        pending_seconds = now - charging_pending_started

    meaningful_phases = {
        "CHARGING_ACTIVE",
        "CHARGING_PENDING",
        "AC_DISCHARGING",
        "CHARGE_HOLD",
    }

    if phase != last_logged_phase and phase in meaningful_phases:
        log_event(
            "CHARGING PHASE: "
            f"{last_logged_phase or 'NONE'} -> {phase}"
        )

    last_logged_phase = phase
    return phase, pending_seconds


# ============================================================
# CSV
# ============================================================

CSV_COLUMNS = [
    "timestamp",
    "mac_model",
    "pmset_source",
    "pmset_state",
    "charging_phase",
    "charging_pending_seconds",
    "battery_source",
    "battery_percent",
    "battery_state",
    "battery_power_w",
    "battery_voltage_v",
    "battery_current_a",
    "battery_instant_current_a",
    "battery_temperature_c",
    "battery_health_percent",
    "battery_cycles",
    "physical_port",
    "whatcable_port",
    "system_profiler_adapter_w",
    "whatbattery_adapter_voltage_v",
    "whatbattery_adapter_current_a",
    "whatbattery_adapter_w",
    "whatcable_advertised_w",
    "whatcable_pd_voltage_v",
    "whatcable_pd_current_a",
    "whatcable_pd_contract_w",
    "whatcable_state",
    "pd_agreement",
    "source_transitions_30s",
    "ac_drain_seconds",
]

csv_file = open(CSV_PATH, "a", newline="", encoding="utf-8")
csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
csv_writer.writeheader()
csv_file.flush()


# ============================================================
# RUNTIME STATE
# ============================================================

last_adapter_time = 0.0
last_whatcable_time = 0.0

adapter = {}
wc = {}

ac_drain_started = None
ac_drain_seconds = 0.0

active_warning_keys = set()


# ============================================================
# MAIN
# ============================================================

try:
    log_event("MONITOR STARTED")
    log_event(f"MAC MODEL: {MAC_MODEL}")

    while True:
        loop_started = time.monotonic()

        battery = battery_info()
        pm = pmset_info()
        now = time.monotonic()

        flap_count, previous_source, source_changed = update_flap_detector(
            pm["source"]
        )

        just_attached = (
            pm["source"] == "AC Power"
            and previous_source not in (None, "AC Power")
        )

        if pm["source"] != "AC Power":
            adapter = {}
            wc = {}
            last_adapter_time = 0.0
            last_whatcable_time = 0.0
        else:
            if just_attached:
                adapter = {}
                wc = {}
                last_adapter_time = 0.0
                last_whatcable_time = 0.0

            if now - last_adapter_time >= ADAPTER_REFRESH:
                adapter = adapter_info()
                last_adapter_time = time.monotonic()

            if now - last_whatcable_time >= WHATCABLE_REFRESH:
                wc = whatcable_info()
                last_whatcable_time = time.monotonic()

        bw = battery.get("power_w")

        if (
            pm["source"] == "AC Power"
            and bw is not None
            and bw <= AC_DRAIN_WARNING_W
        ):
            if ac_drain_started is None:
                ac_drain_started = time.monotonic()
            ac_drain_seconds = time.monotonic() - ac_drain_started
        else:
            ac_drain_started = None
            ac_drain_seconds = 0.0

        pd_ok, pd_values = pd_agreement(
            wc,
            battery,
            adapter,
            pm["source"],
        )

        charging_phase, charging_pending_seconds = determine_charging_phase(
            pm,
            battery,
            wc,
        )

        clear_screen()

        print("=" * 86)
        print(" macOS Power / USB-C Incident Monitor")
        print(" " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 86)

        print()
        print("SYSTEM")
        print(f"  Mac model             : {MAC_MODEL}")

        print()
        print("POWER SOURCE")
        print(f"  macOS source          : {pm['source'] or 'n/a'}")
        print(f"  pmset state           : {pm['state'] or 'n/a'}")
        print(f"  Charging phase        : {charging_phase}")

        if charging_phase == "CHARGING_PENDING":
            print(f"  Pending duration      : {charging_pending_seconds:.1f} s")

        print(f"  Adapter connected     : {adapter.get('connected') or 'n/a'}")
        print(f"  Adapter charging flag : {adapter.get('charging') or 'n/a'}")

        print()
        print("PORT / USB-C / PD")

        if wc:
            print(
                f"  Physical port         : "
                f"{wc.get('physical_port', 'n/a')} "
                f"({wc.get('normalized_port', 'n/a')})"
            )

            if wc.get("device"):
                print(f"  Connected device      : {wc['device']}")

            if wc.get("emarker"):
                print(f"  Cable e-marker        : {wc['emarker']}")
        else:
            print("  Physical port         : not currently resolved")

        print(
            f"  macOS adapter         : "
            f"{fmt(adapter.get('watts'), ' W', 0)}"
        )

        wb_av = battery.get("adapter_voltage_mv")
        wb_ai = battery.get("adapter_current_ma")
        wb_aw = battery.get("adapter_watts")

        if wb_av is not None and wb_ai is not None:
            print(
                f"  WhatBattery adapter   : "
                f"{wb_av / 1000:.2f} V × "
                f"{wb_ai / 1000:.2f} A "
                f"= {fmt(wb_aw, ' W', 0)}"
            )
        else:
            print("  WhatBattery adapter   : n/a")

        if wc.get("watts") is not None:
            print(
                f"  WhatCable PD contract : "
                f"{wc['voltage_v']:.2f} V × "
                f"{wc['current_a']:.2f} A "
                f"= {wc['watts']:.0f} W"
            )
        else:
            print("  WhatCable PD contract : n/a")

        print(
            f"  Advertised maximum    : "
            f"{fmt(wc.get('advertised'), ' W', 0)}"
        )
        print(f"  WhatCable state       : {wc.get('state', 'n/a')}")

        print()
        print("BATTERY")
        print(f"  Telemetry source      : {battery['source']}")
        print(
            f"  State of charge       : "
            f"{fmt(battery.get('capacity'), '%', 0)}"
        )
        print(
            f"  WhatBattery state     : "
            f"{battery.get('charging_state') or 'n/a'}"
        )

        voltage_v = (
            battery["voltage_mv"] / 1000
            if battery.get("voltage_mv") is not None
            else None
        )
        current_a = (
            battery["current_ma"] / 1000
            if battery.get("current_ma") is not None
            else None
        )
        instant_a = (
            battery["instant_current_ma"] / 1000
            if battery.get("instant_current_ma") is not None
            else None
        )

        print(f"  Voltage               : {fmt(voltage_v, ' V', 3)}")
        print(f"  Gauge current         : {fmt(current_a, ' A', 3)}")        print(f"  Instant current       : {fmt(instant_a, ' A', 3)}")

        if bw is None:
            battery_power_text = "UNKNOWN"
        elif bw > BATTERY_FLOW_EPSILON_W:
            battery_power_text = f"CHARGING +{bw:.2f} W"
        elif bw < -BATTERY_FLOW_EPSILON_W:
            battery_power_text = f"DISCHARGING {bw:.2f} W"
        else:
            battery_power_text = f"NEAR HOLD {bw:+.2f} W"

        print(f"  Battery net power     : {battery_power_text}")
        print(
            f"  Temperature           : "
            f"{fmt(battery.get('temperature_c'), ' °C', 1)}"
        )
        print(
            f"  Battery health        : "
            f"{fmt(battery.get('health_percent'), '%', 1)}"
        )
        print(
            f"  Cycle count           : "
            f"{fmt(battery.get('cycle_count'), '', 0)}"
        )

        if battery.get("time_to_full") is not None:
            mins = battery["time_to_full"]
            print(f"  Time to full          : {mins // 60}h {mins % 60}m")

        if battery.get("time_to_empty") is not None:
            mins = battery["time_to_empty"]
            print(f"  Time to empty         : {mins // 60}h {mins % 60}m")

        print()
        print("DIAGNOSTICS")

        warnings = []

        if pd_ok is True:
            pd_text = "YES"
        elif pd_ok is False:
            pd_text = "NO"
            warnings.append(("PD_DISAGREE", "PD SOURCES DISAGREE"))
        else:
            pd_text = "INSUFFICIENT DATA"

        print(f"  PD sources agree      : {pd_text}")

        if pd_values:
            pd_detail = " | ".join(
                f"{name}={watts:.0f}W"
                for name, watts in pd_values
            )
            print(f"  PD comparison         : {pd_detail}")

        print(f"  Source changes / 30s  : {flap_count}")

        if flap_count >= FLAP_WARNING_COUNT:
            warnings.append(
                (
                    "FLAPPING",
                    f"AC POWER FLAPPING ({flap_count} changes / 30s)",
                )
            )

        print(f"  AC drain duration     : {ac_drain_seconds:.1f} s")

        if ac_drain_seconds >= AC_DRAIN_WARNING_SECONDS:
            warnings.append(
                (
                    "AC_DRAIN",
                    "BATTERY DRAINING ON AC "
                    f"({bw:.2f} W for {ac_drain_seconds:.1f}s)",
                )
            )

        if charging_phase == "CHARGING_PENDING":
            print(f"  Charge start delay    : {charging_pending_seconds:.1f} s")

            if charging_pending_seconds >= CHARGING_PENDING_WARNING_SECONDS:
                warnings.append(
                    (
                        "PENDING_TOO_LONG",
                        "CHARGING PENDING TOO LONG "
                        f"({charging_pending_seconds:.1f}s)",
                    )
                )

        current_warning_keys = {key for key, _ in warnings}

        if warnings:
            print()
            for key, message in warnings:
                print(f"  !!! {message}")
                if key not in active_warning_keys:
                    log_event("WARNING: " + message)
        else:
            print("  Status                 : No immediate fault detected")

        cleared_warning_keys = active_warning_keys - current_warning_keys
        for key in sorted(cleared_warning_keys):
            log_event(f"WARNING CLEARED: {key}")

        active_warning_keys = current_warning_keys

        print()
        print("INTERPRETATION")

        if charging_phase == "ON_BATTERY":
            print("  Mac is running from battery.")

        elif charging_phase == "CHARGING_ACTIVE":
            print(f"  Battery is actively receiving {bw:.2f} W.")

        elif charging_phase == "CHARGING_PENDING":
            print("  External power and a PD source are present,")
            print("  but positive battery charge power has not appeared yet.")
            print("  A 15–60 s startup delay has been observed on this Mac.")

        elif charging_phase == "CHARGE_HOLD":
            print(
                "  External power is present and the battery appears "
                "to be intentionally held/not charging."
            )

        elif charging_phase == "AC_CONNECTED":
            print(
                "  External power is connected, but active battery "
                "charging is not currently confirmed."
            )

        elif charging_phase == "AC_DISCHARGING":
            print(
                f"  External power is present while the battery is supplying "
                f"{abs(bw):.2f} W."
            )
            print(
                "  This can indicate insufficient adapter capacity, "
                "heavy system load, PD instability, or line loss."
            )

        else:
            print("  Charging state is not fully resolved.")

        if flap_count >= FLAP_WARNING_COUNT:
            print("  Repeated AC/Battery source switching detected.")
            print(
                "  Check charger, cable, connector, and USB-C PD stability."
            )

        csv_writer.writerow(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "mac_model": MAC_MODEL,
                "pmset_source": pm["source"],
                "pmset_state": pm["state"],
                "charging_phase": charging_phase,
                "charging_pending_seconds": round(charging_pending_seconds, 3),
                "battery_source": battery["source"],
                "battery_percent": battery.get("capacity"),
                "battery_state": battery.get("charging_state"),
                "battery_power_w": bw,
                "battery_voltage_v": voltage_v,
                "battery_current_a": current_a,
                "battery_instant_current_a": instant_a,
                "battery_temperature_c": battery.get("temperature_c"),
                "battery_health_percent": battery.get("health_percent"),
                "battery_cycles": battery.get("cycle_count"),
                "physical_port": wc.get("physical_port"),
                "whatcable_port": wc.get("normalized_port"),
                "system_profiler_adapter_w": adapter.get("watts"),
                "whatbattery_adapter_voltage_v": (
                    wb_av / 1000 if wb_av is not None else None
                ),
                "whatbattery_adapter_current_a": (
                    wb_ai / 1000 if wb_ai is not None else None
                ),
                "whatbattery_adapter_w": wb_aw,
                "whatcable_advertised_w": wc.get("advertised"),
                "whatcable_pd_voltage_v": wc.get("voltage_v"),
                "whatcable_pd_current_a": wc.get("current_a"),
                "whatcable_pd_contract_w": wc.get("watts"),
                "whatcable_state": wc.get("state"),
                "pd_agreement": pd_ok,
                "source_transitions_30s": flap_count,
                "ac_drain_seconds": round(ac_drain_seconds, 3),
            }
        )
        csv_file.flush()

        print()
        print(f"  CSV log   : {CSV_PATH}")
        print(f"  Event log : {EVENT_PATH}")
        print()
        print(
            "  Refresh target: battery/source 1s | "
            "adapter 5s | WhatCable 5s"
        )
        print("  Ctrl-C to stop")
        print("=" * 86)

        elapsed = time.monotonic() - loop_started
        time.sleep(max(0.05, REFRESH - elapsed))

except KeyboardInterrupt:
    log_event("MONITOR STOPPED")
    csv_file.close()
    print()
    print("Stopped.")
    print(f"CSV   : {CSV_PATH}")
    print(f"Events: {EVENT_PATH}")
    sys.exit(0)

except Exception as exc:
    log_event(f"MONITOR CRASHED: {type(exc).__name__}: {exc}")
    try:
        csv_file.close()
    except Exception:
        pass
    raise