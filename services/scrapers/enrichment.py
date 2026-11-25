"""
LLM enrichment — turns scraped product HTML into clean, structured catalog content.

Where it sits in the pipeline:

    scrapers → parsing_queue → orchestrator → diff_engine → [this step] → product_enrichment

How it works:
1. After a brand finishes scraping, the orchestrator calls enqueue_changed(brand).
2. A product is enqueued only when its diff-engine hash (product_db.data_hash)
   differs from the hash stored at its last enrichment — new and changed products only.
3. An RQ worker (enrichment_worker.py) runs enrich_product(handle) for each of them.
4. Scraped HTML is cleaned locally, the spec-sheet PDF is read, and the text is
   screened for prompt injection and garbage before the model sees it.
5. The model returns JSON: html_description, html_specs_table, product_type.
   The cleaned title is stored next to it.
6. The response is validated (pydantic), sanitized (tag whitelist) and the
   product type is checked against the taxonomy in prompts/categories.json.
7. A clean result is stored as 'published'. Anything invalid or suspicious is
   stored as 'needs_review' and never reaches the storefront until a human approves it.

Cost control:
- Results are cached by a hash of the exact model input (llm_enrichment_cache).
  The same input is never paid for twice, even when the product hash changed
  for another reason (new image, new variant).
- Without LLM_API_KEY the step is skipped and the rest of the pipeline is unaffected.

Scraper data stays untouched: results go to product_enrichment, a separate
table keyed by handle. Readers join it WHERE status = 'published'.

Review CLI:
    python enrichment.py list
    python enrichment.py approve <handle> [--product-type "<taxonomy path or leaf>"]
    python enrichment.py retry <handle>
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    DATABASE_URL, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER,
    ENRICH_MAX_PER_BRAND, ENRICH_QUEUE, LLM_POISON_CHECK, REDIS_URL,
)
from diff_engine import should_update
from llm_client import get_llm_client, llm_configured
from utils.ai_cleaner import (
    clean_html_local, clean_title_local, detect_poison_heuristic,
    extract_text_from_pdf, find_spec_sheet_url, find_unsafe_markup,
    fix_mojibake, html_to_text, sanitize_html,
)
from utils.logger import get_logger, notify_changes

logger = get_logger("ENRICHMENT", "job")

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

STATUS_PUBLISHED = "published"
STATUS_NEEDS_REVIEW = "needs_review"

# Input caps keep one product from filling the context window.
MAX_HTML_CHARS = 15000
MAX_PDF_CHARS = 10000

# A rewrite that keeps less than this share of the source text has summarized
# it, which the prompt forbids. Checked only for sources long enough to judge.
MIN_KEPT_TEXT_RATIO = 0.3
MIN_SOURCE_CHARS_FOR_RATIO = 400

JOB_TIMEOUT_SEC = 600
JOB_RETRY_INTERVALS = [60, 300]


# ===========================================================================
# Taxonomy
# ===========================================================================

class Taxonomy:
    """
    Category tree from prompts/categories.json, flattened to leaf paths
    ("Hardware > Plumbing > ... > Kitchen Sink Faucets").

    The model must answer with one of these paths. A bare leaf name is also
    accepted when it is unique in the tree.
    """

    SEPARATOR = " > "

    def __init__(self, tree: dict):
        self.paths: list[str] = [self.SEPARATOR.join(p) for p in self._leaves(tree, ())]
        self._by_path = {p.lower(): p for p in self.paths}
        self._by_leaf: dict[str, list[str]] = {}
        for path in self.paths:
            self._by_leaf.setdefault(self.leaf(path).lower(), []).append(path)

    @classmethod
    def load(cls, filename: str = "categories.json") -> "Taxonomy":
        with open(os.path.join(PROMPTS_DIR, filename), encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def _leaves(cls, node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from cls._leaves(value, path + (key,))
        elif isinstance(node, list):
            if not node:
                yield path          # a key with an empty list is itself a leaf
            for item in node:
                yield path + (item,)

    @classmethod
    def leaf(cls, path: str) -> str:
        return path.split(cls.SEPARATOR)[-1]

    def resolve(self, value) -> str | None:
        """Exact path, or a leaf name that is unique in the tree. Otherwise None."""
        if not value:
            return None
        key = re.sub(r"\s*>\s*", self.SEPARATOR, str(value)).strip().lower()
        if key in self._by_path:
            return self._by_path[key]
        matches = self._by_leaf.get(key, [])
        return matches[0] if len(matches) == 1 else None

    def suggest(self, value) -> str | None:
        """
        Map a category already stored on the product to the taxonomy,
        tolerating small spelling differences. Used as the hint the model
        confirms or corrects.
        """
        resolved = self.resolve(value)
        if resolved or not value:
            return resolved
        leaf = self.leaf(re.sub(r"\s*>\s*", self.SEPARATOR, str(value))).strip().lower()
        close = difflib.get_close_matches(leaf, list(self._by_leaf), n=1, cutoff=0.85)
        if close and len(self._by_leaf[close[0]]) == 1:
            return self._by_leaf[close[0]][0]
        return None

    def as_prompt_text(self) -> str:
        return "\n".join(self.paths)


# ===========================================================================
# Prompts
# ===========================================================================

def _read_prompt(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, name), encoding="utf-8") as f:
        return f.read().strip()


def prompt_version() -> str:
    """
    Short hash of every prompt file. It is part of the cache key, so editing
    a prompt or the taxonomy invalidates cached results without a manual flush.
    """
    digest = hashlib.sha256()
    for name in ("description_clean.txt", "specs_structure.txt", "poison_detect.txt", "categories.json"):
        digest.update(_read_prompt(name).encode("utf-8"))
    return digest.hexdigest()[:16]


def build_system_prompt(suggested_type: str | None) -> str:
    if suggested_type:
        category_rule = (
            f'An earlier matching step assigned this product to "{suggested_type}". '
            "Return that path if it fits the product. If it is clearly wrong, "
            "return the correct path from the taxonomy instead."
        )
    else:
        category_rule = "Choose the single most specific path that fits the product."

    return "\n\n".join([
        _read_prompt("description_clean.txt"),
        _read_prompt("specs_structure.txt"),
        "product_type\n\n"
        + category_rule
        + "\n\n- Copy the path exactly as one full line of the taxonomy, including every level."
        "\n- Never return a path that is not in the taxonomy.",
        'Output format:\n\n{"html_description": "...", "html_specs_table": "...", "product_type": "..."}',
    ])


def build_user_prompt(title: str, clean_html: str, pdf_text: str, taxonomy_text: str) -> str:
    return (
        f"<product_title>\n{title}\n</product_title>\n\n"
        f"<scraped_html>\n{clean_html}\n</scraped_html>\n\n"
        f"<spec_sheet_text>\n{pdf_text or 'No spec sheet available.'}\n</spec_sheet_text>\n\n"
        f"<taxonomy>\n{taxonomy_text}\n</taxonomy>"
    )


def build_poison_user_prompt(title: str, clean_html: str, pdf_text: str) -> str:
    return (
        f"<product_title>\n{title}\n</product_title>\n\n"
        f"<scraped_html>\n{clean_html}\n</scraped_html>\n\n"
        f"<spec_sheet_text>\n{pdf_text or ''}\n</spec_sheet_text>"
    )


# ===========================================================================
# Response schema
# ===========================================================================

class EnrichmentOutput(BaseModel):
    """Shape the model must return. Unknown keys are rejected."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    html_description: str = Field(min_length=1, max_length=60000)
    html_specs_table: str = Field(default="", max_length=30000)
    product_type: str = Field(min_length=1, max_length=300)


class PoisonVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["clean", "suspicious"]
    reasons: list[str] = Field(default_factory=list)


def parse_json_object(text):
    """
    Parse a model response into a dict. Tolerates a markdown code fence,
    text around the object, and a one-element list wrapping it.
    """
    if not text:
        return None
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end])
        except ValueError:
            return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


# ===========================================================================
# Core — no database, no queue
# ===========================================================================

@dataclass
class EnrichmentResult:
    status: str
    content_hash: str
    title: str = ""                     # cleaned title (encoding fixed, duplicate words removed)
    html_description: str = ""
    html_specs_table: str = ""
    product_type: str | None = None     # taxonomy leaf, e.g. "Kitchen Sink Faucets"
    category: str | None = None         # full taxonomy path
    reasons: list[str] = field(default_factory=list)
    cached: bool = False

    def payload(self) -> dict:
        """What is stored in the cache: everything except per-run flags."""
        data = asdict(self)
        data.pop("cached")
        return data


class MemoryCache:
    """In-process cache with the same interface as PostgresCache. Used in tests."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def get(self, content_hash: str):
        return self.store.get(content_hash)

    def put(self, content_hash: str, payload: dict, llm) -> None:
        self.store.setdefault(content_hash, payload)


def compute_content_hash(llm, title: str, clean_html: str, pdf_text: str,
                         suggested_type: str | None) -> str:
    """
    Hash of everything that determines the model output: prompts, provider,
    model and the exact text sent. Equal hash means an equal request.
    """
    canonical = json.dumps(
        [prompt_version(), llm.provider, llm.model, title, clean_html, pdf_text, suggested_type or ""],
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enrich_content(title, raw_html, pdf_text, existing_type, llm, cache,
                   taxonomy: Taxonomy | None = None,
                   poison_check: bool = LLM_POISON_CHECK) -> EnrichmentResult:
    """
    Enrich one product. Returns a result with status 'published' or
    'needs_review'; never returns unsanitized HTML.

    LLMError (network, provider outage) is not caught here: a failed call is
    not a verdict about the product, so the job is retried instead.
    """
    taxonomy = taxonomy or Taxonomy.load()

    title = clean_title_local(title)
    clean_html = clean_html_local(raw_html)[:MAX_HTML_CHARS]
    pdf_text = (pdf_text or "")[:MAX_PDF_CHARS]
    suggested_type = taxonomy.suggest(existing_type)

    content_hash = compute_content_hash(llm, title, clean_html, pdf_text, suggested_type)

    cached = cache.get(content_hash)
    if cached:
        return EnrichmentResult(**cached, cached=True)

    result = _run_checks_and_model(
        title, clean_html, pdf_text, suggested_type, llm, taxonomy, poison_check, content_hash)
    cache.put(content_hash, result.payload(), llm)
    return result


def _run_checks_and_model(title, clean_html, pdf_text, suggested_type, llm,
                          taxonomy, poison_check, content_hash) -> EnrichmentResult:
    def needs_review(*reasons, **fields) -> EnrichmentResult:
        return EnrichmentResult(STATUS_NEEDS_REVIEW, content_hash, title=title, reasons=list(reasons), **fields)

    source_text = html_to_text(clean_html)
    if not source_text and not pdf_text:
        # Nothing to rewrite — the model would invent a description.
        return needs_review("empty_source")

    # 1. Free regex screen. A hit stops here: suspicious text is never sent for rewriting.
    heuristic_hits = detect_poison_heuristic(f"{title}\n{source_text}\n{pdf_text}")
    if heuristic_hits:
        return needs_review(*[f"poison_heuristic:{hit}" for hit in heuristic_hits])

    # 2. LLM classifier for what regexes cannot see.
    if poison_check:
        verdict_raw = llm.complete_json(
            _read_prompt("poison_detect.txt"),
            build_poison_user_prompt(title, clean_html, pdf_text),
        )
        try:
            verdict = PoisonVerdict.model_validate(parse_json_object(verdict_raw))
        except ValidationError:
            return needs_review("poison_check_invalid_response")
        if verdict.verdict != "clean":
            return needs_review("poison_llm", *[f"poison_llm:{r[:120]}" for r in verdict.reasons[:5]])

    # 3. Generation.
    raw = llm.complete_json(
        build_system_prompt(suggested_type),
        build_user_prompt(title, clean_html, pdf_text, taxonomy.as_prompt_text()),
    )
    data = parse_json_object(raw)
    if data is None:
        return needs_review("invalid_json")
    try:
        output = EnrichmentOutput.model_validate(data)
    except ValidationError as e:
        return needs_review(*sorted({f"schema:{'.'.join(map(str, err['loc'])) or 'root'}" for err in e.errors()}))

    # 4. Output checks. Each failed check adds a reason; any reason means review.
    reasons = [f"unsafe_html:{hit}"
               for hit in find_unsafe_markup(output.html_description + output.html_specs_table)]

    html_description = sanitize_html(fix_mojibake(output.html_description))
    html_specs_table = sanitize_html(fix_mojibake(output.html_specs_table))

    description_text = html_to_text(html_description)
    if not description_text:
        reasons.append("empty_description")
    elif (len(source_text) >= MIN_SOURCE_CHARS_FOR_RATIO
          and len(description_text) < MIN_KEPT_TEXT_RATIO * len(source_text)):
        reasons.append("description_truncated")

    if html_specs_table and "<table" not in html_specs_table:
        reasons.append("specs_not_a_table")

    # The injection may have worked partially: screen what the model wrote too.
    reasons += [f"poison_in_output:{hit}"
                for hit in detect_poison_heuristic(f"{description_text}\n{html_to_text(html_specs_table)}")]

    category = taxonomy.resolve(output.product_type)
    if category is None:
        reasons.append("unknown_product_type")

    return EnrichmentResult(
        status=STATUS_NEEDS_REVIEW if reasons else STATUS_PUBLISHED,
        content_hash=content_hash,
        title=title,
        html_description=html_description,
        html_specs_table=html_specs_table,
        product_type=Taxonomy.leaf(category) if category else None,
        category=category,
        reasons=reasons,
    )


def select_changed(rows) -> list[str]:
    """
    Change detection for enrichment, reusing the diff engine.

    rows: (handle, product data_hash, hash stored at last enrichment or None)
    Returns handles that are new or whose scraped data changed since.
    """
    return [
        handle for handle, data_hash, enriched_hash in rows
        if data_hash and should_update(data_hash.strip(), enriched_hash)
    ]


# ===========================================================================
# Database
# ===========================================================================

def get_db_connection():
    import psycopg2
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        database=DB_NAME, user=DB_USER, password=DB_PASS,
    )


class PostgresCache:
    """Content-hash cache in llm_enrichment_cache. Shared by all workers."""

    def __init__(self, cur):
        self.cur = cur

    def get(self, content_hash: str):
        self.cur.execute(
            "SELECT result FROM llm_enrichment_cache WHERE content_hash = %s", (content_hash,))
        row = self.cur.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def put(self, content_hash: str, payload: dict, llm) -> None:
        self.cur.execute(
            """
            INSERT INTO llm_enrichment_cache (content_hash, result, llm_provider, llm_model)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (content_hash) DO NOTHING
            """,
            (content_hash, json.dumps(payload, ensure_ascii=False), llm.provider, llm.model),
        )


def save_enrichment(cur, handle, vendor, source_hash, result: EnrichmentResult, llm) -> None:
    cur.execute(
        """
        INSERT INTO product_enrichment (
            handle, vendor, source_hash, content_hash, status,
            title, html_description, html_specs_table, product_type, category,
            review_reasons, llm_provider, llm_model
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (handle) DO UPDATE SET
            vendor = EXCLUDED.vendor,
            source_hash = EXCLUDED.source_hash,
            content_hash = EXCLUDED.content_hash,
            status = EXCLUDED.status,
            title = EXCLUDED.title,
            html_description = EXCLUDED.html_description,
            html_specs_table = EXCLUDED.html_specs_table,
            product_type = EXCLUDED.product_type,
            category = EXCLUDED.category,
            review_reasons = EXCLUDED.review_reasons,
            llm_provider = EXCLUDED.llm_provider,
            llm_model = EXCLUDED.llm_model,
            reviewed_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            handle, vendor, source_hash, result.content_hash, result.status,
            result.title, result.html_description, result.html_specs_table, result.product_type, result.category,
            json.dumps(result.reasons), llm.provider, llm.model,
        ),
    )


