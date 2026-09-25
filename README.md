# Gizwits Thermostat – Home Assistant Local Integration

[![hacs_badge](https://shields.io)](https://github.com)
[![License: MIT](https://shields.io)](https://opensource.org)

A fully **local network** integration for Home Assistant to monitor and control Gizwits GAgent Wi-Fi thermostats. This integration requires **no cloud account, no Gizwits App ID, and no remote access tokens**.

It was built by capturing local traffic from a **Radiant Australia** underfloor heating thermostat. Because it interfaces directly with the generic Gizwits GAgent transport layer, it can work with other HVAC systems running the same underlying firmware payload schemas.

---

## ⚡ Features & Status Matrix

| Feature / Control | Status | Protocol Mechanism |
| :--- | :--- | :--- |
| **Local Status Monitoring** | ✅ **Confirmed** | Decoded from real phone-app request/reply frames. |
| **Power Control (On/Off)** | ✅ **Confirmed** | Verified via sub-commands (`0x93`) and state pushing. |
| **Target Setpoint Adjustment**| ✅ **Supported** | Managed natively via Home Assistant's `set_temperature` service. |
| **Instant State Updates** | ✅ **Confirmed** | Leverages unsolicited UDP network broadcasts on change. |
| **HVAC Action (Heating/Idle)**| 🟡 **Inferred** | Tied to Flag Bit 3 (Valve 1 status); requires validation. |
| **Schedule Configuration** | ❌ **Unsupported** | Write format for internal weekly schedules is unknown. |

---

## 🛠️ Installation & HACS Configuration

### Step 1: Prepare Your Personal Repository
1. Fork or push this repository's folder structure to your own **public** GitHub profile (HACS requires public read-access).
2. Open `custom_components/gizwits_thermostat/manifest.json`.
3. Locate the `documentation` and `issue_tracker` URLs, and replace `YOUR_GITHUB_USERNAME` with your real GitHub account name.

### Step 2: Add Custom Repository to HACS
1. In Home Assistant, navigate to **HACS**.
2. Click the **Three Dots (⋮) Menu** in the top-right corner and select **Custom repositories**.
3. Paste your public GitHub repository URL into the repository field.
4. Select **Integration** as the category, and click **Add**.

### Step 3: Install and Setup
1. Find **Gizwits Thermostat (tested on Radiant Australia)** in your HACS integrations dashboard and click **Download**.
2. **Restart Home Assistant** to clear the component cache.
3. Go to **Settings** ➡️ **Devices & services** ➡️ **Add integration**.
4. Search for **"Gizwits Thermostat"** and enter your thermostat's local IP address. If you have multiple devices, add them sequentially as independent instances.

⚠️ **CRITICAL CONFIGURATION NOTE:** You **must force-close the Radiant phone app** while pairing or running tests. The thermostat hardware only accepts a highly restricted number of concurrent TCP client sockets; if the phone app is running in the background, the device will reject Home Assistant's connection requests.

---

## 📊 Integration Entities & Attributes

Once paired, the integration automatically creates a Device entry containing the following entities:

### 1. Climate Entity (`climate.<thermostat_name>`)
* **State Values:** `heat` (On), `off` (Off).
* **Attributes Exposed & Features:**
  * `current_temperature`: Maps directly to the **Floor Sensor** (`°C`).
  * `temperature` (Target Setpoint): Fully adjustable target setting. Can be altered using the standard thermostat dial card UI or via the `climate.set_temperature` automation service.
  * `hvac_action`: Displays `heating` when the heating relay loop is engaged, and `idle` when target temperature is reached.

### 2. Monitoring Sensors
* **`sensor.<name>_floor_temperature`**: Current physical floor loop temperature.
* **`sensor.<name>_air_temperature`**: Room ambient air temperature sensor value.
* **`sensor.<name>_setpoint`**: The currently targeted thermostat temperature limit.
* **`sensor.<name>_device_clock`**: *(Diagnostic, disabled by default)* Shows internal hardware clock string.

### 3. Binary Control Sensors
* **`binary_sensor.<name>_power`**: Raw power state (`on`/`off`).
* **`binary_sensor.<name>_valve_1`**: Relay loop state monitor (`on` means current is flowing to element).

---

## 🎨 Dashboard Visual Representation

Here is a visual mockup of how the integration surfaces entities natively on your Lovelace Dashboard grid using classic entity stacks:

```text
┌────────────────────────────────────────────────────────┐
│ 🛋️ Living Room Underfloor Heating                      │
│                                                        │
│   ┌────────────────────────────────────────────────┐   │
│   │                 Heating                        │   │
│   │                  24.5°                         │   │
│   │       Target Setpoint: [ 22.5°C ] ➖/➕        │   │
│   └────────────────────────────────────────────────┘   │
│                                                        │
│   ⚙️ System Attributes                                  │
│   ──────────────────────────────────────────────────   │
│   🌡️ Air Temp Sensor   ...................   19.2°C     │
│   🔥 Valve Loop 1 Status .................   ACTIVE    │
│   ⏰ Internal Clock    ...................    22:09    │
│                                                        │
│   [  POWER OFF  ]                      [  MODE: MAN  ] │
└────────────────────────────────────────────────────────┘
```

---

## 🔍 Protocol Mechanics & Frame Deep-Dive

The communication structure operates via a reverse-engineered local framing protocol.

### General Frame Envelope
Every packet sent or received over local TCP conforms to this exact structure:
```text
+───────────────────────+────────────────+────────────+─────────────────────+───────────────────+

| Header (Magic Bytes)  | Length (Bytes) | Null Separ | Cmd Mode (2 Bytes)  | Payload Segment   |
| 00 00 00 03           | [Variable]     | 00         | [Variable Type]     | [Data Bytes]      |
+───────────────────────+────────────────+────────────+─────────────────────+───────────────────+
```

### Handshake / Login Authentication Sequence
Before commands can be processed, a handshake sequence must happen over local port connection:
1. **Passcode Request:** Home Assistant sends command code `06`.
2. **Passcode Response:** Thermostat returns command code `07` embedded with its hardware passcode string.
3. **Login Request:** Home Assistant transmits command code `08` compiling the received passcode signature.
4. **Login Validation:** Thermostat yields code `09`. If the final byte parses as `0x00`, local access authentication is accepted.

### Reading & Operational Writes
* **Polled Read Execution:** A frame using command `0x93` combined with sub-payload bytes `05 00 00` requests full state details. The device responds on command wrapper `0x94` followed by a custom **14-byte status payload block**.
* **Power Modification Execution:** Toggling the hardware state issues command code `0x93` with sub-arrays `05 0a 01` (Turn On) or `05 0a 00` (Turn Off). This transaction validates instantly via a `0x94` success code, immediately followed by an unsolicited UDP state push.
* **Network Push Mechanics:** State sync is driven over **UDP Port 12414**. When Home Assistant shares the same local network subnet or VLAN segment as the physical thermostats, changes on the wall display are instantly broadcast back. If UDP sockets are blocked by other running services, a fail-safe internal polling loop runs every 5 minutes over TCP.

### The 14-Byte Payload Structure Matrix
When parsing state answers (where Byte 0 always registers as `0x06`):

```text
Byte 00: Always 0x06 [Constant]
Byte 02: [System Bitmask Flags]
         ├── Bit 0: Power State (0 = Off, 1 = On)
         ├── Bit 1: Manual Override Mode State
         ├── Bit 2: Interface Child Key-Lock Toggle
         ├── Bit 3: Physical Valve Output 1 Loop
         └── Bit 4: Physical Valve Output 2 Loop
Byte 04-05: Air Temperature Sensor Reading (Raw Data Integer = °C × 10)
Byte 06-07: Operational Target Setpoint    (Raw Data Integer = °C × 10)
Byte 08   : Current Active Schedule Program Index
Byte 09   : Active Weekday Integer (0 = Sunday)
Byte 10-11: Internal Hardware Clock (Encoded in BCD; e.g., 0x22 0x09 translates to 22:09)
Byte 12-14: Floor Temperature Sensor Reading (Raw Data Integer = °C × 10)
```

---

## 🎛️ Advanced Development Tools

### Raw Hex Command Injector Service
The integration includes a built-in specialized developer payload service loop: `gizwits_thermostat.send_raw`. This service allows you to write raw hex data strings over the active connection pipeline to monitor hardware responses directly.

⚠️ **WARNING:** Do not inject unverified byte arguments. The integration treats data frames as opaque blobs. Passing bad or corrupted array instructions could corrupt the hardware memory configurations.

---

## 🛠️ Diagnostics & Local Testing

You can validate payload extraction parsing functionality completely separate from an active Home Assistant runtime ecosystem.

Execute the included testing suite from your project root directory:
```bash
python -m unittest tests.test_gizwits_lan -v
```
This tests frame decoding pipelines using historical hex traffic logs caught during active network operations.

---

## 📋 Troubleshooting Guidelines

* **`cannot_connect` Failure Alert During Setup:** Force-close all vendor apps on smartphones, verify your local routing configurations permit UDP broadcast streams across execution ports, and power-cycle the physical thermostat breaker.
