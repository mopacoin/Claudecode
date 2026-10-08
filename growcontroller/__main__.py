import argparse
import json
import logging
import os
import signal
import threading

from .controller import Controller
from .web import serve


def expand(x):
    """${ENV}-Platzhalter in Strings ersetzen, damit Passwörter nicht in der Config stehen müssen."""
    if isinstance(x, dict):
        return {k: expand(v) for k, v in x.items()}
    if isinstance(x, list):
        return [expand(v) for v in x]
    return os.path.expandvars(x) if isinstance(x, str) else x


def main():
    ap = argparse.ArgumentParser(prog="growcontroller")
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(a.config) as f:
        cfg = json.load(f)
    cfg = expand(cfg)
    ctl = Controller(cfg, a.data)
    w = cfg.get("web", {})
    srv = serve(ctl, w.get("host", "0.0.0.0"), w.get("port", 8080), w.get("token"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: ctl.stop_evt.set())
    ctl.run()  # blockiert bis Signal; schaltet danach alle Ausgänge aus
    srv.shutdown()


if __name__ == "__main__":
    main()
