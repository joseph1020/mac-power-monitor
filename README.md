# mac-power-monitor

Real-time macOS USB-C Power Delivery and battery incident monitor.

`mac-power-monitor.py` combines macOS power-source telemetry with WhatBattery and WhatCable to help distinguish normal charge-start behavior from USB-C / USB-PD instability.

## What it monitors

- macOS power source via `pmset`
- battery-side power, current, voltage, temperature, health, and adapter contract via WhatBattery
- negotiated USB-C PD contract and active port via WhatCable
- adapter wattage via `system_profiler`
- cross-checking of PD wattage sources
- AC ↔ Battery source flapping
- sustained battery discharge while external power is present
- delayed charge-current startup after AC/PD attachment

## Charging states

The monitor classifies the current condition as:

- `ON_BATTERY` — Mac is drawing from battery
- `CHARGING_ACTIVE` — battery-side charge power is positive
- `CHARGING_PENDING` — external power/PD is present but positive battery charge power has not appeared yet
- `AC_DISCHARGING` — external power is present while the battery is still supplying power
- `CHARGE_HOLD` — battery appears intentionally held/not charging
- `AC_CONNECTED` — external power is connected but active battery charging is not confirmed
- `UNKNOWN` — state cannot be resolved from current telemetry

A short delay between AC/PD attachment and positive battery-side charge power can be normal. During testing on a MacBookPro18,3, roughly 15–60 seconds was observed even though macOS recognized charging almost immediately.

## Warnings

- `PD SOURCES DISAGREE`
- `AC POWER FLAPPING`
- `BATTERY DRAINING ON AC`
- `CHARGING PENDING TOO LONG`

The default thresholds are deliberately conservative and can be adjusted near the top of the script.

## Requirements

macOS with Python 3 plus:

```bash
brew install whatcable-cli
brew install --cask darrylmorley/whatbattery/whatbattery
```

The script can fall back to IORegistry battery telemetry if WhatBattery is unavailable, but WhatCable is needed for USB-C PD/port diagnostics.

## Run

```bash
python3 mac-power-monitor.py
```

Stop with `Ctrl-C`.

Logs are written to:

```text
~/Library/Logs/mac-power-monitor/
```

Each session produces:

- `power_YYYYMMDD_HHMMSS.csv`
- `events_YYYYMMDD_HHMMSS.log`

## Physical port labels

Physical left/right labels are only mapped when the model is explicitly known. For `MacBookPro18,3`, labels are shown in Korean when the macOS locale is Korean and in English otherwise. Canonical CSV port identifiers remain locale-independent, such as `Port-USB-C@1`.

## Important interpretation notes

- `system_profiler` wattage and the WhatCable PD contract are negotiated adapter capability, **not instantaneous Mac power consumption**.
- WhatBattery `powerWatts` is treated as battery-side power, not total system input power.
- `AC_DISCHARGING` does not automatically mean hardware failure. Possible causes include insufficient adapter capacity, high system load, PD instability, or line loss.
- The monitor is diagnostic software, not a USB-PD protocol analyzer. Sub-second detach/reconnect events may be missed because macOS tools are polled synchronously.

## Background

This project grew out of troubleshooting a real intermittent USB-C charging incident in which macOS repeatedly switched between AC Power and Battery Power. Controlled testing showed stable operation with a known-good 1 m cable and repeatable flapping with a suspect 2 m cable across multiple USB-C ports. The monitor was built to make this class of failure visible in both live output and timestamped logs.

## License

MIT