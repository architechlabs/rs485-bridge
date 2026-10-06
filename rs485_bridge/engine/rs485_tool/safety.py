"""Central transmit gate. TX starts locked for every application entry point."""
class SafetyGate:
    def __init__(self, locked: bool = True): self.locked = locked
    def lock(self): self.locked = True
    def unlock(self): self.locked = False
    def require_tx(self):
        if self.locked: raise PermissionError("TX LOCK is enabled. Run 'tx unlock' explicitly before transmitting.")
