"""
Local text tools for the LLM enrichment step (see enrichment.py).

Nothing in this module calls an LLM. It covers the work done before and after
the model call:

Before (token savers and input screening):
- clean_html_local()         strip scripts, styles and attributes from scraped HTML
- clean_title_local()        fix encoding and duplicated words in a title
- find_spec_sheet_url()      pick the spec-sheet PDF from product_db.useful_links
- extract_text_from_pdf()    text of the first PDF pages
- detect_poison_heuristic()  regex screen for prompt injection and garbage scrapes

After (output hardening):
- fix_mojibake()             repair encoding artifacts in the model output
- find_unsafe_markup()       report markup the model was told not to produce
- sanitize_html()            tag-whitelist sanitizer, applied before anything is stored
"""

import io
import json
import re

import ftfy
import nh3
import pdfplumber
import requests
from selectolax.lexbor import LexborHTMLParser as HTMLParser

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
PDF_MAX_PAGES = 2                    # first pages hold the spec table; more only burns context
PDF_MAX_BYTES = 10 * 1024 * 1024
PDF_TIMEOUT_SEC = 15

# ---------------------------------------------------------------------------
# Output whitelist
# ---------------------------------------------------------------------------
# Enriched HTML is injected into storefront pages, so only structural tags
# survive. Links and images are left out on purpose: a link the model was
# talked into adding is the usual payload of a poisoned product page.
ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i",
    "h3", "h4", "sup", "sub",
    "table", "thead", "tbody", "tr", "th", "td",
}
ALLOWED_ATTRIBUTES = {
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

_UNSAFE_MARKUP_RULES = {
    "active_content": re.compile(
        r"<\s*(script|iframe|object|embed|form|style|link|meta|base|svg)\b", re.I),
    "event_handler": re.compile(r"<[^>]+\son\w+\s*=", re.I),
    "script_url": re.compile(r"(javascript|vbscript)\s*:|data\s*:\s*text/html", re.I),
    "link_or_image": re.compile(r"<\s*(a|img)\b", re.I),
}

# ---------------------------------------------------------------------------
# Poison heuristics
# ---------------------------------------------------------------------------
# Each rule names one way scraped text can be unfit for the model: an attempt
# to steer it (prompt injection) or a page that is not a product description.
_POISON_RULES = {
    "instruction_override": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}"
        r"\b(previous|prior|above|earlier|all|your|system)\b.{0,30}"
        r"\b(instructions?|prompts?|rules|context)\b", re.I | re.S),
    "role_hijack": re.compile(
        r"\byou are (now|no longer)\b|\bnew instructions?\s*:"
        r"|\bact as (an?|the)\b.{0,30}\b(assistant|ai|model)\b", re.I | re.S),
    "prompt_probe": re.compile(
        r"\b(system prompt|developer message|reveal your (instructions|prompt))\b", re.I),
    "llm_addressing": re.compile(
        r"\bas an ai\b|\bai language model\b|\blarge language model\b"
        r"|\battention,? (ai|llm|assistant)\b", re.I),
    "chat_markup": re.compile(
        r"<\|?(im_start|im_end|endoftext)\|?>|\[/?INST\]|<</?SYS>>", re.I),
    "output_steering": re.compile(
        r"\b(html_description|html_specs_table|product_type)\b", re.I),
    "scrape_garbage": re.compile(
        r"\b(access denied|403 forbidden|404 not found|page not found"
        r"|enable javascript|verify you are (a )?human|captcha)\b", re.I),
}
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_UNICODE_TAGS = re.compile(r"[\U000E0000-\U000E007F]")
_ZERO_WIDTH_LIMIT = 3


# ===========================================================================
# Before the model call
# ===========================================================================