# ===========================================================================
# RQ job + enqueue
# ===========================================================================

def enrich_product(handle: str) -> dict:
    """RQ job: enrich one product and store the result."""
    llm = get_llm_client()
    if llm is None:
        logger.info(f"{handle}: LLM not configured, skipping")
        return {"handle": handle, "status": "skipped"}

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p."Title", p."Body (HTML)", p."Vendor", p.useful_links,
                       p."Product Type", p.data_hash, e.source_hash
                FROM product_db p
                LEFT JOIN product_enrichment e ON e.handle = p.handle
                WHERE p.handle = %s
                """,
                (handle,),
            )
            row = cur.fetchone()
            if not row:
                logger.warning(f"{handle}: not found in product_db, skipping")
                return {"handle": handle, "status": "missing"}
            title, body_html, vendor, useful_links, existing_type, data_hash, enriched_hash = row

            # The product may have been enriched by an earlier duplicate job.
            if not select_changed([(handle, data_hash, enriched_hash)]):
                logger.info(f"{handle}: unchanged since last enrichment, skipping")
                return {"handle": handle, "status": "unchanged"}

            pdf_text = extract_text_from_pdf(find_spec_sheet_url(useful_links))
            result = enrich_content(title, body_html, pdf_text, existing_type, llm, PostgresCache(cur))
            save_enrichment(cur, handle, vendor, data_hash.strip(), result, llm)
        conn.commit()
    finally:
        conn.close()

    # Characters removed by local cleaning before the model call (token savings).
    chars_saved = len(body_html or "") - len(clean_html_local(body_html))
    logger.info(
        f"{handle}: {result.status}"
        f" | type: {result.product_type or '-'} | local cleaning saved {chars_saved} chars"
        f"{' (cache hit)' if result.cached else ''}"
        f"{' — ' + ', '.join(result.reasons) if result.reasons else ''}"
    )
    if result.status == STATUS_NEEDS_REVIEW:
        notify_changes(
            f"🟡 <b>Enrichment needs review</b>\n"
            f"<b>Brand:</b> {vendor}\n"
            f"<b>Product:</b> {handle}\n"
            f"<b>Reasons:</b> {', '.join(result.reasons)}"
        )
    return {"handle": handle, "status": result.status, "cached": result.cached, "reasons": result.reasons}


def enqueue_changed(vendor: str) -> int:
    """
    Enqueue an enrichment job for every new or changed product of a brand.
    Called by the orchestrator after the brand's scrape. Returns the number enqueued.
    """
    if not llm_configured():
        logger.info(f"{vendor}: LLM not configured — enrichment skipped")
        return 0

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.handle, p.data_hash, e.source_hash
                FROM product_db p
                LEFT JOIN product_enrichment e ON e.handle = p.handle
                WHERE p."Vendor" = %s AND p."Status" = 'active' AND p.data_hash IS NOT NULL
                ORDER BY p.handle
                """,
                (vendor,),
            )
            changed = select_changed(cur.fetchall())
    finally:
        conn.close()

    if not changed:
        logger.info(f"{vendor}: no new or changed products to enrich")
        return 0

    from redis import Redis
    from rq import Queue, Retry

    batch = changed[:ENRICH_MAX_PER_BRAND]
    queue = Queue(ENRICH_QUEUE, connection=Redis.from_url(REDIS_URL))
    for handle in batch:
        queue.enqueue(
            "enrichment.enrich_product", handle,
            job_timeout=JOB_TIMEOUT_SEC,
            retry=Retry(max=len(JOB_RETRY_INTERVALS), interval=JOB_RETRY_INTERVALS),
        )

    deferred = len(changed) - len(batch)
    logger.info(
        f"{vendor}: enqueued {len(batch)} product(s) for enrichment"
        f"{f', {deferred} deferred to the next cycle' if deferred else ''}"
    )
    return len(batch)


