# Changelog

## 0.3.0

- **Listen-only entities** (`listen_selector`, `listen_source`, `off_delay`): decode NV
  updates seen on the bus and publish them without sending anything to the network.
- **Bindings to the bridge**: "To bridge", "Move to bridge" and "Unlink" in the web NV table,
  `lonutil.py bind` / `unbind` on the command line. Backup before writing, read-back
  verification and a device health check afterwards.
- The serial port is locked: a second process gets a clear error.
- Stricter response matching (transaction, response code, sender, NV index).
- Memory reads start with 16-byte blocks and halve them on rejection *or* timeout.

## 0.2.0

- Web interface (`lonweb.py`): panel, node tools, bus monitor, configuration editor.
- NodeUtil-style diagnostics (`lonutil.py`) and the device simulator (`lonsim.py`).

## 0.1.0

- First bridge: NV fetch polling, acknowledged NV updates, Home Assistant discovery.
