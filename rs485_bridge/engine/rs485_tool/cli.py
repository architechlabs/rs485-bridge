"""Command-line entry point."""
from __future__ import annotations
import argparse, logging
from .device_detection import detect_devices,format_devices
from .shell import EngineeringShell
from .config import load_config,settings_from
from .serial_manager import SerialManager

def main(argv=None):
    parser=argparse.ArgumentParser(prog="rs485-tool",description="RS485/Modbus RTU engineering toolkit")
    parser.add_argument("--verbose",action="store_true");parser.add_argument("--debug",action="store_true");parser.add_argument("--quiet",action="store_true");parser.add_argument("--config")
    sub=parser.add_subparsers(dest="command");sub.add_parser("detect");sub.add_parser("devices");sub.add_parser("shell")
    mock=sub.add_parser("mock-server");mock.add_argument("--host",default="127.0.0.1");mock.add_argument("--port",type=int,default=5020);mock.add_argument("--delay",type=float,default=.03);mock.add_argument("--crc-error",action="store_true");mock.add_argument("--malformed",action="store_true")
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO if args.verbose else logging.WARNING if args.quiet else logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command in ("detect","devices"): print(format_devices(detect_devices()));return 0
    if args.command=="mock-server":
        from .mock import MockModbusServer
        try: MockModbusServer(args.host,args.port,args.delay,args.crc_error,args.malformed).serve_forever()
        except KeyboardInterrupt: pass
        return 0
    # No command (or explicit shell) is the one-command workflow: detect, select a sole likely adapter, open the shell.
    EngineeringShell(args.config).run();return 0

if __name__=="__main__": main()
