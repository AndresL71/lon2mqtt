# lon2mqtt

A single-file bridge between a **LonWorks (ISO/IEC 14908, LonTalk)** network and **MQTT**, with **Home Assistant MQTT discovery**.

It talks directly to an EnOcean/Echelon USB network interface running **MIP/U61 firmware**
(for example the **U10 FT rev B**) through its FTDI serial port. No kernel driver, no `lonifd`
daemon, no LON network interface and no LNS/OpenLNS license are needed. It works inside an
unprivileged LXC container or a Raspberry Pi as long as `/dev/ttyUSB0` is available.

> Status: early. Tested with a U10 FT rev B on an FT-10 network. Use at your own risk: the
> bridge sends real network-management and NV update messages to your devices.

## Features

- Reads network variables with the network-management **NV Fetch** service (no bindings needed).
- Writes network variables with an **acknowledged explicit NV update**, using the selector
  reported by the node itself (**Query NV Config**), so it works for bound and unbound inputs.
- Per-entity polling intervals, plus **event-driven refresh**: the interface runs in layer 2, so
  when a bound NV update (e.g. a wall switch talking to a dimmer) is seen on the bus, the
  affected entity is re-read immediately.
- Home Assistant entities: `light` (dimmable or on/off), `switch`, `binary_sensor`, `sensor`,
  `number`. Availability (LWT) and HA birth-message handling included.
- Common SNVT codecs: `SNVT_switch`, `SNVT_lux`, `SNVT_temp_p`, `SNVT_temp`, `SNVT_lev_percent`,
  `SNVT_lev_cont`, `SNVT_count`, `SNVT_power`, `SNVT_elec_kwh`, `SNVT_press_p`, `SNVT_speed`,
  `SNVT_occupancy`, and `raw` (hex) for anything else.
- Command-line tools to **scan** a node's NV table, **read**, **write** and **sniff** the bus.

## How it works

| Layer | Detail |
|---|---|
| Serial | 460800 baud, 8N1, raw, no flow control, DTR/RTS asserted |
| Framing (UMIP) | `7E 00 <len> <ni_cmd> <data…>`, `len` counts `ni_cmd`, `7E` in data doubled, no checksum |
| Start-up | `7E 00 02 E5 01` puts the interface in layer-2 mode (it echoes the mode) |
| Transmit | `ni_cmd 0x12`, data = LPDU header + NPDU; the interface appends the CRC |
| Receive | `ni_cmd 0x1A`, data = LPDU header + NPDU + CRC16 |
| Read | SPDU request `0x73 <nv index>` to the node's Neuron ID → response `0x33 <index> <value>` |
| Write | `0x68` query gives the selector; then ACKD TPDU `[0x80 \| sel_hi][sel_lo][value]` to subnet/node |