def clean_html_local(raw_html) -> str:
    """
    Strip scraped HTML down to bare structure before it is sent to the model.
    Removes non-content tags and every attribute, then collapses whitespace.
    """
    if not raw_html:
        return ""

    text = ftfy.fix_text(str(raw_html))
    tree = HTMLParser(text)

    for tag in tree.css("script, style, meta, link, noscript, iframe, button, input, form, svg"):
        tag.decompose()

    # Classes, ids and inline styles carry no product information.
    for node in tree.root.traverse():
        for name in list(node.attributes or {}):
            del node.attrs[name]

    clean_html = tree.body.html if tree.body else None
    if not clean_html:
        return re.sub(r"\s+", " ", text).strip()

    clean_html = re.sub(r"^<body>|</body>$", "", clean_html.strip())
    return re.sub(r"\s+", " ", clean_html).strip()


def html_to_text(html) -> str:
    """Visible text of an HTML fragment, whitespace collapsed."""
    if not html:
        return ""
    tree = HTMLParser(str(html))
    node = tree.body or tree.root
    text = node.text(separator=" ") if node else ""
    return re.sub(r"\s+", " ", text).strip()


def clean_title_local(text) -> str:
    """Fix encoding, drop a word repeated back to back ("Acme Acme"), collapse whitespace."""
    if not text:
        return ""
    text = ftfy.fix_text(str(text))
    text = re.sub(r"(?i)\b(\w+)\s+\1\b", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def find_spec_sheet_url(useful_links):
    """
    Pick the spec-sheet PDF from product_db.useful_links.
    Scrapers store that column as a JSON list of {"text": ..., "url": ...}.
    """
    if not useful_links:
        return None
    try:
        links = json.loads(useful_links) if isinstance(useful_links, str) else useful_links
    except (TypeError, ValueError):
        return None
    if not isinstance(links, list):
        return None

    for link in links:
        url = link.get("url", "") if isinstance(link, dict) else str(link)
        lowered = (url or "").lower()
        if ".pdf" in lowered and ("spec" in lowered or "tech" in lowered):
            return url
    return None


def extract_text_from_pdf(url):
    """Download a spec sheet and return the text of its first pages, or None."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    try:
        response = requests.get(url, timeout=PDF_TIMEOUT_SEC)
        if response.status_code != 200 or len(response.content) > PDF_MAX_BYTES:
            return None
        text_content = ""
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for page in pdf.pages[:PDF_MAX_PAGES]:
                text = page.extract_text()
                if text:
                    text_content += text + "\n"
        return text_content or None
    except Exception:
        # A missing or unreadable spec sheet must not block enrichment.
        return None


def detect_poison_heuristic(text) -> list[str]:
    """
    Regex screen for prompt injection and garbage scrapes.
    Returns the names of the rules that matched; an empty list means nothing found.
    """
    if not text:
        return []
    hits = [name for name, pattern in _POISON_RULES.items() if pattern.search(text)]
    if len(_ZERO_WIDTH.findall(text)) >= _ZERO_WIDTH_LIMIT or _UNICODE_TAGS.search(text):
        hits.append("hidden_characters")
    return hits


# ===========================================================================
# After the model call
# ===========================================================================

# Sequences ftfy leaves behind when the model copies half-broken text.
_MOJIBAKE_REPLACEMENTS = {
    "â€”": "—",
    "â€“": "–",
    "â€™": "'",
    "€”": "—",
    "Ã": "",
}


def fix_mojibake(text) -> str:
    """Repair encoding artifacts in model output."""
    if not text:
        return ""
    text = ftfy.fix_text(text)
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


def find_unsafe_markup(html) -> list[str]:
    """
    Name the kinds of forbidden markup present in raw model output.
    The sanitizer removes them either way; a hit means the model did something
    it was told not to do, so the result goes to human review.
    """
    if not html:
        return []
    return [name for name, pattern in _UNSAFE_MARKUP_RULES.items() if pattern.search(html)]


def sanitize_html(html) -> str:
    """Whitelist sanitizer: keeps ALLOWED_TAGS, drops everything else and all other attributes."""
    if not html:
        return ""
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        clean_content_tags={"script", "style", "iframe", "object", "embed", "noscript"},
        strip_comments=True,
    ).strip()
