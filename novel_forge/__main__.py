import logging
import os
import re
from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG


class QuietPollingAccessFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, target, _, status = args
        if method != "GET" or status not in (200, 304) or not isinstance(target, str):
            return True
        path = target.partition("?")[0]
        return path != "/api/health" and not re.fullmatch(r"/api/jobs/[^/]+/(?:revisions|editor-desk)", path)


def logging_config():
    config = deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["quiet_polling"] = {"()": QuietPollingAccessFilter}
    config["loggers"]["uvicorn.access"].setdefault("filters", []).append("quiet_polling")
    config["loggers"]["novel_forge"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return config


if __name__ == "__main__":
    uvicorn.run(
        "novel_forge.app:app",
        host=os.getenv("NOVEL_FORGE_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "3457")),
        timeout_graceful_shutdown=5,
        log_config=logging_config(),
    )
