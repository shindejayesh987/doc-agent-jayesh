import asyncio
import logging
import os
import json
import copy
import math
import random
import re
from .utils import *
from concurrent.futures import ThreadPoolExecutor, as_completed

_log = logging.getLogger(__name__)

# Maximum continuation attempts for multi-turn completion loops
_MAX_CONTINUATION_ATTEMPTS = 8


# ── Phase 3: LLM bridge ────────────────────────────────────────────────────────
# These two async helpers replace all ChatGPT_API* callsites.
# finish_reason is normalized to legacy values: "finished" | "max_output_reached"
async def _llm(provider, prompt: str, chat_history: list = None) -> str:
    """Call provider with a prompt string, return response string."""
    from .llm.base import Message
    msgs = [Message(role=m["role"], content=m["content"]) for m in (chat_history or [])]
    msgs.append(Message(role="user", content=prompt))
    resp = await provider.complete(msgs)
    return resp.content or ""


async def _llm_fr(provider, prompt: str, chat_history: list = None):
    """Call provider, return (content, finish_reason) where reason is 'finished'|'max_output_reached'."""
    from .llm.base import Message
    msgs = [Message(role=m["role"], content=m["content"]) for m in (chat_history or [])]
    msgs.append(Message(role="user", content=prompt))
    resp = await provider.complete(msgs)
    finish = "max_output_reached" if resp.finish_reason == "length" else "finished"
    return resp.content or "", finish


async def _llm_json(provider, prompt: str, max_retries: int = 3) -> dict:
    """
    Call provider expecting a JSON response; retry with a stronger instruction
    if the response cannot be parsed.

    Uses ``parse_json_robust`` (raises on failure) so we can distinguish a
    genuine parse error from an intentional empty-dict response.

    Returns the parsed dict/list on success, or {} after all retries fail.
    """
    # Always include the JSON reminder — small local models need it every time.
    # On retries escalate with "CRITICAL" to shake loose any preamble/wrapping.
    _JSON_REMINDER  = "\n\nReturn valid JSON only. No markdown, no extra text."
    _JSON_CRITICAL  = "\n\nCRITICAL: valid JSON only, starting with { or [. No markdown."
    raw = ""
    for attempt in range(max_retries):
        suffix = _JSON_CRITICAL if attempt > 0 else _JSON_REMINDER
        p = prompt + suffix
        raw = await _llm(provider, p)
        try:
            return parse_json_robust(raw)
        except Exception:
            if attempt < max_retries - 1:
                _log.warning(
                    "_llm_json attempt %d/%d failed to parse JSON. Retrying.",
                    attempt + 1, max_retries,
                )
    _log.error(
        "_llm_json: all %d attempts failed. Raw (first 200): %s",
        max_retries, raw[:200],
    )
    return {}


