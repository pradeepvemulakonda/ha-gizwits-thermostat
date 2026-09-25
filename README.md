# Gizwits Thermostat – Home Assistant integration

Fully **local**: no cloud account, no Gizwits app ID or token needed. Confirmed against a real
capture of the phone app switching a Radiant Australia floor-heating thermostat on and off.
The transport (Gizwits GAgent) is generic; the power/status byte layout below is specific to this
thermostat's firmware and hasn't been checked against other Gizwits-based devices.

> **What's confirmed vs. what's inferred**
>
> | Part | State |
> |---|---|
> | Local status (power, setpoint, floor/air temp, clock, weekday) | **Confirmed** from a real capture: the same request/reply the phone app uses. |
> | Local on/off | **Confirmed**: `0x93` sub-command `05 0a 01`/`05 0a 00`, acked by `0x94` with `06 0a <value>`, followed by the thermostat's own status push showing the flag changed. |
> | Instant push updates | **Confirmed**: the thermostat broadcasts its status by UDP (port 12414) on every change. |
> | `hvac_action` HEATING/IDLE (valve 1) | **Inferred**: flag bit 3 has turned on together with power in every capture; not yet isolated changing on its own. |
> | Setpoint / schedule changes | **Not supported** - a separate, larger read (`05 01 00`) returns a config/schedule block, but its write format hasn't been worked out. Setpoint is shown, read-only. |

## The protocol (for reference)

All frames: `00 00 00 03 | length | 00 | cmd(2 bytes) | payload`.

```
Login:      06 (passcode request) -> 07 (passcode)
            08 (login + passcode) -> 09 (result: last byte 0 = OK)

Read status: 93  seq(4 bytes) + 05 00 00     -> 94  seq + 06 <14-byte status>
Set power:   93  seq(4 bytes) + 05 0a <0|1>  -> 94  seq + 06 0a <0|1>
                                              -> (shortly after) 91 <14-byte status, unsolicited>

Status payload (14 bytes, byte 0 is always 0x06):
  [2]     flags: bit0 power, bit1 manual?, bit2 key-lock?, bit3 valve1, bit4 valve2
  [4:6]   air temperature x10
  [6:8]   setpoint x10
  [8]     program            [9] weekday (0 = Sunday)
  [10:11] device clock, BCD hour/minute (e.g. 0x22 0x09 = 22:09)
  [12:14] floor temperature x10
```

## Install via HACS (custom repository)

1. Push this folder to your own GitHub repository (public, so HACS can read it). Then, in
   `custom_components/gizwits_thermostat/manifest.json`, replace `YOUR_GITHUB_USERNAME` in the
   `documentation` / `issue_tracker` URLs with your GitHub username (repo: `ha-gizwits-thermostat`).
2. In Home Assistant: **HACS → the ⋮ menu (top right) → Custom repositories**.
3. Add your repository's URL, category **Integration**, then **Add**.
4. Find "Gizwits Thermostat (tested on Radiant Australia)" in HACS and **Download**.
5. Restart Home Assistant.
6. **Settings → Devices & services → Add integration → "Gizwits Thermostat"**, enter each thermostat's IP (add them separately if you have more than one).

(You can also skip HACS and copy `custom_components/gizwits_thermostat/` straight into your `config/custom_components/` folder, then restart - HACS just adds update tracking.)

**Close the Radiant phone app while adding a thermostat or testing** - the device only tolerates a few TCP clients and can go quiet if the app is still connected.

## Entities (per thermostat)

* `climate.<name>` – HEAT/OFF (turning on/off works), action HEATING/IDLE, current temperature = floor sensor, target = setpoint (read-only).
* Sensors: floor temperature, setpoint, air temperature; diagnostic (disabled by default): device clock, raw status.
* Binary sensors: power, valve 1.

Status updates arrive instantly by UDP push when Home Assistant is on the same network segment (VLAN) as the thermostats; a safety-net poll (default every 5 minutes, configurable in the integration's options) covers the rest. If UDP 12414 can't be bound (e.g. something else is already listening), it logs a warning and falls back to polling only.

## Advanced: `gizwits_thermostat.send_raw`

Sends a raw `0x93` sub-command over the local connection and logs the reply - useful while reverse-engineering the setpoint/schedule write. **Only use bytes you've verified from a real capture**; the thermostat's own datapoint schema treats the whole frame as one opaque blob, so nothing validates it for you.

## Troubleshooting

* *"cannot_connect" when adding*: force-stop the phone app, make sure no other script is connected, power-cycle the thermostat.
* *IP changed*: re-add it with the new IP - the existing entry is updated (matched by device ID).
* Enable debug logs: `logger: logs: custom_components.gizwits_thermostat: debug`.

## Disclaimer

This is an independent, community project based on reverse-engineering the local network
traffic of a Gizwits GAgent Wi-Fi thermostat (sold in Australia under the Radiant brand). It is
not affiliated with, endorsed by, or supported by Radiant or Gizwits. Use at your own risk -
sending an unverified raw command to the thermostat could change or corrupt its settings.

Licensed under the [MIT License](LICENSE).

## Tests

`python -m unittest tests.test_gizwits_lan -v` (no Home Assistant needed) - decodes real captured frames and exercises the LAN client (status read, power on/off, timeouts, bad acks) and the UDP broadcast listener against fakes built from real captured bytes.