Protocol details were taken from EnOcean's open-source
[lon-driver](https://github.com/izot/lon-driver) (`u61/U61Link.c`) and
[lon-stack-dx](https://github.com/izot/lon-stack-dx) (`lon_usb/lon_usb_link.c`), and from
S. Pastor's `native-linux-u61` work on running the U61 in user space. This project
re-implements the protocol in Python and contains no code from those repositories.

## Supported hardware

| Interface | Firmware | Supported |
|---|---|---|
| U10 FT rev B, U20 PL, U60 TP-1250, U70 PL | MIP/U61 (parallel USB) | yes (U10 FT rev B tested) |
| U10 rev C, U60 FT | MIP/U50 (serial USB, code packets + checksums) | no |

Interfaces with MIP/U50 firmware use a different link protocol and are not supported yet.

## Installation

```bash
git clone https://github.com/<you>/lon2mqtt.git
cd lon2mqtt
pip install -r requirements.txt      # pyyaml, paho-mqtt
cp config.example.yaml config.yaml   # edit it
```

The kernel's `ftdi_sio` driver exposes the interface as `/dev/ttyUSBx` (USB ID `0920:7500`).
Do **not** load the EnOcean `u61.ko` line discipline: it takes over the port.

The user running the bridge needs access to the serial port (`root`, or membership in the
`dialout` group).

### Proxmox LXC

Add to `/etc/pve/lxc/<CTID>.conf` on the host (or use `dev0: /dev/ttyUSB0` on PVE ≥ 8.2):

```
lxc.cgroup2.devices.allow: c 188:* rwm
lxc.mount.entry: /dev/ttyUSB0 dev/ttyUSB0 none bind,optional,create=file
```

## Configuration

See [`config.example.yaml`](config.example.yaml). The essentials:

- `lonworks.domain_id`: the domain your devices are installed in (hex, 0/1/3/6 bytes).
- `lonworks.source_subnet` / `source_node`: the logical address the bridge uses to send.
  **It must not be used by any other device** on the network. Responses are sent back to it.
- For each node: its 48-bit **Neuron ID** (12 hex digits). Subnet/node are discovered.
- For each entity: `nv_in` (the input NV the bridge writes) and/or `nv_out` (the output NV it
  reads), the `snvt_type`, the Home Assistant `type` and the `polling_interval`.

A typical LonWorks dimmer exposes an input (`nviSwitch`) that you write and an output
(`nvoSwitch`) that reports the actual level. Both are `SNVT_switch` (value 0–100 % and state).
Map them to one `light` entity with `nv_in` and `nv_out`.

### Finding NV indexes

```
$ python3 lon2mqtt.py -c config.yaml scan lighting_module_1
 idx  dir  selector bound auth len  value
   0  in   0x3fff              3  000000
  29  out  0x3fe2              2  0000
  51  in   0x0428   yes        2  c801
  ...
-- NV67 rejected: end of NV table (67 NVs)
```

`scan` also accepts a Neuron ID that is not in the configuration yet.

## Running

```bash
python3 lon2mqtt.py -c config.yaml          # add -v for debug output
```

A minimal systemd unit:

```ini
[Unit]
Description=LonWorks to MQTT bridge
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/lon2mqtt/lon2mqtt.py -c /opt/lon2mqtt/config.yaml
Restart=always

[Install]
WantedBy=multi-user.target
```

## MQTT topics

| Topic | Content |
|---|---|
| `<base>/status` | `online` / `offline` (retained, LWT) |
| `<base>/<entity>/state` | JSON, retained. Always contains `raw` (hex); plus `value`, `state`, `brightness`, `switch_state` depending on type |
| `<base>/<entity>/set` | Commands. Lights: HA JSON schema (`{"state":"ON","brightness":150}`, brightness 0–200 = 0–100 %). Switches: `ON`/`OFF`. Numbers: the value |
| `<prefix>/<component>/<node>/<entity>/config` | Home Assistant discovery (retained) |

## Command-line tools

```bash
python3 lon2mqtt.py -c config.yaml read  <node> 29 --type SNVT_switch
python3 lon2mqtt.py -c config.yaml write <node> 51 75:on --type SNVT_switch
python3 lon2mqtt.py -c config.yaml write <node> 51 9601            # raw hex
python3 lon2mqtt.py -c config.yaml sniff                           # decoded bus traffic
```

## Limitations

- **LonTalk authentication is not implemented.** NVs flagged for authentication can be read
  but writes are rejected by the node (the bridge logs a clear error).
- Only one interface and one domain per instance. Each instance needs its own MQTT `client_id`
  (two instances with the same id keep disconnecting each other).
- Polling uses network-management messages; keep intervals reasonable on busy FT-10 channels
  (each read is a request/response pair of roughly 20–25 bytes on the wire).
- Event-driven refresh only sees **bound** NV updates travelling on the same channel.
- New SNVTs can be added to the `CODECS` table in a single line if they are fixed-point scalars.

## License

MIT
