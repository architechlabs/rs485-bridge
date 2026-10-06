"""Explain measured failures without claiming unobserved electrical causes."""
def failure_details(error, transport='tcp'):
    message=str(error)
    lower=message.lower()
    if 'modbus exception' in lower:
        code='modbus_exception'
        hint='Controller replied. Verify function, Unit ID and the documented address/value; network reachability is established.'
    elif 'timed out' in lower or 'deadline' in lower or isinstance(error, TimeoutError):
        code='timeout'
        hint='Test reachability from this add-on host. Check routing/VPN, firewall, controller IP, Modbus enablement and Unit ID. A laptop connection does not prove HA host access.'
    elif 'refused' in lower or '10061' in lower:
        code='connection_refused'
        hint='Host rejected the connection. Check TCP port 502, Modbus TCP enablement and controller client limits.'
    elif 'reset' in lower or 'closed connection' in lower or '10054' in lower:
        code='connection_closed'
        hint='Controller closed the connection. Use fresh connections, increase connect delay/request spacing, check Unit ID/function and competing BMS clients.'
    elif 'name or service' in lower or 'getaddrinfo' in lower:
        code='dns_error'
        hint='Resolve the hostname from the add-on host, or configure the verified IP address.'
    elif transport=='serial':
        code='serial_error'
        hint='Check adapter ownership, USB mapping on the HA host, baud/parity and wiring. USB power LEDs do not establish valid received data.'
    else:
        code='transport_or_protocol_error'
        hint='Inspect the raw capture, Unit ID, function and register map. Test reachability from the add-on host.'
    return {'code':code,'error':message,'hint':hint}
