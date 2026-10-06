# Installation and operation

## What users install

RS485 Control Studio is a Home Assistant app/add-on plus an independently runnable bridge. It uses the **existing MQTT integration** in Home Assistant. No custom integration files are copied into `/config`, and no separate custom component is required. A broker and the built-in MQTT integration must be working for entity discovery.

The add-on requires Home Assistant OS/Supervisor. For Container/Core, run the service on a separate host and supply the same broker credentials. MQTT authentication is new functionality; passwords are kept in the private data directory, masked in the UI/API, and excluded from configuration exports.

## Install locally

Copy this entire directory, including `engine`, to `/addons/rs485_bridge`. Reload the add-on store and install from Local add-ons. The package intentionally has no `image:` entry so Supervisor builds the included Dockerfile. The first installation needs Internet access for the Python base image and pinned dependencies.

Enable Start on boot if desired, start the add-on, and open the Web UI. Only Ingress is exposed by default; the service rejects requests from outside the Supervisor ingress proxy. UART and read-only udev access enable USB detection without full privileged hardware access.

On the MQTT page the UI shows whether discovery is connected. If broker discovery is unavailable, supply host, port, username/password and optional certificate-validated TLS in Settings. The MQTT integration in HA must use that broker. Use a unique site ID per bridge instance.

## Connections

- **TCP:** supply controller IP, Modbus TCP port (typically 502), timeout and request delay. BACnet/IP's UDP port 47808 is a separate protocol. Prefer fresh connections for this LG controller; the service closes after each exchange. Read transport failures get at most one retry on a new connection. Write failures never get automatic retries.
- **USB/RTU:** attach the adapter to the bridge host and choose its detected device. The list shows chipset/USB hints, not proof that the electrical interface is RS485. Use a stable Linux by-ID path where available. Supply baud, data bits, parity, stop bits and timing from the gateway documentation. CH340/COM7 was observed on the Windows laptop; COM7 is not hardcoded and will not exist on a Linux HA host.
- **Passive capture:** serial only, with polling disabled. No bytes are transmitted, regardless of global control settings. Raw chunks are stored immediately and silence-framed analysis is stored as a separate event kind. USB buffering prevents exact on-wire byte timing.

Do not place a new RTU master onto a live bus already being driven by another master. The process prevents duplicate local workers; it cannot detect every external bus participant. Linux device permissions, driver support and wiring remain hardware concerns.

## Units and entities

Each unit has a stable ID, display name, gateway, slave/Unit ID, indoor/profile address and profile. A name change preserves IDs; changing the stable ID creates different discovery identifiers. Each configured unit is one HA device.

Profiles create individual entities and a climate entity when power/mode/target/current roles exist. Users can rename entities, assign areas, build standard/custom cards and use all exposed writable entities in automations. Numbers/selects/switches are gated by policy. Sensors are read-only. Buttons support explicit mapped command values. Climate OFF writes the power point; another climate mode writes the mode then power ON. Those are sequential physical operations, not an atomic HVAC transaction.

States are updated from successful readback. MQTT availability becomes offline when required reads fail, and retained state remains historical while unavailable. Optional profile points can be absent without taking the entire unit offline. Unknown enum values remain raw values instead of being assigned a fabricated name.

## Enabling operations

Fresh installations default to **TX locked**, global writes disabled and unit control disabled. Configure first; enabling a policy requires explicit review in the studio. Once authorized, policies persist across restarts for unattended HA operation. Locking stops new queued writes; a write already on the wire cannot be recalled. Changing an in-use profile locks the site again.

Active polling also transmits requests. Enable it per gateway and unlock TX globally. Default polling is 15 seconds and each transaction is separated by at least 250 ms. Poll intervals cannot be below 5 seconds. Failure polling backs off; command queues are bounded and expire rather than sending old commands after a long outage.

When verified, enable global writes and per-unit control. UI controls ask for a write confirmation; Home Assistant automations use the authorized policy without an interactive prompt. MQTT retained/duplicate commands are rejected and clean sessions prevent queued commands from being replayed on reconnect.

## This LG site

Known controller: `192.168.29.136:502`. Modbus Unit ID/Vnet is `10`. Indoor address `07` is OFFICE. BACnet Device ID 9000 and BACnet Type C do not select Modbus units.

LG's formula is `indoor_address × 16 + point_number − 1` within the relevant function/address space. Included profile `lg-ac-smart5` uses the [LG AC Smart 5 manual](https://media.us.lg.com/m/43e89bd22e3cc035/original/OM_AC_Smart5_PACS5A000-pdf.pdf):

| Point | Protocol address for 07 | Read/write | Values |
|---|---|---|---|
| Power | 0x70 | 01/05 | OFF 0; ON wire value FF00 |
| Mode | 0x70 | 03/06 | Cool 1, Dry 2, Fan 3, Auto 4, Heat 5 |
| Fan | 0x71 | 03/06 | Low 1, Middle 2, High 3, Auto 4 |
| Setpoint | 0x72 | 03/06 | Whole °C, 16–30, step 1 |
| Room temperature | 0x75 | 03 only | Integer °C from controller |
| Alarm | 0x76 | 01 only | Optional: normal 0 / alarm 1 |
| Error code | 0x76 | 03 only | Optional raw code |