def _safe_get(obj, key: str, default=""):
    """Safely call .get() on an _llm_json result that may be a list or scalar."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


# ── Phase 4: bounded concurrency helper ───────────────────────────────────────
async def _gather_bounded(coros, concurrency: int):
    """
    Run coroutines with a local semaphore so at most `concurrency` run at once.
    Results are returned in the same order as `coros`.
    The RateLimitedProvider already throttles at the provider level; this adds a
    second cap that prevents creating thousands of queued coroutine objects when
    gather is called with very large lists (e.g. 500-item TOCs).
    """
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(coro):
        async with sem:
            return await coro

    return list(await asyncio.gather(*[_run(c) for c in coros]))


################### check title in page #########################################################
# ── Phase 6 helpers ────────────────────────────────────────────────────────────

def _title_match_heuristic(title: str, page_text: str):
    """Fast substring check before spending an LLM call.

    Returns 'yes' if the normalised title is clearly present in the page,
    or None when inconclusive (caller must fall through to LLM).
    Never returns 'no' — a missing substring doesn't mean the section isn't
    there (OCR gaps, line-breaks, etc.).
    """
    if not title or len(title.strip()) < 3:
        return None
    norm_title = " ".join(title.lower().split())
    norm_text  = " ".join(page_text.lower().split())
    if norm_title in norm_text:
        return "yes"
    return None


_TOC_POSITIVE_RE = re.compile(
    r'(table\s+of\s+contents?|contents?\s*\n|^\s*contents?\s*$)',
    re.IGNORECASE | re.MULTILINE,
)
_TOC_ENTRY_RE = re.compile(
    r'^\s*\d+(\.\d+)*\.?\s+\S',
    re.MULTILINE,
)

def _toc_page_heuristic(content: str):
    """Returns 'yes' on strong TOC signals, None when uncertain (needs LLM).

    Only returns a positive verdict — false positives are cheaper than
    false negatives, and the LLM acts as the final arbiter for edge cases.
    """
    if _TOC_POSITIVE_RE.search(content):
        return "yes"
    if len(_TOC_ENTRY_RE.findall(content)) >= 3:
        return "yes"
    return None


async def _check_if_complete(content: str, result: str, kind: str, provider=None) -> str:
    """Single completion-check replacing two near-identical functions.

    kind='extraction'     — checks that a TOC extracted from a document is complete.
    kind='transformation' — checks that a raw TOC was fully transformed/cleaned.
    """
    if kind == 'extraction':
        label_a, label_b = "Document", "Table of contents"
        desc = "complete, which it contains all the main sections in the partial document"
    else:
        label_a, label_b = "Raw Table of contents", "Cleaned Table of contents"
        desc = "complete"
    prompt = (
        f"You are given a {label_a.lower()} and a table of contents.\n"
        f"Your job is to check if the table of contents is {desc}.\n\n"
        'Reply format:\n{\n    "thinking": <reasoning>\n    "completed": "yes" or "no"\n}\n'
        "Directly return the final JSON structure. Do not output anything else."
        f"\n {label_a}:\n{content}\n {label_b}:\n{result}"
    )
    json_content = await _llm_json(provider, prompt)
    return _safe_get(json_content, 'completed', 'no')


def _chunk_budget(opt) -> int:
    """Compute effective token-per-chunk budget from config + provider context window.

    Priority order:
      1. opt.pipeline.chunk_token_budget  (from config.yaml, default 20 000)
      2. Hard fallback: 20 000

    Always capped at 75% of the provider's context_window so a misconfigured
    budget can never silently overflow the model's context limit.
    Minimum is 1 000 tokens so a single page is never rejected.
    """
    budget = 20_000
    if opt is not None:
        pipeline = getattr(opt, 'pipeline', None)
        if pipeline is not None:
            budget = int(getattr(pipeline, 'chunk_token_budget', budget))

    provider = getattr(opt, 'provider', None) if opt is not None else None
    if provider is not None:
        cap = int(provider.context_window * 0.75)
        budget = min(budget, cap)

    return max(budget, 1_000)


def _inter_call_delay(opt) -> float:
    """Return seconds to pause between sequential LLM calls.

    Configurable via opt.pipeline.inter_call_delay.  Defaults to 0.5s —
    enough headroom for most providers without unnecessarily slowing the pipeline.
    Providers with tight rate limits (Anthropic free tier) can set this higher in app.py.
    """
    if opt is not None:
        pipeline = getattr(opt, 'pipeline', None)
        if pipeline is not None:
            return float(getattr(pipeline, 'inter_call_delay', 0.5))
    return 0.5


async def check_title_appearance(item, page_list, start_index=1, provider=None):
    title=item['title']
    if 'physical_index' not in item or item['physical_index'] is None:
        return {'list_index': item.get('list_index'), 'answer': 'no', 'title':title, 'page_number': None}


    page_number = item['physical_index']
    page_text = page_list[page_number-start_index][0]

    heuristic = _title_match_heuristic(title, page_text)
    if heuristic is not None:
        return {'list_index': item['list_index'], 'answer': heuristic, 'title': title, 'page_number': page_number}

    prompt = f"""
    Your job is to check if the given section appears or starts in the given page_text.

    Note: do fuzzy matching, ignore any space inconsistency in the page_text.

    The given section title is {title}.
    The given page_text is {page_text}.

    Reply format:
    {{

        "thinking": <why do you think the section appears or starts in the page_text>
        "answer": "yes or no" (yes if the section appears or starts in the page_text, no otherwise)
    }}
    Directly return the final JSON structure. Do not output anything else."""

    response = await _llm_json(provider, prompt)
    answer = response.get('answer', 'no')
    return {'list_index': item['list_index'], 'answer': answer, 'title': title, 'page_number': page_number}


async def check_title_appearance_in_start(title, page_text, provider=None, logger=None):
    heuristic = _title_match_heuristic(title, page_text)
    if heuristic is not None:
        return heuristic

    prompt = f"""
    Your job is to check if the given section starts in the given page_text. We focus on whether the given section has started in the page.

    Note: do fuzzy matching, ignore any space inconsistency in the page_text.

    The given section title is {title}.
    The given page_text is {page_text}.

    Reply format:
    {{

        "thinking": <why do you think the section appears or starts in the page_text>
        "answer": "yes or no" (yes if the section appears or starts in the page_text, no otherwise)
    }}
    Directly return the final JSON structure. Do not output anything else."""

    response = await _llm_json(provider, prompt)
    return response.get('answer', 'no')


async def check_title_appearance_in_start_concurrent(toc_tree, page_list, provider=None, logger=None):
    async def process_item(item):
        if item.get('physical_index') is None:
            return item

        page_index = item['physical_index']
        page_list_index = page_index - 1

        if page_list_index < 0 or page_list_index >= len(page_list):
            return item

        page_text = page_list[page_list_index][0]
        answer = await check_title_appearance_in_start(item['title'], page_text, provider=provider, logger=logger)
        item['appear_start'] = answer
        return item

    # Process in small batches to avoid overwhelming rate limits
    _BATCH_SIZE = 5
    results = []
    for batch_start in range(0, len(toc_tree), _BATCH_SIZE):
        batch = toc_tree[batch_start:batch_start + _BATCH_SIZE]
        if batch_start > 0:
            await asyncio.sleep(1.5)
        batch_tasks = [process_item(item) for item in batch]
        batch_results = await asyncio.gather(*batch_tasks)
        results.extend(batch_results)
    return results


async def toc_detector_single_page(content, provider=None):
    heuristic = _toc_page_heuristic(content)
    if heuristic is not None:
        return heuristic

    prompt = f"""
    Your job is to detect if there is a table of content provided in the given text.

    Given text: {content}

    return the following JSON format:
    {{
        "thinking": <why do you think there is a table of content in the given text>
        "toc_detected": "<yes or no>",
    }}

    Directly return the final JSON structure. Do not output anything else.
    Please note: abstract,summary, notation list, figure list, table list, etc. are not table of contents."""

    json_content = await _llm_json(provider, prompt)
    return _safe_get(json_content, 'toc_detected', 'no')


async def check_if_toc_extraction_is_complete(content, toc, provider=None):
    return await _check_if_complete(content, toc, kind='extraction', provider=provider)


async def check_if_toc_transformation_is_complete(content, toc, provider=None):
    return await _check_if_complete(content, toc, kind='transformation', provider=provider)

async def extract_toc_content(content, provider=None):
    prompt = f"""
    Your job is to extract the full table of contents from the given text, replace ... with :

    Given text: {content}

    Directly return the full table of contents content. Do not output anything else."""

    response, finish_reason = await _llm_fr(provider, prompt)

    if_complete = await _check_if_complete(content, response, kind='transformation', provider=provider)
    if if_complete == "yes" and finish_reason == "finished":
        return response

    for _attempt in range(_MAX_CONTINUATION_ATTEMPTS):
        chat_history = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ]
        prompt = "please continue the generation of table of contents , directly output the remaining part of the structure"
        new_response, finish_reason = await _llm_fr(provider, prompt, chat_history)
        response = response + new_response
        if_complete = await _check_if_complete(content, response, kind='transformation', provider=provider)
        if if_complete == "yes" and finish_reason == "finished":
            return response
        _log.warning("extract_toc_content: attempt %d/%d incomplete", _attempt + 1, _MAX_CONTINUATION_ATTEMPTS)

    raise RuntimeError(f"extract_toc_content: failed to complete after {_MAX_CONTINUATION_ATTEMPTS} continuation attempts")

async def detect_page_index(toc_content, provider=None):
    _log.info('start detect_page_index')
    prompt = f"""
    You will be given a table of contents.

    Your job is to detect if there are page numbers/indices given within the table of contents.

    Given text: {toc_content}

    Reply format:
    {{
        "thinking": <why do you think there are page numbers/indices given within the table of contents>
        "page_index_given_in_toc": "<yes or no>"
    }}
    Directly return the final JSON structure. Do not output anything else."""

    json_content = await _llm_json(provider, prompt)
    return _safe_get(json_content, 'page_index_given_in_toc', 'no')

async def toc_extractor(page_list, toc_page_list, provider):
    def transform_dots_to_colon(text):
        text = re.sub(r'\.{5,}', ': ', text)
        # Handle dots separated by spaces
        text = re.sub(r'(?:\. ){5,}\.?', ': ', text)
        return text

    toc_content = ""
    for page_index in toc_page_list:
        toc_content += page_list[page_index][0]
    toc_content = transform_dots_to_colon(toc_content)
    has_page_index = await detect_page_index(toc_content, provider=provider)

    return {
        "toc_content": toc_content,
        "page_index_given_in_toc": has_page_index
    }




async def toc_index_extractor(toc, content, provider=None):
    _log.info('start toc_index_extractor')
    toc_extractor_prompt = """
    You are given a table of contents in a json format and several pages of a document, your job is to add the physical_index to the table of contents in the json format.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    The response should be in the following JSON format:
    [
        {
            "structure": <structure index, "x.x.x" or None> (string),
            "title": <title of the section>,
            "physical_index": "<physical_index_X>" (keep the format)
        },
        ...
    ]

    Only add the physical_index to the sections that are in the provided pages.
    If the section is not in the provided pages, do not add the physical_index to it.
    Directly return the final JSON structure. Do not output anything else."""

    prompt = toc_extractor_prompt + '\nTable of contents:\n' + str(toc) + '\nDocument pages:\n' + content
    json_content = await _llm_json(provider, prompt)
    return json_content



async def toc_transformer(toc_content, provider=None):
    _log.info('start toc_transformer')
    init_prompt = """
    You are given a table of contents, You job is to transform the whole table of content into a JSON format included table_of_contents.

    structure is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    The response should be in the following JSON format:
    {
    table_of_contents: [
        {
            "structure": <structure index, "x.x.x" or None> (string),
            "title": <title of the section>,
            "page": <page number or None>,
        },
        ...
        ],
    }
    You should transform the full table of contents in one go.
    Directly return the final JSON structure, do not output anything else. """

    prompt = init_prompt + '\n Given table of contents\n:' + toc_content
    last_complete, finish_reason = await _llm_fr(provider, prompt)
    if_complete = await _check_if_complete(toc_content, last_complete, kind='transformation', provider=provider)
    if if_complete == "yes" and finish_reason == "finished":
        last_complete = parse_json_robust(last_complete)
        cleaned_response=convert_page_to_int(last_complete['table_of_contents'])
        return cleaned_response

    last_complete = get_json_content(last_complete)
    for _attempt in range(_MAX_CONTINUATION_ATTEMPTS):
        position = last_complete.rfind('}')
        if position != -1:
            last_complete = last_complete[:position+2]
        prompt = f"""
        Your task is to continue the table of contents json structure, directly output the remaining part of the json structure.
        The response should be in the following JSON format:

        The raw table of contents json structure is:
        {toc_content}

        The incomplete transformed table of contents json structure is:
        {last_complete}

        Please continue the json structure, directly output the remaining part of the json structure."""

        new_complete, finish_reason = await _llm_fr(provider, prompt)

        if new_complete.startswith('```json'):
            new_complete = get_json_content(new_complete)
        last_complete = last_complete + new_complete

        if_complete = await _check_if_complete(toc_content, last_complete, kind='transformation', provider=provider)
        if if_complete == "yes" and finish_reason == "finished":
            break
        _log.warning("toc_transformer: attempt %d/%d incomplete", _attempt + 1, _MAX_CONTINUATION_ATTEMPTS)
    else:
        raise RuntimeError(f"toc_transformer: failed to complete after {_MAX_CONTINUATION_ATTEMPTS} continuation attempts")

    last_complete = json.loads(last_complete)
    cleaned_response = convert_page_to_int(last_complete['table_of_contents'])
    return cleaned_response




async def find_toc_pages(start_page_index, page_list, opt, logger=None):
    """
    Phase 4: detect TOC pages in parallel instead of one-at-a-time.

    All pages in the candidate window (up to toc_check_page_num) are checked
    concurrently via _gather_bounded.  The resulting yes/no list is then scanned
    in order to find the same first-contiguous-"yes" block that the old serial
    loop would have produced — so behaviour is identical, just faster.
    """
    _log.info('start find_toc_pages')

    end_idx = min(len(page_list), start_page_index + opt.toc_check_page_num)
    pages_to_check = list(range(start_page_index, end_idx))

    if not pages_to_check:
        if logger:
            logger.info('No pages to check for toc')
        return []

    concurrency = getattr(getattr(opt, 'pipeline', None), 'concurrency', 8)

    async def check_one(i):
        result = await toc_detector_single_page(page_list[i][0], provider=opt.provider)
        return i, result

    raw = await _gather_bounded([check_one(i) for i in pages_to_check], concurrency)
    results_map = dict(raw)

    # Reconstruct the first contiguous "yes" block — identical logic to original
    toc_page_list = []
    last_page_is_yes = False
    for i in pages_to_check:
        detected = results_map[i]
        if detected == 'yes':
            if logger:
                logger.info(f'Page {i} has toc')
            toc_page_list.append(i)
            last_page_is_yes = True
        elif detected == 'no' and last_page_is_yes:
            if logger:
                logger.info(f'Found the last page with toc: {i - 1}')
            break

    if not toc_page_list and logger:
        logger.info('No toc found')

    return toc_page_list

def remove_page_number(data):
    if isinstance(data, dict):
        data.pop('page_number', None)
        for key in list(data.keys()):
            if 'nodes' in key:
                remove_page_number(data[key])
    elif isinstance(data, list):
        for item in data:
            remove_page_number(item)
    return data

def extract_matching_page_pairs(toc_page, toc_physical_index, start_page_index):
    pairs = []
    # Both inputs must be lists of dicts; guard against _llm_json returning {} or scalar
    if not isinstance(toc_physical_index, list):
        toc_physical_index = []
    if not isinstance(toc_page, list):
        toc_page = []
    for phy_item in toc_physical_index:
        if not isinstance(phy_item, dict):
            continue
        for page_item in toc_page:
            if not isinstance(page_item, dict):
                continue
            if phy_item.get('title') == page_item.get('title'):
                physical_index = phy_item.get('physical_index')
                if physical_index is not None and int(physical_index) >= start_page_index:
                    pairs.append({
                        'title': phy_item.get('title'),
                        'page': page_item.get('page'),
                        'physical_index': physical_index
                    })
    return pairs


def calculate_page_offset(pairs):
    differences = []
    for pair in pairs:
        try:
            physical_index = pair['physical_index']
            page_number = pair['page']
            difference = physical_index - page_number
            differences.append(difference)
        except (KeyError, TypeError):
            continue

    if not differences:
        return None

    difference_counts = {}
    for diff in differences:
        difference_counts[diff] = difference_counts.get(diff, 0) + 1

    most_common = max(difference_counts.items(), key=lambda x: x[1])[0]

    return most_common

def add_page_offset_to_toc_json(data, offset):
    for i in range(len(data)):
        if data[i].get('page') is not None and isinstance(data[i]['page'], int):
            data[i]['physical_index'] = data[i]['page'] + offset
            del data[i]['page']

    return data



def page_list_to_group_text(page_contents, token_lengths, max_tokens=20000, overlap_page=1):
    num_tokens = sum(token_lengths)

    if num_tokens <= max_tokens:
        # merge all pages into one text
        page_text = "".join(page_contents)
        return [page_text]

    subsets = []
    current_subset = []
    current_token_count = 0

    expected_parts_num = math.ceil(num_tokens / max_tokens)
    average_tokens_per_part = math.ceil(((num_tokens / expected_parts_num) + max_tokens) / 2)

    for i, (page_content, page_tokens) in enumerate(zip(page_contents, token_lengths)):
        if current_token_count + page_tokens > average_tokens_per_part:

            subsets.append(''.join(current_subset))
            # Start new subset from overlap if specified
            overlap_start = max(i - overlap_page, 0)
            current_subset = page_contents[overlap_start:i]
            current_token_count = sum(token_lengths[overlap_start:i])

        # Add current page to the subset
        current_subset.append(page_content)
        current_token_count += page_tokens

    # Add the last subset if it contains any pages
    if current_subset:
        subsets.append(''.join(current_subset))

    _log.info('divided page_list into %d groups', len(subsets))
    return subsets

async def add_page_number_to_toc(part, structure, provider=None):
    fill_prompt_seq = """
    You are given an JSON structure of a document and a partial part of the document. Your task is to check if the title that is described in the structure is started in the partial given document.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    If the full target section starts in the partial given document, insert the given JSON structure with the "start": "yes", and "start_index": "<physical_index_X>".

    If the full target section does not start in the partial given document, insert "start": "no",  "start_index": None.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x" or None> (string),
                "title": <title of the section>,
                "start": "<yes or no>",
                "physical_index": "<physical_index_X> (keep the format)" or None
            },
            ...
        ]
    The given structure contains the result of the previous part, you need to fill the result of the current part, do not change the previous result.
    Directly return the final JSON structure. Do not output anything else."""

    prompt = fill_prompt_seq + f"\n\nCurrent Partial Document:\n{part}\n\nGiven Structure\n{json.dumps(structure, indent=2)}\n"
    json_result = await _llm_json(provider, prompt)

    cleaned = []
    for item in json_result:
        if not isinstance(item, dict):
            continue
        if 'start' in item:
            del item['start']
        cleaned.append(item)
    return cleaned


def remove_first_physical_index_section(text):
    """
    Removes the first section between <physical_index_X> and <physical_index_X> tags,
    and returns the remaining text.
    """
    pattern = r'<physical_index_\d+>.*?<physical_index_\d+>'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        # Remove the first matched section
        return text.replace(match.group(0), '', 1)
    return text

### add verify completeness
async def generate_toc_continue(toc_content, part, provider=None):
    _log.info('start generate_toc_continue')
    prompt = """
    You are an expert in extracting hierarchical tree structure.
    You are given a tree structure of the previous part and the text of the current part.
    Your task is to continue the tree structure from the previous part to include the current part.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    For the title, you need to extract the original title from the text, only fix the space inconsistency.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the start and end of page X. \

    For the physical_index, you need to extract the physical index of the start of the section from the text. Keep the <physical_index_X> format.

    The response should be in the following format.
        [
            {
                "structure": <structure index, "x.x.x"> (string),
                "title": <title of the section, keep the original title>,
                "physical_index": "<physical_index_X> (keep the format)"
            },
            ...
        ]

    Directly return the additional part of the final JSON structure. Do not output anything else."""

    prompt = prompt + '\nGiven text\n:' + part + '\nPrevious tree structure\n:' + json.dumps(toc_content, indent=2)
    response, finish_reason = await _llm_fr(provider, prompt)

    try:
        result = parse_json_robust(response)
        if isinstance(result, list):
            return result
    except Exception:
        pass

    if finish_reason == 'max_output_reached':
        _log.warning("generate_toc_continue: output truncated — returning empty list for this chunk")
        return []
    raise Exception(f'generate_toc_continue failed, finish reason: {finish_reason}')

### add verify completeness
async def generate_toc_init(part, provider=None):
    _log.info('start generate_toc_init')
    prompt = """
    You are an expert in extracting hierarchical tree structure, your task is to generate the tree structure of the document.

    The structure variable is the numeric system which represents the index of the hierarchy section in the table of contents. For example, the first section has structure index 1, the first subsection has structure index 1.1, the second subsection has structure index 1.2, etc.

    For the title, you need to extract the original title from the text, only fix the space inconsistency.

    The provided text contains tags like <physical_index_X> and <physical_index_X> to indicate the start and end of page X.

    For the physical_index, you need to extract the physical index of the start of the section from the text. Keep the <physical_index_X> format.

    The response should be in the following format.
        [
            {{
                "structure": <structure index, "x.x.x"> (string),
                "title": <title of the section, keep the original title>,
                "physical_index": "<physical_index_X> (keep the format)"
            }},

        ],


    Directly return the final JSON structure. Do not output anything else."""

    prompt = prompt + '\nGiven text\n:' + part
    response, finish_reason = await _llm_fr(provider, prompt)

    try:
        result = parse_json_robust(response)
        if isinstance(result, list):
            return result
    except Exception:
        pass

    if finish_reason == 'max_output_reached':
        _log.warning("generate_toc_init: output truncated — returning empty list for this chunk")
        return []
    raise Exception(f'generate_toc_init failed, finish reason: {finish_reason}')

async def process_no_toc(page_list, start_index=1, provider=None, opt=None, logger=None):
    page_contents=[]
    token_lengths=[]
    for page_index in range(start_index, start_index+len(page_list)):
        page_text = f"<physical_index_{page_index}>\n{page_list[page_index-start_index][0]}\n<physical_index_{page_index}>\n\n"
        page_contents.append(page_text)
        token_lengths.append(provider.count_tokens(page_text))
    group_texts = page_list_to_group_text(page_contents, token_lengths, max_tokens=_chunk_budget(opt))
    logger.info(f'len(group_texts): {len(group_texts)}')

    delay = _inter_call_delay(opt)
    total_groups = len(group_texts)
    _log.info('Processing group 1/%d (init)', total_groups)
    toc_with_page_number = await generate_toc_init(group_texts[0], provider)
    for idx, group_text in enumerate(group_texts[1:], start=2):
        await asyncio.sleep(delay)
        _log.info('Processing group %d/%d (continue)', idx, total_groups)
        toc_with_page_number_additional = await generate_toc_continue(toc_with_page_number, group_text, provider)
        toc_with_page_number.extend(toc_with_page_number_additional)
    _log.info('TOC generation complete: %d items extracted', len(toc_with_page_number))

    toc_with_page_number = convert_physical_index_to_int(toc_with_page_number)
    logger.info(f'convert_physical_index_to_int: {toc_with_page_number}')

    return toc_with_page_number

async def process_toc_no_page_numbers(toc_content, toc_page_list, page_list, start_index=1, provider=None, opt=None, logger=None):
    page_contents=[]
    token_lengths=[]
    toc_content = await toc_transformer(toc_content, provider)
    logger.info(f'toc_transformer: {toc_content}')
    for page_index in range(start_index, start_index+len(page_list)):
        page_text = f"<physical_index_{page_index}>\n{page_list[page_index-start_index][0]}\n<physical_index_{page_index}>\n\n"
        page_contents.append(page_text)
        token_lengths.append(provider.count_tokens(page_text))

    group_texts = page_list_to_group_text(page_contents, token_lengths, max_tokens=_chunk_budget(opt))
    logger.info(f'len(group_texts): {len(group_texts)}')

    delay = _inter_call_delay(opt)
    toc_with_page_number=copy.deepcopy(toc_content)
    total_groups = len(group_texts)
    for idx, group_text in enumerate(group_texts, start=1):
        if idx > 1:
            await asyncio.sleep(delay)
        _log.info('add_page_number_to_toc: group %d/%d', idx, total_groups)
        toc_with_page_number = await add_page_number_to_toc(group_text, toc_with_page_number, provider)
    logger.info(f'add_page_number_to_toc: {toc_with_page_number}')

    toc_with_page_number = convert_physical_index_to_int(toc_with_page_number)
    logger.info(f'convert_physical_index_to_int: {toc_with_page_number}')

    return toc_with_page_number



async def process_toc_with_page_numbers(toc_content, toc_page_list, page_list, toc_check_page_num=None, provider=None, logger=None):
    toc_with_page_number = await toc_transformer(toc_content, provider)
    logger.info(f'toc_with_page_number: {toc_with_page_number}')

    toc_no_page_number = remove_page_number(copy.deepcopy(toc_with_page_number))

    start_page_index = toc_page_list[-1] + 1
    main_content = ""
    for page_index in range(start_page_index, min(start_page_index + toc_check_page_num, len(page_list))):
        main_content += f"<physical_index_{page_index+1}>\n{page_list[page_index][0]}\n<physical_index_{page_index+1}>\n\n"

    toc_with_physical_index = await toc_index_extractor(toc_no_page_number, main_content, provider)
    logger.info(f'toc_with_physical_index: {toc_with_physical_index}')

    toc_with_physical_index = convert_physical_index_to_int(toc_with_physical_index)
    logger.info(f'toc_with_physical_index: {toc_with_physical_index}')

    matching_pairs = extract_matching_page_pairs(toc_with_page_number, toc_with_physical_index, start_page_index)
    logger.info(f'matching_pairs: {matching_pairs}')

    offset = calculate_page_offset(matching_pairs)
    logger.info(f'offset: {offset}')

    toc_with_page_number = add_page_offset_to_toc_json(toc_with_page_number, offset)
    logger.info(f'toc_with_page_number: {toc_with_page_number}')

    toc_with_page_number = await process_none_page_numbers(toc_with_page_number, page_list, provider=provider)
    logger.info(f'toc_with_page_number: {toc_with_page_number}')

    return toc_with_page_number



##check if needed to process none page numbers
async def process_none_page_numbers(toc_items, page_list, start_index=1, model=None, provider=None):
    for i, item in enumerate(toc_items):
        if "physical_index" not in item:
            # logger.info(f"fix item: {item}")
            # Find previous physical_index
            prev_physical_index = 0  # Default if no previous item exists
            for j in range(i - 1, -1, -1):
                if toc_items[j].get('physical_index') is not None:
                    prev_physical_index = toc_items[j]['physical_index']
                    break

            # Find next physical_index
            next_physical_index = -1  # Default if no next item exists
            for j in range(i + 1, len(toc_items)):
                if toc_items[j].get('physical_index') is not None:
                    next_physical_index = toc_items[j]['physical_index']
                    break

            page_contents = []
            for page_index in range(prev_physical_index, next_physical_index+1):
                # Add bounds checking to prevent IndexError
                list_index = page_index - start_index
                if list_index >= 0 and list_index < len(page_list):
                    page_text = f"<physical_index_{page_index}>\n{page_list[list_index][0]}\n<physical_index_{page_index}>\n\n"
                    page_contents.append(page_text)
                else:
                    continue

            item_copy = copy.deepcopy(item)
            del item_copy['page']
            result = await add_page_number_to_toc(page_contents, item_copy, provider)
            if result and isinstance(result[0], dict) and isinstance(result[0].get('physical_index'), str) and result[0]['physical_index'].startswith('<physical_index'):
                item['physical_index'] = int(result[0]['physical_index'].split('_')[-1].rstrip('>').strip())
                del item['page']

    return toc_items




async def check_toc(page_list, opt=None):
    toc_page_list = await find_toc_pages(start_page_index=0, page_list=page_list, opt=opt)
    if len(toc_page_list) == 0:
        _log.info('no toc found')
        return {'toc_content': None, 'toc_page_list': [], 'page_index_given_in_toc': 'no'}
    else:
        _log.info('toc found')
        toc_json = await toc_extractor(page_list, toc_page_list, opt.provider)

        if toc_json['page_index_given_in_toc'] == 'yes':
            _log.info('index found')
            return {'toc_content': toc_json['toc_content'], 'toc_page_list': toc_page_list, 'page_index_given_in_toc': 'yes'}
        else:
            current_start_index = toc_page_list[-1] + 1

            while (toc_json['page_index_given_in_toc'] == 'no' and
                   current_start_index < len(page_list) and
                   current_start_index < opt.toc_check_page_num):

                additional_toc_pages = await find_toc_pages(
                    start_page_index=current_start_index,
                    page_list=page_list,
                    opt=opt
                )

                if len(additional_toc_pages) == 0:
                    break

                additional_toc_json = await toc_extractor(page_list, additional_toc_pages, opt.provider)
                if additional_toc_json['page_index_given_in_toc'] == 'yes':
                    _log.info('index found')
                    return {'toc_content': additional_toc_json['toc_content'], 'toc_page_list': additional_toc_pages, 'page_index_given_in_toc': 'yes'}

                else:
                    current_start_index = additional_toc_pages[-1] + 1
            _log.info('index not found')
            return {'toc_content': toc_json['toc_content'], 'toc_page_list': toc_page_list, 'page_index_given_in_toc': 'no'}






################### fix incorrect toc #########################################################
async def single_toc_item_index_fixer(section_title, content, provider=None):
    toc_extractor_prompt = """
    You are given a section title and several pages of a document, your job is to find the physical index of the start page of the section in the partial document.

    The provided pages contains tags like <physical_index_X> and <physical_index_X> to indicate the physical location of the page X.

    Reply in a JSON format:
    {
        "thinking": <explain which page, started and closed by <physical_index_X>, contains the start of this section>,
        "physical_index": "<physical_index_X>" (keep the format)
    }
    Directly return the final JSON structure. Do not output anything else."""

    prompt = toc_extractor_prompt + '\nSection Title:\n' + str(section_title) + '\nDocument pages:\n' + content
    json_content = await _llm_json(provider, prompt)
    return convert_physical_index_to_int(_safe_get(json_content, 'physical_index', ''))



async def fix_incorrect_toc(toc_with_page_number, page_list, incorrect_results, start_index=1, provider=None, logger=None):
    _log.info('start fix_incorrect_toc with %d incorrect results', len(incorrect_results))
    incorrect_indices = {result['list_index'] for result in incorrect_results}

    end_index = len(page_list) + start_index - 1

    incorrect_results_and_range_logs = []
    # Helper function to process and check a single incorrect item
    async def process_and_check_item(incorrect_item):
        list_index = incorrect_item['list_index']

        # Check if list_index is valid
        if list_index < 0 or list_index >= len(toc_with_page_number):
            # Return an invalid result for out-of-bounds indices
            return {
                'list_index': list_index,
                'title': incorrect_item['title'],
                'physical_index': incorrect_item.get('physical_index'),
                'is_valid': False
            }

        # Find the previous correct item
        prev_correct = None
        for i in range(list_index-1, -1, -1):
            if i not in incorrect_indices and i >= 0 and i < len(toc_with_page_number):
                physical_index = toc_with_page_number[i].get('physical_index')
                if physical_index is not None:
                    prev_correct = physical_index
                    break
        # If no previous correct item found, use start_index
        if prev_correct is None:
            prev_correct = start_index - 1

        # Find the next correct item
        next_correct = None
        for i in range(list_index+1, len(toc_with_page_number)):
            if i not in incorrect_indices and i >= 0 and i < len(toc_with_page_number):
                physical_index = toc_with_page_number[i].get('physical_index')
                if physical_index is not None:
                    next_correct = physical_index
                    break
        # If no next correct item found, use end_index
        if next_correct is None:
            next_correct = end_index

        incorrect_results_and_range_logs.append({
            'list_index': list_index,
            'title': incorrect_item['title'],
            'prev_correct': prev_correct,
            'next_correct': next_correct
        })

        page_contents=[]
        for page_index in range(prev_correct, next_correct+1):
            # Add bounds checking to prevent IndexError
            list_index = page_index - start_index
            if list_index >= 0 and list_index < len(page_list):
                page_text = f"<physical_index_{page_index}>\n{page_list[list_index][0]}\n<physical_index_{page_index}>\n\n"
                page_contents.append(page_text)
            else:
                continue
        content_range = ''.join(page_contents)

        physical_index_int = await single_toc_item_index_fixer(incorrect_item['title'], content_range, provider)

        # Check if the result is correct
        check_item = incorrect_item.copy()
        check_item['physical_index'] = physical_index_int
        check_result = await check_title_appearance(check_item, page_list, start_index, provider)

        return {
            'list_index': list_index,
            'title': incorrect_item['title'],
            'physical_index': physical_index_int,
            'is_valid': check_result['answer'] == 'yes'
        }

    # Process incorrect items concurrently
    tasks = [
        process_and_check_item(item)
        for item in incorrect_results
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for item, result in zip(incorrect_results, results):
        if isinstance(result, Exception):
            _log.error("Processing item %s generated an exception: %s", item, result)
            continue
    results = [result for result in results if not isinstance(result, Exception)]

    # Update the toc_with_page_number with the fixed indices and check for any invalid results
    invalid_results = []
    for result in results:
        if result['is_valid']:
            # Add bounds checking to prevent IndexError
            list_idx = result['list_index']
            if 0 <= list_idx < len(toc_with_page_number):
                toc_with_page_number[list_idx]['physical_index'] = result['physical_index']
            else:
                # Index is out of bounds, treat as invalid
                invalid_results.append({
                    'list_index': result['list_index'],
                    'title': result['title'],
                    'physical_index': result['physical_index'],
                })
        else:
            invalid_results.append({
                'list_index': result['list_index'],
                'title': result['title'],
                'physical_index': result['physical_index'],
            })

    logger.info(f'incorrect_results_and_range_logs: {incorrect_results_and_range_logs}')
    logger.info(f'invalid_results: {invalid_results}')

    return toc_with_page_number, invalid_results



async def fix_incorrect_toc_with_retries(toc_with_page_number, page_list, incorrect_results, start_index=1, max_attempts=3, provider=None, logger=None):
    _log.info('start fix_incorrect_toc')
    fix_attempt = 0
    current_toc = toc_with_page_number
    current_incorrect = incorrect_results

    while current_incorrect:
        _log.info('Fixing %d incorrect results', len(current_incorrect))

        current_toc, current_incorrect = await fix_incorrect_toc(current_toc, page_list, current_incorrect, start_index, provider, logger)

        fix_attempt += 1
        if fix_attempt >= max_attempts:
            logger.info("Maximum fix attempts reached")
            break

    return current_toc, current_incorrect




################### verify toc #########################################################
async def verify_toc(page_list, list_result, start_index=1, N=None, provider=None):
    _log.info('start verify_toc')
    if not list_result:
        return 0, []
    # Find the last non-None physical_index
    last_physical_index = None
    for item in reversed(list_result):
        if item.get('physical_index') is not None:
            last_physical_index = item['physical_index']
            break

    # Early return if we don't have valid physical indices
    if last_physical_index is None or last_physical_index < len(page_list)/2:
        return 0, []

    # Determine which items to check
    if N is None:
        _log.info('check all items')
        sample_indices = range(0, len(list_result))
    else:
        N = min(N, len(list_result))
        _log.info('check %d items', N)
        sample_indices = random.sample(range(0, len(list_result)), N)

    # Prepare items with their list indices
    indexed_sample_list = []
    for idx in sample_indices:
        item = list_result[idx]
        # Skip items with None physical_index (these were invalidated by validate_and_truncate_physical_indices)
        if item.get('physical_index') is not None:
            item_with_index = item.copy()
            item_with_index['list_index'] = idx  # Add the original index in list_result
            indexed_sample_list.append(item_with_index)

    # Run checks in small batches to avoid overwhelming rate limits
    _BATCH_SIZE = 5
    results = []
    for batch_start in range(0, len(indexed_sample_list), _BATCH_SIZE):
        batch = indexed_sample_list[batch_start:batch_start + _BATCH_SIZE]
        if batch_start > 0:
            await asyncio.sleep(1.5)  # brief pause between batches
        batch_tasks = [
            check_title_appearance(item, page_list, start_index, provider)
            for item in batch
        ]
        batch_results = await asyncio.gather(*batch_tasks)
        results.extend(batch_results)

    # Process results
    correct_count = 0
    incorrect_results = []
    for result in results:
        if result['answer'] == 'yes':
            correct_count += 1
        else:
            incorrect_results.append(result)

    # Calculate accuracy
    checked_count = len(results)
    accuracy = correct_count / checked_count if checked_count > 0 else 0
    _log.info('accuracy: %.2f%%', accuracy * 100)
    return accuracy, incorrect_results





################### main process #########################################################
async def meta_processor(page_list, mode=None, toc_content=None, toc_page_list=None, start_index=1, opt=None, logger=None):
    _log.info('meta_processor mode: %s', mode)
    _log.info('start_index: %d', start_index)

    if mode == 'process_toc_with_page_numbers':
        toc_with_page_number = await process_toc_with_page_numbers(toc_content, toc_page_list, page_list, toc_check_page_num=opt.toc_check_page_num, provider=opt.provider, logger=logger)
    elif mode == 'process_toc_no_page_numbers':
        toc_with_page_number = await process_toc_no_page_numbers(toc_content, toc_page_list, page_list, provider=opt.provider, opt=opt, logger=logger)
    else:
        toc_with_page_number = await process_no_toc(page_list, start_index=start_index, provider=opt.provider, opt=opt, logger=logger)

    toc_with_page_number = [item for item in toc_with_page_number if item.get('physical_index') is not None]

    toc_with_page_number = validate_and_truncate_physical_indices(
        toc_with_page_number,
        len(page_list),
        start_index=start_index,
        logger=logger
    )

    accuracy, incorrect_results = await verify_toc(page_list, toc_with_page_number, start_index=start_index, provider=opt.provider)

    logger.info({
        'mode': 'process_toc_with_page_numbers',
        'accuracy': accuracy,
        'incorrect_results': incorrect_results
    })
    if accuracy == 1.0 and len(incorrect_results) == 0:
        return toc_with_page_number
    if accuracy > 0.6 and len(incorrect_results) > 0:
        toc_with_page_number, incorrect_results = await fix_incorrect_toc_with_retries(toc_with_page_number, page_list, incorrect_results, start_index=start_index, max_attempts=3, provider=opt.provider, logger=logger)
        return toc_with_page_number
    else:
        if mode == 'process_toc_with_page_numbers':
            return await meta_processor(page_list, mode='process_toc_no_page_numbers', toc_content=toc_content, toc_page_list=toc_page_list, start_index=start_index, opt=opt, logger=logger)
        elif mode == 'process_toc_no_page_numbers':
            return await meta_processor(page_list, mode='process_no_toc', start_index=start_index, opt=opt, logger=logger)
        else:
            raise Exception('Processing failed')


_MAX_RECURSION_DEPTH = 10

async def process_large_node_recursively(node, page_list, opt=None, logger=None, _depth=0):
    if _depth >= _MAX_RECURSION_DEPTH:
        _log.error(
            "process_large_node_recursively: max depth %d reached at node '%s' — stopping recursion",
            _MAX_RECURSION_DEPTH, node.get('title', '?')
        )
        return node

    node_page_list = page_list[node['start_index']-1:node['end_index']]
    token_num = sum([page[1] for page in node_page_list])

    if node['end_index'] - node['start_index'] > opt.max_page_num_each_node and token_num >= opt.max_token_num_each_node:
        _log.info('large node: %s  start=%d  end=%d  tokens=%d',
                  node['title'], node['start_index'], node['end_index'], token_num)

        node_toc_tree = await meta_processor(node_page_list, mode='process_no_toc', start_index=node['start_index'], opt=opt, logger=logger)
        node_toc_tree = await check_title_appearance_in_start_concurrent(node_toc_tree, page_list, provider=opt.provider, logger=logger)

        # Filter out items with None physical_index before post_processing
        valid_node_toc_items = [item for item in node_toc_tree if item.get('physical_index') is not None]

        if valid_node_toc_items and node['title'].strip() == valid_node_toc_items[0]['title'].strip():
            node['nodes'] = post_processing(valid_node_toc_items[1:], node['end_index'])
            node['end_index'] = valid_node_toc_items[1]['start_index'] if len(valid_node_toc_items) > 1 else node['end_index']
        else:
            node['nodes'] = post_processing(valid_node_toc_items, node['end_index'])
            node['end_index'] = valid_node_toc_items[0]['start_index'] if valid_node_toc_items else node['end_index']

    if 'nodes' in node and node['nodes']:
        tasks = [
            process_large_node_recursively(child_node, page_list, opt, logger=logger, _depth=_depth + 1)
            for child_node in node['nodes']
        ]
        await asyncio.gather(*tasks)

    return node

async def tree_parser(page_list, opt, doc=None, logger=None):
    check_toc_result = await check_toc(page_list, opt)
    logger.info(check_toc_result)

    if check_toc_result.get("toc_content") and check_toc_result["toc_content"].strip() and check_toc_result["page_index_given_in_toc"] == "yes":
        toc_with_page_number = await meta_processor(
            page_list,
            mode='process_toc_with_page_numbers',
            start_index=1,
            toc_content=check_toc_result['toc_content'],
            toc_page_list=check_toc_result['toc_page_list'],
            opt=opt,
            logger=logger)
    else:
        toc_with_page_number = await meta_processor(
            page_list,
            mode='process_no_toc',
            start_index=1,
            opt=opt,
            logger=logger)

    toc_with_page_number = add_preface_if_needed(toc_with_page_number)
    toc_with_page_number = await check_title_appearance_in_start_concurrent(toc_with_page_number, page_list, provider=opt.provider, logger=logger)

    # Filter out items with None physical_index before post_processings
    valid_toc_items = [item for item in toc_with_page_number if item.get('physical_index') is not None]

    toc_tree = post_processing(valid_toc_items, len(page_list))
    tasks = [
        process_large_node_recursively(node, page_list, opt, logger=logger)
        for node in toc_tree
    ]
    await asyncio.gather(*tasks)

    return toc_tree


def page_index_main(doc, opt=None):
    logger = JsonLogger(doc)

    is_valid_pdf = (
        (isinstance(doc, str) and os.path.isfile(doc) and doc.lower().endswith(".pdf")) or
        isinstance(doc, BytesIO)
    )
    if not is_valid_pdf:
        raise ValueError("Unsupported input type. Expected a PDF file path or BytesIO object.")

    if opt is None:
        opt = ConfigLoader().load()
    if not hasattr(opt, 'provider'):
        from .llm.factory import build_provider_from_opt
        try:
            opt.provider = build_provider_from_opt(opt)
        except Exception as exc:
            raise ValueError(f"Failed to initialise LLM provider: {exc}") from exc

    _log.info("Parsing PDF...")
    page_list = get_page_tokens(doc)

    if not page_list:
        raise ValueError("PDF appears to be empty — no pages could be extracted.")

    logger.info({'total_page_number': len(page_list)})
    logger.info({'total_token': sum([page[1] for page in page_list])})

    timeout = getattr(getattr(opt, 'pipeline', None), 'timeout_seconds', None)

    async def page_index_builder():
        structure = await tree_parser(page_list, opt, doc=doc, logger=logger)
        if opt.if_add_node_id == 'yes':
            write_node_id(structure)
        if opt.if_add_node_text == 'yes':
            add_node_text(structure, page_list)
        if opt.if_add_node_summary == 'yes':
            if opt.if_add_node_text == 'no':
                add_node_text(structure, page_list)
            await generate_summaries_for_structure(structure, provider=opt.provider)
            if opt.if_add_node_text == 'no':
                remove_structure_text(structure)
            if opt.if_add_doc_description == 'yes':
                # Create a clean structure without unnecessary fields for description generation
                clean_structure = create_clean_structure_for_description(structure)
                doc_description = await generate_doc_description(clean_structure, provider=opt.provider)
                return {
                    'doc_name': get_pdf_name(doc),
                    'doc_description': doc_description,
                    'structure': structure,
                }
        return {
            'doc_name': get_pdf_name(doc),
            'structure': structure,
        }

    async def _run():
        if timeout:
            try:
                return await asyncio.wait_for(page_index_builder(), timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"page_index_main timed out after {timeout}s. "
                    "Increase pipeline.timeout_seconds in config.yaml to allow more time."
                )
        return await page_index_builder()

    return asyncio.run(_run())


def page_index(doc, model=None, toc_check_page_num=None, max_page_num_each_node=None, max_token_num_each_node=None,
               if_add_node_id=None, if_add_node_summary=None, if_add_doc_description=None, if_add_node_text=None):

    user_opt = {
        arg: value for arg, value in locals().items()
        if arg != "doc" and value is not None
    }
    opt = ConfigLoader().load(user_opt)
    return page_index_main(doc, opt)


def validate_and_truncate_physical_indices(toc_with_page_number, page_list_length, start_index=1, logger=None):
    """
    Validates and truncates physical indices that exceed the actual document length.
    This prevents errors when TOC references pages that don't exist in the document (e.g. the file is broken or incomplete).
    """
    if not toc_with_page_number:
        return toc_with_page_number

    max_allowed_page = page_list_length + start_index - 1
    truncated_items = []

    for i, item in enumerate(toc_with_page_number):
        if item.get('physical_index') is not None:
            original_index = item['physical_index']
            if original_index > max_allowed_page:
                item['physical_index'] = None
                truncated_items.append({
                    'title': item.get('title', 'Unknown'),
                    'original_index': original_index
                })
                if logger:
                    logger.info(f"Removed physical_index for '{item.get('title', 'Unknown')}' (was {original_index}, too far beyond document)")

    if truncated_items and logger:
        logger.info(f"Total removed items: {len(truncated_items)}")

    _log.info('Document validation: %d pages, max allowed index: %d', page_list_length, max_allowed_page)
    if truncated_items:
        _log.info('Truncated %d TOC items that exceeded document length', len(truncated_items))

    return toc_with_page_number
