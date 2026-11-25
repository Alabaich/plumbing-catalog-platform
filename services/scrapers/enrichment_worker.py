"""
RQ worker for LLM enrichment jobs.

Consumes the queue the orchestrator fills (see enrichment.enqueue_changed).
Runs with the RQ scheduler enabled so failed jobs are retried after a delay.

Usage:
    python enrichment_worker.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from redis import Redis
from rq import Queue, Worker

from config import ENRICH_QUEUE, REDIS_URL
from llm_client import llm_configured
from utils.logger import get_logger

logger = get_logger("ENRICHMENT", "worker")


def main():
    if not llm_configured():
        # Not an error: the orchestrator enqueues nothing without a key either.
        logger.warning("LLM_API_KEY / LLM_MODEL not set — worker will idle, no jobs are enqueued.")

    connection = Redis.from_url(REDIS_URL)
    queue = Queue(ENRICH_QUEUE, connection=connection)
    logger.info(f"Enrichment worker listening on queue '{ENRICH_QUEUE}'")
    Worker([queue], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