The site reported mode 1, fan 3, setpoint 22 and room temperature 26. Fan 6 was explicitly rejected by the controller. OFF/ON were acknowledged; fan HIGH was both acknowledged and read back. The UI earlier displayed 26.5°C: the Modbus integer reading does not establish fractional precision or rounding. Fractional writes and undocumented registers are deliberately excluded. Confirm profile compatibility with the controller/firmware and installed indoor product before authorizing it elsewhere.

## Profiles, external mappings and updates

Profile Library imports JSON files and exported library bundles up to 1 MB. The editor exposes every field. A profile specifies its version, documentary source, base/stride, point offsets, read/write function, units, scaling, signed integers, ranges/step, enum states/commands and optional climate roles. Use the generic example only as a template; it is not a verified HVAC mapping.

Changing an existing profile ID replaces its mapping; changing the ID creates a new profile. Imports reject extra fields, invalid ranges and unsafe coil encodings. Configuration bundles import with TX and writes locked. Imported content is data; shell scripts, Python modules, templates that execute code, archives and runtime package installs are not supported.

Software/library upgrades are versioned add-on releases. For maintainers, update pinned dependencies, run tests and the container build, then perform the HAOS staging acceptance in `docs/validation.md`. Capture databases persist under `/data` and should be included in HA backups. Captures are not automatically pruned: monitor available storage and export/archive deliberately.

## API and saved recipes

In **Settings → Saved commands**, choose equipment, point and value, name the action and save it. The same dialog offers Run buttons for saved actions. Running a recipe asks for confirmation in the studio and still obeys the global and per-unit policy.

The studio uses the same API available to other software. Locally supply `Authorization: Bearer <api-token>`; modifying requests must also supply `X-Bridge-Request: 1`. Ingress uses Supervisor authentication and the proxy's source address. Routes:

| Route | Purpose |
|---|---|
| GET /api/status | Unit values, availability, gateway state and policy |
| GET/POST /api/config | Masked settings, revision check and explicit policy authorization |
| GET/POST /api/profiles | List/import validated mappings |
| GET /api/serial | OS devices, VID/PID/chipset hints and stable port |
| POST /api/command | `{"unit":"office","point":"fan","value":"high"}` |
| POST /api/gateway/poll | `{"gateway":"lg"}`; deliberate read request |
| POST /api/gateway/reconnect | Reconnect between transactions |
| GET /api/events | Recent raw captures and structured decoding |
| GET /api/export | Snapshot of all events as streaming JSONL |
| GET /api/bundle | Profiles and settings without broker passwords |
| GET/POST /api/commands | List/save `name`, `unit`, `point`, `value` recipes |
| POST /api/commands/run | `{"name":"office_high"}`; follows write policy |

For engineering raw send/replay and reverse-engineering observations, the original CLI is included. Disable the bridge gateway before opening a second serial session or TCP client. Run the CLI from `/data` if using an add-on terminal, or use the Windows toolkit to inspect exported JSONL. Never automatically replay all observed traffic; that can include both requests and replies and unrelated controllers.

## Home Assistant examples

Add a standard thermostat card using the discovered climate entity. Inspect its actual entity ID in Settings → Devices & services → MQTT; users may rename it. Example entity IDs below are placeholders for the discovered IDs.

```yaml
type: thermostat
entity: climate.office
```

```yaml
alias: Office weekday cooling
triggers:
  - trigger: time
    at: "09:00:00"
conditions:
  - condition: time
    weekday: [mon, tue, wed, thu, fri]
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.office
    data:
      hvac_mode: cool
  - action: climate.set_temperature
    target:
      entity_id: climate.office
    data:
      temperature: 23
  - action: climate.set_fan_mode
    target:
      entity_id: climate.office
    data:
      fan_mode: medium
mode: single
```

## Troubleshooting

- No entities: broker connection must be online; the built-in HA MQTT integration must use the same broker; unit must be enabled. Discovery is republished when HA announces online and on broker reconnect.
- Entities unavailable: TX may be locked, gateway polling disabled, unit not supported or reads failing. Cached values are not a new verification. Use Connections → Read now, then inspect Traffic.
- Repeated TCP reset: fresh-connection mode is the default; verify IP, port, Unit ID, controller session limits and other BMS clients. Writes with uncertain results require readback before another write.
- Illegal Data Value 03: value/range is unsupported. Do not guess additional fan/mode values. Edit the allowlist only with verified documentation.
- Unknown serial device: enable UART mapping on HA host; attach adapter there; choose correct port. Lights prove power/activity, not valid Modbus traffic.
- No serial bytes: verify baud/parity, A/B, reference ground, interface configuration and whether any master is polling. The LG Ethernet BMS gateway does not imply a Modbus RTU feed on an arbitrary A/B connector.
- Port owned: stop other terminals and scripts. The bridge rejects duplicate local gateway owners but cannot control another application.
- Configuration import errors: the API reports strict validation failures; correct the mapping rather than discarding raw capture data.
