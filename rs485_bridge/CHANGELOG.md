# 0.3.2

- Separate command/readback pacing from normal polling; optional responsive TCP preset and read-only timing benchmark.
- Commands wake the worker immediately and receive priority between poll transactions.
- Publish each verified point during compound commands; retain actual readback and never repeat a write after a lost acknowledgement.
- Bounded additional readbacks for a stale value, unchanged MQTT message suppression and command latency attributes on climate entities.
- Applying/verifying/verified feedback, one-second cached UI refresh, and fewer configuration confirmations when existing permissions are unchanged.

# 0.3.1

- Replace native confirmation/token dialogs with in-page dialogs that work in iframe panels.
- Persistent import progress/errors and explicit cancellation feedback.
- Log API operations, UI import events, applied site counts and unit state changes without logging credentials or request bodies.
- Preserve an existing room's stable entity ID when its verified controller/Unit ID/indoor address matches an imported room.
- Regression checked in a cross-origin iframe without native modal permission.
- Version static assets and reload on backend version changes; null/missing room addresses cannot crash the unit list.

# 0.3.0

- Explicit read-only monitoring setup and no-payload transport tests from the add-on host.
- Validated site imports preserve MQTT credentials/site ID and apply mappings together with rollback on file failures.
- SHERRY SINGH 12-room preset with floor labels; unknown addresses remain unassigned and cannot transmit.
- Per-point diagnostics/MQTT availability and partial read visibility.
- Configurable TCP connect settling delay and total response deadline.
- Custom point controls and background refresh that preserves setup forms.

# 0.2.0

- Configurable TCP and USB/RTU gateways and multiple individually addressed units.
- Home Assistant MQTT climate, sensor, number, select, switch and button discovery.
- Ingress setup studio, strict profiles, upload/export, saved recipes and raw captures.
- Safe defaults, explicit write authorization, fresh TCP connections and readback.
