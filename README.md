# RS485 Control Studio — Home Assistant repository

Modbus TCP and USB/RS485 bridge with a setup studio, multiple units, raw captures and native Home Assistant MQTT entities.

## Install through the app/add-on store

1. Install a broker (for example Mosquitto) and configure Home Assistant's built-in MQTT integration.
2. Open **Settings → Apps/Add-ons → Store → ⋮ → Repositories**.
3. Add **https://github.com/architechlabs/rs485-bridge**.
4. Install **RS485 Control Studio**, start it, and select **Open Web UI**.
5. Configure the gateway and units, verify reads, then deliberately authorize writes.

[Add this repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Farchitechlabs%2Frs485-bridge)

The bridge uses the built-in MQTT integration; no separate custom component is required. Fresh settings start with TX locked. See [operation and troubleshooting](rs485_bridge/DOCS.md).

## Repository layout — important when uploading

Upload the **contents** of `home_assistant/` as the Git repository root:

```text
repository.yaml
README.md
.gitignore
rs485_bridge/
  config.yaml
  Dockerfile
  requirements.txt
  app/
  engine/
  profiles/
  web/
```

`repository.yaml` belongs beside `rs485_bridge/`, not inside it. Its presence at the Git repository root is required by Home Assistant. Uploading only the app folder or placing the metadata one directory above the published root will produce “not a valid app repository.”

For a local installation, copy just `rs485_bridge/` to the Home Assistant host's `/addons/rs485_bridge` and reload the store. This local-directory installation is different from registering a Git repository URL.

## Publishing from the engineering workspace

Run `python scripts/release-addon.py` from the parent engineering project. It creates:

- `dist/rs485-control-studio-addon-0.3.0.zip`: the app folder for local installation.
- `dist/rs485-control-studio-repository-0.3.0.zip`: metadata at the archive root plus the app folder, ready to publish as a Git repository.

Archives exclude Git history, Python caches and runtime credentials/data. Extract the repository archive and upload its contents directly to the repository root.
