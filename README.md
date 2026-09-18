# mac-power-monitor

A real-time macOS USB-C power and battery incident monitor for diagnosing unstable charging, Power Delivery renegotiation, and unexpected battery drain.

The script combines `pmset`, `system_profiler`, WhatBattery, and WhatCable so macOS power-source state, negotiated USB-C PD capability, and battery-side power can be compared in one view.

## Why this exists

A Mac can recognise external power before positive battery charge current appears, and a charger can remain logically attached while the battery is still supplying power.

This monitor is designed to make those cases visible without treating every temporary charging delay as a fault.

It:

- tracks AC ↔ Battery source changes
- compares PD wattage reported by WhatCable, WhatBattery, and macOS
- distinguishes active charging, charge-start delay, charge hold, and battery discharge on AC
- records sustained battery drain while external power is present
- identifies the active USB-C port when WhatCable can resolve it
- keeps timestamped CSV telemetry and a separate event log
- avoids repeated warning spam by logging warning state changes rather than every refresh

## Requirements

- macOS
- Python 3
- WhatBattery
- WhatCable

Install the external tools with Homebrew:

```bash
brew install whatcable-cli
brew install --cask darrylmorley/whatbattery/whatbattery
```

If WhatBattery is unavailable, the script falls back to `AppleSmartBattery` IORegistry data for basic battery telemetry. WhatCable is still required for USB-C PD and active-port diagnostics.

## Usage

Run:

```bash
python3 mac-power-monitor.py
```

Stop with `Ctrl-C`.

The live view shows:

- macOS AC / Battery source
- `pmset` charging state
- charging-phase classification
- active USB-C port
- WhatCable PD voltage, current, and wattage
- WhatBattery adapter contract
- macOS adapter wattage
- battery voltage and current
- battery-side net power
- temperature, health, and cycle count
- source transitions within the last 30 seconds
- sustained battery-discharge duration while on AC

## Charging states

| State | Meaning |
|---|---|
| `ON_BATTERY` | Mac is drawing from the battery |
| `CHARGING_ACTIVE` | Battery-side charge power is positive |
| `CHARGING_PENDING` | External power / PD is present but positive battery charge power has not appeared yet |
| `AC_DISCHARGING` | External power is present while the battery is still supplying power |
| `CHARGE_HOLD` | Battery appears intentionally held or not charging |
| `AC_CONNECTED` | External power is present but active battery charging is not confirmed |
| `UNKNOWN` | Current telemetry is insufficient to classify the state |

A short delay between AC/PD attachment and positive battery-side charge power can be normal. During testing on a `MacBookPro18,3`, roughly 15–60 seconds was observed even though macOS recognised charging almost immediately.

## Warning logic

The monitor can raise:

- `PD SOURCES DISAGREE`
- `AC POWER FLAPPING`
- `BATTERY DRAINING ON AC`
- `CHARGING PENDING TOO LONG`

Default thresholds are defined near the top of the script.

Warnings use stable internal keys, so a sustained condition is logged once when it starts and again when it clears rather than once per refresh cycle.

## PD cross-check

The script compares available adapter / PD wattage from:

```text
WhatCable
WhatBattery
system_profiler
```

A small tolerance is allowed between sources.

These values describe negotiated adapter capability. They are **not** instantaneous Mac power consumption.

WhatBattery `powerWatts` is treated as battery-side power and should not be added directly to negotiated adapter wattage to estimate total system load.

## Logs

Each run creates timestamped logs under:

```text
~/Library/Logs/mac-power-monitor/
```

Files:

```text
power_YYYYMMDD_HHMMSS.csv
events_YYYYMMDD_HHMMSS.log
```

The CSV contains sampled telemetry for later comparison. The event log records meaningful source transitions, charging-phase changes, warnings, and warning clears.

## Physical port labels

Physical left/right labels are only shown for Mac models with an explicitly verified mapping.

For `MacBookPro18,3`:

- Korean locale: `왼쪽 USB-C 1`, `왼쪽 USB-C 2`, `오른쪽 USB-C`
- Other locales: `Left USB-C 1`, `Left USB-C 2`, `Right USB-C`

Canonical identifiers such as `Port-USB-C@1` remain locale-independent in the logs.

## Validation

The monitor was developed while investigating a real intermittent USB-C charging incident.

Controlled testing reproduced the failure pattern with a suspect 2 m USB-C cable across multiple Mac USB-C ports, while a known-good 1 m cable remained stable on the same charger and PD ports.

The monitor captured:

- repeated AC ↔ Battery switching
- PD contract loss and recovery
- battery discharge during source dropouts
- stable 60 W charging with a known-good cable
- delayed transition from AC recognition to positive battery charge power

## Limitations

- This is a diagnostic monitor, not a USB-PD protocol analyser.
- Telemetry is collected through macOS command-line tools and synchronous polling.
- Very short sub-second disconnect/reconnect events can be missed.
- WhatCable and macOS output formats may change in future versions.
- Physical left/right USB-C mapping is intentionally limited to explicitly verified Mac models.
- `AC_DISCHARGING` is not, by itself, proof of a hardware fault. High system load, insufficient adapter capacity, PD instability, or line loss can all produce battery discharge while on AC.
- A warning identifies an observed condition, not a definitive root cause.

## License

MIT. See [LICENSE](LICENSE).
