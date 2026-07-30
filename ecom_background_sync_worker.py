from __future__ import annotations

import logging
import signal
import time

from webapp.server import (
    ecom_background_sync_enabled,
    ecom_background_sync_interval_seconds,
    run_ecom_background_sync_tick,
    set_ecom_background_sync_state,
)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("industria.ecom_background_sync")
STOP = False


def request_stop(signum, frame) -> None:
    global STOP
    STOP = True
    set_ecom_background_sync_state({
        "status": "stopping",
        "message": "Arrêt demandé du service de sync interne eCom.",
    })


def main() -> int:
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Sync interne eCom démarrée.")
    set_ecom_background_sync_state({
        "status": "idle",
        "message": "Service de sync interne eCom démarré.",
    })
    while not STOP:
        if ecom_background_sync_enabled():
            result = run_ecom_background_sync_tick()
            logger.info("Tick eCom: %s", result)
        else:
            set_ecom_background_sync_state({
                "status": "disabled",
                "message": "Sync interne eCom désactivée.",
            })
        sleep_for = ecom_background_sync_interval_seconds()
        for _ in range(sleep_for):
            if STOP:
                break
            time.sleep(1)
    logger.info("Sync interne eCom arrêtée.")
    set_ecom_background_sync_state({
        "status": "stopped",
        "message": "Service de sync interne eCom arrêté.",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