# ===========================================================================
# Review CLI
# ===========================================================================

def _cli_list(cur, args) -> None:
    cur.execute(
        """
        SELECT handle, vendor, review_reasons, updated_at
        FROM product_enrichment
        WHERE status = %s
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (STATUS_NEEDS_REVIEW, args.limit),
    )
    rows = cur.fetchall()
    if not rows:
        print("Nothing waiting for review.")
    for handle, vendor, reasons, updated_at in rows:
        reasons = reasons if isinstance(reasons, list) else json.loads(reasons or "[]")
        print(f"{updated_at:%Y-%m-%d %H:%M}  {vendor}  {handle}\n    {', '.join(reasons)}")


def _cli_approve(cur, args) -> None:
    cur.execute(
        "SELECT status, html_description, product_type FROM product_enrichment WHERE handle = %s",
        (args.handle,),
    )
    row = cur.fetchone()
    if not row:
        sys.exit(f"No enrichment row for {args.handle}")
    status, html_description, product_type = row
    if not html_description:
        sys.exit("Nothing to publish: this product was stopped before generation. Use 'retry' after fixing the source.")

    category = None
    if args.product_type:
        category = Taxonomy.load().resolve(args.product_type)
        if category is None:
            sys.exit(f"Not in the taxonomy (or ambiguous leaf): {args.product_type}")
    elif not product_type:
        sys.exit("The stored product type is not in the taxonomy. Pass --product-type.")

    if category:
        cur.execute(
            "UPDATE product_enrichment SET product_type = %s, category = %s WHERE handle = %s",
            (Taxonomy.leaf(category), category, args.handle),
        )
    cur.execute(
        """
        UPDATE product_enrichment
        SET status = %s, reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE handle = %s
        """,
        (STATUS_PUBLISHED, args.handle),
    )
    print(f"{args.handle}: {status} → {STATUS_PUBLISHED}")


def _cli_retry(cur, args) -> None:
    cur.execute(
        "DELETE FROM product_enrichment WHERE handle = %s RETURNING content_hash", (args.handle,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"No enrichment row for {args.handle}")
    cur.execute("DELETE FROM llm_enrichment_cache WHERE content_hash = %s", (row[0],))
    print(f"{args.handle}: cleared — it will be enqueued again on the next cycle")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review queue for LLM enrichment")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="products waiting for review")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=_cli_list)

    p_approve = sub.add_parser("approve", help="publish a reviewed product")
    p_approve.add_argument("handle")
    p_approve.add_argument("--product-type", help="taxonomy path or unique leaf name")
    p_approve.set_defaults(func=_cli_approve)

    p_retry = sub.add_parser("retry", help="drop the result and its cache entry")
    p_retry.add_argument("handle")
    p_retry.set_defaults(func=_cli_retry)

    args = parser.parse_args()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            args.func(cur, args)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
