from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from openrouter_llm import build_openrouter_chat_llm, openrouter_logging_callbacks, read_prompt_text


def _try_parse_json_object(text: str) -> Tuple[Dict[str, Any], str]:
    """Best-effort parse of a JSON object from model text.

    Returns (parsed_dict, source_tag).
    """
    raw = (text or "").strip()
    if not raw:
        return {}, "empty"

    # First: direct parse.
    try:
        obj = json.loads(raw)
        return (obj if isinstance(obj, dict) else {}), "direct"
    except Exception:
        pass

    # Second: fenced ```json blocks.
    fence = re.search(r"```json\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            obj2 = json.loads(fence.group(1))
            return (obj2 if isinstance(obj2, dict) else {}), "fenced"
        except Exception:
            pass

    # Third: try the outermost {...} span.
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidate = raw[start : end + 1]
        try:
            obj3 = json.loads(candidate)
            return (obj3 if isinstance(obj3, dict) else {}), "substring"
        except Exception:
            pass

    return {}, "unparsed"


_ARC_SUMMARIZER_PROMPT = """\
You are an arc summarizer for a tabletop RPG log.

Input:
- A JSON object with:
  - "paragraphs": list of story paragraphs, each with name, summary, locations, characters, npcs
  - "existing_arc_names": list of arc names already used (do NOT reuse any of these)

Task:
- Produce a concise arc summary that covers the full story arc shown across all paragraphs.

Output:
Return STRICT JSON with keys:
- arc_name: a new title for this arc (3-10 words, must differ from all existing_arc_names)
- arc_summary: 8-14 sentence summary of the full arc trajectory, key characters, locations visited, major decisions, and durable consequences

Rules:
- Keep it in-world (no meta commentary).
- Prioritize detalization over narative beauty, all facts and important details should be kept.
- Do not include markdown.
- arc_summary must cover all major events and consequences across every paragraph.
"""


def summarize_arc(
    *,
    paragraphs: List[Dict[str, Any]],
    existing_arc_names: Optional[List[str]] = None,
    llm: Optional[ChatOpenAI] = None,
) -> Dict[str, str]:
    """Summarize completed arc paragraphs into an arc name and summary.

    Returns: {"arc_name": str, "arc_summary": str}
    """
    if llm is None:
        llm = build_openrouter_chat_llm(temperature=0.3, title_suffix="-summarizer")

    llm = llm.with_config({"callbacks": openrouter_logging_callbacks(scope="summarizer")})

    blob = json.dumps(
        {
            "paragraphs": [
                {
                    "name": str(p.get("name") or ""),
                    "summary": str(p.get("summary") or ""),
                    "locations": list(p.get("locations") or []),
                    "characters": list(p.get("characters") or []),
                    "npcs": list(p.get("npcs") or []),
                }
                for p in paragraphs
                if isinstance(p, dict)
            ],
            "existing_arc_names": list(existing_arc_names or []),
        },
        ensure_ascii=False,
        indent=2,
    )

    msg = llm.invoke(
        [
            SystemMessage(content=_ARC_SUMMARIZER_PROMPT),
            HumanMessage(content=blob),
        ]
    )

    text = (getattr(msg, "content", "") or "").strip()
    parsed, _ = _try_parse_json_object(text)
    arc_name = str(parsed.get("arc_name") or "").strip()
    arc_summary = str(parsed.get("arc_summary") or "").strip()

    return {"arc_name": arc_name, "arc_summary": arc_summary}


def summarize_ongoing_paragraph(
    *,
    world_context: str,
    ongoing_paragraph: Dict[str, Any],
    prompt_path: str = "agents/summarizer/prompt.txt",
    llm: Optional[ChatOpenAI] = None,
) -> Dict[str, str]:
    """Summarize an ongoing paragraph (expected ~10 turns) into a named paragraph.

    Returns: {"name": str, "summary": str}
    """

    def _trim(text: str, max_chars: int) -> str:
        t = (text or "").strip()
        return t

    def _extract_first_sentence(text: str, max_chars: int = 220) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        # Prefer the first non-empty line.
        for line in t.splitlines():
            s = line.strip()
            if s:
                t = s
                break
        # Then try to cut at sentence boundary.
        m = re.search(r"[.!?](?:\s|$)", t)
        if m:
            t = t[: m.end()].strip()
        return _trim(t, max_chars)

    def _heuristic_summary(ongoing: Dict[str, Any]) -> Dict[str, str]:
        turns = ongoing.get("turns") if isinstance(ongoing, dict) else None
        turns_list = turns if isinstance(turns, list) else []

        locations: list[str] = []
        characters: list[str] = []
        npcs: list[str] = []
        try:
            if isinstance(ongoing.get("locations"), list):
                locations = [str(x) for x in ongoing.get("locations") if str(x).strip()]
            if isinstance(ongoing.get("characters"), list):
                characters = [str(x) for x in ongoing.get("characters") if str(x).strip()]
            if isinstance(ongoing.get("npcs"), list):
                npcs = [str(x) for x in ongoing.get("npcs") if str(x).strip()]
        except Exception:
            pass

        header_bits: list[str] = []
        if locations:
            header_bits.append(f"Locations: {', '.join(locations)}.")
        if characters:
            header_bits.append(f"Characters: {', '.join(characters)}.")
        if npcs:
            header_bits.append(f"NPCs: {', '.join(npcs)}.")

        lines: list[str] = []
        for t in turns_list[:10]:
            if not isinstance(t, dict):
                continue
            loc = str(t.get("location") or "").strip()
            narr = str(t.get("narration") or "").strip()
            beat = _extract_first_sentence(narr, max_chars=220)
            if not beat:
                continue
            prefix = f"[{loc}] " if loc else ""
            lines.append(prefix + beat)

        if not lines:
            summary = "A sequence of events unfolded over several turns."
        else:
            # Keep it compact but grounded in actual narration.
            summary = " ".join(header_bits + lines)
            summary = _trim(summary, 1600)

        return {"name": "Summary", "summary": summary}

    def _heuristic_name(ongoing: Dict[str, Any]) -> str:
        # Prefer a short, stable title derived from participants and first location.
        locations = ongoing.get("locations") if isinstance(ongoing, dict) else None
        characters = ongoing.get("characters") if isinstance(ongoing, dict) else None

        loc = ""
        if isinstance(locations, list) and locations:
            loc = str(locations[0] or "").strip()

        chars: list[str] = []
        if isinstance(characters, list):
            chars = [str(x).strip() for x in characters if str(x).strip()]

        if loc and chars:
            if len(chars) >= 2:
                # Keep it within 3–10 words as per prompt.
                return f"{chars[0]} & {chars[1]} in {loc}".strip()
            return f"{chars[0]} in {loc}".strip()
        if loc:
            return f"Events in {loc}".strip()
        if chars:
            if len(chars) >= 2:
                return f"{chars[0]} & {chars[1]}".strip()
            return chars[0]
        return "Summary"

    prompt_text = read_prompt_text(prompt_path)

    if llm is None:
        llm = build_openrouter_chat_llm(temperature=0.3, title_suffix="-summarizer")

    llm = llm.with_config({"callbacks": openrouter_logging_callbacks(scope="summarizer")})

    blob = json.dumps(
        {
            "world_context": world_context,
            "ongoing_paragraph": ongoing_paragraph,
        },
        ensure_ascii=False,
        indent=2,
    )

    msg = llm.invoke(
        [
            SystemMessage(content=prompt_text),
            HumanMessage(content=blob),
        ]
    )

    text = (getattr(msg, "content", "") or "").strip()
    parsed, parse_source = _try_parse_json_object(text)

    name = str(parsed.get("name") or "").strip()
    summary = str(parsed.get("summary") or "").strip()

    if (not name) or name.lower() == "summary":
        name = _heuristic_name(ongoing_paragraph)
    if not summary:
        # Deterministic fallback grounded in actual turn narrations.
        hs = _heuristic_summary(ongoing_paragraph)
        name = name or hs.get("name") or _heuristic_name(ongoing_paragraph)
        summary = hs.get("summary") or "A sequence of events unfolded over several turns."
        return {"name": name, "summary": summary}

    return {"name": name, "summary": summary}
