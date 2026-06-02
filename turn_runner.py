"""Storage-Assistant turn-orchestration loop.

Extracted from console_app.py so that ConsoleApp stays focused on
lifecycle/state management.  ConsoleApp.invoke_once() is a thin wrapper
that calls run_turn().
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

from gm.full_history import trim_full_gm_messages
from gm.tools import (
    gm_allowed_tools,
    gm_tools_for_current_context,
    is_context_changed,
    is_scene_request_pending,
    reset_turn_lock,
)
from memory_store import HistoryLimits, approx_token_count
from openrouter_langchain_logging import logs_enabled
from stream_watchdog import (
    InvalidGMOutputError,
    StreamWatchdog,
    _clear_watchdog_abort,
    clear_detected_invalid_pattern,
    get_detected_invalid_pattern,
    log_retry_with_correction,
)

if TYPE_CHECKING:
    from console_app import ConsoleApp


def run_turn(app: "ConsoleApp", user_msg: Optional[HumanMessage]) -> None:
    """Execute one Storage Assistant turn.  Called by ConsoleApp.invoke_once()."""
    app.world.ensure_initialized()
    app._clear_turn_temp_dir()
    reset_turn_lock()
    app._ensure_sa_bootstrap()

    if app._maybe_run_world_seed():
        return

    # Hard guard: do not progress simulation while world time is still placeholder.
    try:
        if app.world.get_world_time().to_string() == "Y0000-01-01 00:00:00":
            if logs_enabled():
                print("[trace] invoke_once: world time still default; waiting for valid world_seed time")
            return
    except Exception:
        pass

    if app._maybe_run_scene_description():
        return

    if is_scene_request_pending():
        if logs_enabled():
            print("[trace] invoke_once: scene request still pending; skipping SA invocation")
        return

    # Scene request remains SA-driven via run_scene, but scene pick/description
    # and activation are completed automatically by Python orchestration.

    if app._finalize_turn_if_ready():
        return

    # Freeze SA while a scene is actively running.
    try:
        _scene_after_finalize = app.world.get_scene()
        _state = str((_scene_after_finalize or {}).get("state") or "").strip()
        if _state == "active":
            if logs_enabled():
                print(f"[trace] invoke_once: scene in progress (state={_state}); skipping SA invocation")
            return
    except Exception:
        pass

    # SA remains responsible for storage updates; scene requests use run_scene only.

    context_tools = gm_tools_for_current_context()
    tool_names = {t.name if hasattr(t, 'name') else str(t) for t in context_tools}

    if logs_enabled():
        print(f"[trace] invoke_once starting with tools: {sorted(tool_names)}")

    # When only read-only tools are available (active scene, characters haven't
    # acted yet) and there is no explicit user message, the SA has no state
    # mutations it can perform.  Skip the invocation entirely so the
    # auto-advance loop can proceed to character execution and turn finalization.
    _READ_ONLY_SA_TOOLS = {"get_location", "get_character_detail"}
    if user_msg is None and tool_names.issubset(_READ_ONLY_SA_TOOLS):
        if logs_enabled():
            print("[trace] invoke_once: only read-only tools available; skipping SA invocation")
        return

    # Recreate the GM with a context-appropriate tool set at the start of each invocation.
    # This avoids bloating the prompt with "don't use X" rules and improves reliability.
    graph = app._gm_factory.build(tools=context_tools)

    # IMPORTANT: keep in-memory history safely below the configured budget.
    # Even if persisted history is trimmed, the combined prompt size can still
    # exceed the provider/model limit due to system prompt + injected world context.
    # Leave headroom by trimming more aggressively when we're close to the cap.
    try:
        msgs = list(app.state.get("messages") or [])
        msgs = app._strip_tool_error_pairs(msgs)
        approx_tokens = 0
        for m in msgs:
            approx_tokens += approx_token_count(str(getattr(m, "content", "") or ""))

        max_hist = int(app.limits.max_history_tokens)
        # Leave ~20% headroom for system prompt + injected world context + tool schemas.
        headroom_target = int(max_hist * 0.8)
        if approx_tokens > headroom_target:
            tightened = HistoryLimits(
                model_max_context_tokens=int(app.limits.model_max_context_tokens),
                history_fraction=min(0.25, float(app.limits.history_fraction or 0.5)),
            )
            tightened_turns = max(1, int(app.max_turns) // 2)
            trimmed_msgs = trim_full_gm_messages(
                msgs,
                limits=tightened,
                max_turns=tightened_turns,
            )
            app.state = {"messages": list(trimmed_msgs)}
    except Exception:
        pass

    def _build_base_invoke_messages() -> List[Any]:
        msgs: List[Any] = list(app.state.get("messages") or [])
        if user_msg is not None:
            msgs.append(user_msg)
        return msgs

    base_invoke_messages = _build_base_invoke_messages()

    INTERNAL_RETRY_MARKER = "[internal_retry_v1]"

    def _retry_base_from_state(state: Dict[str, Any], *, drop_tool_errors: bool = False) -> List[Any]:
        """Retry from a prior state, keeping tool results but dropping injected system context and invalid AI text.
        
        If drop_tool_errors is True, also drop tool messages that contain error patterns
        (e.g., "is not a valid tool", "Context changed"). This is used when context changed
        and old error messages would confuse the model about which tools are now available.
        When dropping tool errors, we also remove the corresponding tool_calls from AI messages
        to keep the conversation consistent.
        """

        msgs = list(state.get("messages") or [])
        
        # First pass: identify tool_call_ids to drop (if drop_tool_errors)
        dropped_tool_call_ids: set[str] = set()
        if drop_tool_errors:
            for m in msgs:
                t = getattr(m, "type", "")
                if t == "tool":
                    content = str(getattr(m, "content", "") or "").lower()
                    if "is not a valid tool" in content or "context changed" in content:
                        tc_id = getattr(m, "tool_call_id", "") or ""
                        if tc_id:
                            dropped_tool_call_ids.add(tc_id)
        
        out: List[Any] = []
        for m in msgs:
            if getattr(m, "type", "") == "system":
                continue
            # Drop any AI messages with invalid text (rambling, pseudo-markup)
            # These confuse the model on retry by making it continue bad patterns
            t = getattr(m, "type", "")
            if t in {"ai", "assistant"}:
                content = str(getattr(m, "content", "") or "")
                # Keep AI messages that have tool calls (even if they have some text)
                tool_calls = getattr(m, "tool_calls", None) or []
                has_tool_calls = bool(tool_calls)
                if not has_tool_calls and app._is_invalid_gm_text_output(content):
                    continue  # Skip this invalid text-only AI message
                # If we're dropping tool errors, filter out the dropped tool_calls from AI messages
                if drop_tool_errors and dropped_tool_call_ids and tool_calls:
                    filtered_calls = [tc for tc in tool_calls if tc.get("id", "") not in dropped_tool_call_ids]
                    if filtered_calls != tool_calls:
                        # Create a new AI message with filtered tool_calls
                        m = AIMessage(
                            content=content,
                            tool_calls=filtered_calls,
                            additional_kwargs=getattr(m, "additional_kwargs", {}) or {},
                        )
                        # Skip entirely if no tool calls left and content is empty/invalid
                        if not filtered_calls and (not content.strip() or app._is_invalid_gm_text_output(content)):
                            continue
            # Drop tool error messages
            if drop_tool_errors and t == "tool":
                tc_id = getattr(m, "tool_call_id", "") or ""
                if tc_id in dropped_tool_call_ids:
                    continue  # Skip stale tool error message
            out.append(m)
        return out

    injected_retry_contents: set[str] = set()
    
    # Create stream watchdog for early abort on invalid patterns
    watchdog = StreamWatchdog()

    def _try_invoke(messages: List[Any]) -> Dict[str, Any]:
        """Invoke the graph with watchdog for early abort on invalid patterns."""
        # Clear any previously detected pattern and abort flags
        clear_detected_invalid_pattern()
        _clear_watchdog_abort()
        
        # Start watchdog before invoke
        watchdog.start()
        try:
            result = graph.invoke({"messages": messages})
        except KeyboardInterrupt as ki:
            # Watchdog triggered abort via KeyboardInterrupt
            pattern = watchdog.get_detected_pattern() or get_detected_invalid_pattern()
            if pattern:
                if logs_enabled():
                    print(f"\n[trace] Watchdog aborted stream: {pattern}")
                raise InvalidGMOutputError(pattern, "")
            raise  # Re-raise if not from watchdog
        finally:
            watchdog.stop()
        
        # Check if streaming detected an invalid pattern (backup check)
        detected_pattern = get_detected_invalid_pattern()
        if detected_pattern:
            raise InvalidGMOutputError(detected_pattern, "")
        return result

    def _looks_like_context_overflow(err: Exception) -> bool:
        s = str(err or "").lower()
        # Provider/model-specific phrasings.
        return any(
            p in s
            for p in [
                "context length",
                "maximum context length",
                "context_length_exceeded",
                "max context",
                "too many tokens",
                "prompt is too long",
                "input is too long",
            ]
        )

    # Initialize out_state in case all retries fail
    out_state: Dict[str, Any] = {"messages": base_invoke_messages}
    
    try:
        out_state = _try_invoke(base_invoke_messages)
    except InvalidGMOutputError as ige:
        # Streaming detected invalid pattern (```json, etc.) - retry with correction
        # Use a loop to allow multiple retries for persistent invalid output patterns
        max_invalid_retries = 5
        current_pattern = ige.pattern
        current_messages = base_invoke_messages
        
        for invalid_retry in range(max_invalid_retries):
            if logs_enabled():
                print(f"[trace] InvalidGMOutputError: detected '{current_pattern}' in stream; retrying with correction ({invalid_retry + 1}/{max_invalid_retries})")
            # Log to watchdog log file
            log_retry_with_correction(current_pattern)
            retry_content = (
                f"{INTERNAL_RETRY_MARKER}\n"
                f"STOP. Your output contained '{current_pattern}' which is INVALID. "
                "You must NOT output JSON blocks, markdown, or explanatory text. "
                "Use ONLY the built-in tool calling interface. "
                "Call exactly one tool now."
            )
            injected_retry_contents.add(retry_content)
            
            try:
                out_state = _try_invoke(current_messages + [HumanMessage(content=retry_content)])
                break  # Success - exit retry loop
            except InvalidGMOutputError as ige2:
                current_pattern = ige2.pattern
                # Continue loop for next retry
        else:
            # Exhausted all retries - log and continue with fallback state
            if logs_enabled():
                print(f"[trace] Exhausted {max_invalid_retries} retries for invalid output patterns; continuing with base messages")
            # out_state already initialized with base messages above
    except Exception as e:  # noqa: BLE001
        # If we hit a context-length error, aggressively trim in-memory history and retry once.
        if _looks_like_context_overflow(e):
            if logs_enabled():
                print("[trace] gm context overflow detected; trimming history and retrying")

            # Keep a smaller fraction of history for the retry.
            tightened = HistoryLimits(
                model_max_context_tokens=int(app.limits.model_max_context_tokens),
                history_fraction=min(0.25, float(app.limits.history_fraction or 0.5)),
            )
            tightened_turns = max(1, int(app.max_turns) // 2)

            trimmed_msgs = trim_full_gm_messages(
                list(app.state.get("messages") or []),
                limits=tightened,
                max_turns=tightened_turns,
            )
            app.state = {"messages": list(trimmed_msgs)}

            base_invoke_messages = _build_base_invoke_messages()

            out_state = _try_invoke(base_invoke_messages)
        else:
            # Error-recovery only: nudge the model to use the native tool-calling interface.
            retry_content = (
                f"{INTERNAL_RETRY_MARKER}\n"
                "Tool invocation failed due to invalid tool-call formatting. "
                "Do NOT output <function_calls>/<invoke> blocks; use the tool calling interface. "
                "Continue from the current storage state. "
                f"Error: {e}"
            )
            injected_retry_contents.add(retry_content)
            out_state = _try_invoke(base_invoke_messages + [HumanMessage(content=retry_content)])

    if logs_enabled():
        print("[trace] passed try/except block, checking for invalid AI text")

    # Check for invalid GM text output FIRST (even before context change handling)
    # so we catch JSON dumps and long monologues before exiting.
    # Check ALL AI messages from this invocation, not just the last one,
    # because the model may ramble and then make a tool call.
    def _get_all_new_ai_texts(state: Dict[str, Any], base_count: int) -> List[str]:
        msgs = list(state.get("messages") or [])
        new_msgs = msgs[base_count:] if base_count < len(msgs) else []
        texts = []
        for m in new_msgs:
            t = getattr(m, "type", "")
            if t in {"ai", "assistant"}:
                content = str(getattr(m, "content", "") or "")
                if content.strip():
                    texts.append(content)
        return texts

    base_msg_count = len(base_invoke_messages)
    all_new_ai_texts = _get_all_new_ai_texts(out_state, base_msg_count)
    
    # Find the worst offender among new AI texts
    invalid_ai_text = ""
    for ai_text in all_new_ai_texts:
        if app._is_invalid_gm_text_output(ai_text):
            invalid_ai_text = ai_text
            break
    
    if invalid_ai_text:
        app._log_pseudo_tool_markup_event(invalid_ai_text)
        
        is_pseudo_markup = app._looks_like_pseudo_tool_markup(invalid_ai_text)
        word_count = len((invalid_ai_text or "").strip().split())
        is_long_monologue = word_count > 50  # Must match threshold in _is_invalid_gm_text_output
        
        if logs_enabled():
            if is_long_monologue and not is_pseudo_markup:
                print(f"[trace] gm output was too long ({word_count} words); retrying with tool-call-only instruction")
            else:
                print("[trace] gm output looked like pseudo tool markup; retrying with tool-call-only instruction")

        if is_long_monologue and not is_pseudo_markup:
            retry_content = (
                f"{INTERNAL_RETRY_MARKER}\n"
                f"Your last output was {word_count} words of text. You must NOT write explanatory text, reports, or commentary. "
                "You output ONLY via tool calls. Call exactly one tool now using the native tool-calling mechanism."
            )
        elif '```json' in invalid_ai_text.lower():
            retry_content = (
                f"{INTERNAL_RETRY_MARKER}\n"
                "Your last output contained ```json blocks which will NOT execute. "
                "Do NOT dump JSON or scene state as text. "
                "Use ONLY the built-in tool calling interface. "
                "Call exactly one tool now using the native tool-calling mechanism."
            )
        else:
            retry_content = (
                f"{INTERNAL_RETRY_MARKER}\n"
                "Your last output contained invalid markup or JSON which will NOT execute. "
                "Use ONLY the built-in LangChain tool calling interface. "
                "Call exactly one tool now using the native tool-calling mechanism."
            )
        injected_retry_contents.add(retry_content)
        # Don't retry if context changed - exit for fresh tool bindings instead
        if not is_context_changed():
            out_state = _try_invoke(_retry_base_from_state(out_state) + [HumanMessage(content=retry_content)])

    # Check for context change after invoke completes.
    # If tools changed (e.g., run_scene triggered scene activation), save state
    # and exit so the auto-advance loop restarts with fresh tool bindings.
    ctx_changed = is_context_changed()
    if logs_enabled():
        print(f"[trace] post-invoke context change check: is_context_changed()={ctx_changed}")
    if ctx_changed:
        if logs_enabled():
            print("[trace] context changed after invoke; saving state and exiting for fresh tool bindings")
        # Clean state: strip invalid AI text AND tool error messages that would
        # confuse the model about which tools are now available
        try:
            cleaned_messages = _retry_base_from_state(out_state, drop_tool_errors=True)
            app.state = {"messages": cleaned_messages}
            # Use the save_gm_history method to persist cleaned state
            app.save_gm_history()
            if logs_enabled():
                print("[trace] context change cleanup successful; returning from invoke_once")
        except Exception as e:
            if logs_enabled():
                print(f"[trace] context change cleanup failed: {e}")
            raise
        return

    def _state_includes_tool_call(state: Dict[str, Any], tool_name: str) -> bool:
        for m in (state.get("messages") or []):
            try:
                if getattr(m, "type", "") == "tool" and str(getattr(m, "name", "") or "") == tool_name:
                    return True
            except Exception:
                continue
        return False

    def _count_tool_messages(msgs: List[Any]) -> int:
        n = 0
        for m in msgs:
            try:
                if getattr(m, "type", "") == "tool":
                    n += 1
            except Exception:
                continue
        return n

    def _last_tool_message(msgs: List[Any]) -> Tuple[str, str]:
        for m in reversed(msgs):
            try:
                if getattr(m, "type", "") != "tool":
                    continue
                name = str(getattr(m, "name", "") or "").strip()
                content = str(getattr(m, "content", "") or "").strip()
                return (name, content)
            except Exception:
                continue
        return ("", "")

    def _tick_remaining_characters() -> List[str]:
        try:
            scene = app.world.get_scene()
            if not (isinstance(scene, dict) and scene.get("state") == "active"):
                return []
            chars = scene.get("characters") if isinstance(scene.get("characters"), dict) else {}
            remaining = [
                str(name).strip()
                for name, entry in chars.items()
                if not (entry if isinstance(entry, dict) else {}).get("acted")
                and str(name).strip()
            ]
            return remaining
        except Exception:
            return []

    def _tool_error_is_blocking(tool_name: str, tool_content: str) -> bool:
        s = (tool_content or "").strip().lower()
        if not s:
            return False
        if s.startswith("error:"):
            return True
        # Common recovery-worthy tool errors.
        return any(
            p in s
            for p in [
                "not available in the current context",
                "please fix your mistakes",
                "not all characters ended",
                "is not valid right now",
            ]
        )

    def _tick_retry_instruction() -> str:
        allowed = []
        try:
            allowed = list(gm_allowed_tools())
        except Exception:
            allowed = []

        last_tool_name, last_tool_content = _last_tool_message(list(out_state.get("messages") or []))
        last_tool_err_line = ""
        if last_tool_content and _tool_error_is_blocking(last_tool_name, last_tool_content):
            snippet = last_tool_content
            if "no active scene" not in snippet.lower():
                last_tool_err_line = f"Last tool error ({last_tool_name}): {snippet}\n"

        remaining = _tick_remaining_characters()
        allowed_str = ", ".join([str(x) for x in allowed if str(x).strip()])

        if remaining:
            target = remaining[0]
            # Provide guidance that characters execute automatically
            suggestion = (
                f"Scene in progress. {len(remaining)} character(s) still to act: {', '.join(remaining)}.\n"
                "Characters act automatically - DO NOT attempt to control them.\n"
                "Wait for all characters to complete; turn will finalize automatically."
            )
        elif "run_scene" in allowed:
            suggestion = (
                "You MUST call run_scene to begin the next selected scene. "
                "Do not call maintenance tools (update_character, update_location, etc.) - start the scene first."
            )
        elif allowed_str:
            suggestion = (
                "Call exactly one tool from the allowed list that advances the world state. "
                "Do not write narration or project summaries."
            )
        else:
            suggestion = "No tools appear available; do not output narration."

        return (
            f"{INTERNAL_RETRY_MARKER}\n"
            "Internal tick: you MUST use the tool-calling interface to advance the game.\n"
            + last_tool_err_line
            + (f"Allowed tools now: {allowed_str}\n" if allowed_str else "")
            + suggestion
        )

    # /continue (tick) should advance the world via tools, not free-form text.
    # If the model returns no effective tool progress on a tick, retry with a context-aware instruction.
    if user_msg is None:
        before_tools = _count_tool_messages(base_invoke_messages)

        def _has_new_tool_calls(state: Dict[str, Any]) -> bool:
            return _count_tool_messages(list(state.get("messages") or [])) > before_tools

        def _last_tool_is_error(state: Dict[str, Any]) -> bool:
            name, content = _last_tool_message(list(state.get("messages") or []))
            return _tool_error_is_blocking(name, content)

        # Retry until we get a successful tool call, but avoid long tight loops
        # when the model keeps returning no tool calls on ticks.
        raw_retry = (os.getenv("LLM_WORLD_TICK_MAX_RETRIES") or "").strip()
        try:
            max_retries = int(raw_retry) if raw_retry else 24
        except Exception:
            max_retries = 24

        raw_no_tool_retry = (os.getenv("LLM_WORLD_TICK_MAX_NO_TOOL_RETRIES") or "").strip()
        try:
            max_no_tool_retries = int(raw_no_tool_retry) if raw_no_tool_retry else 4
        except Exception:
            max_no_tool_retries = 4
        max_no_tool_retries = max(1, min(int(max_no_tool_retries), int(max_retries)))

        consecutive_no_tool_calls = 0

        for attempt in range(max_retries):
            # If context changed (e.g., all characters just ended), stop retrying here
            # and let the auto-advance loop call invoke_once again with fresh tool bindings.
            if is_context_changed():
                if logs_enabled():
                    print("[trace] tool context changed mid-invocation; ending invoke_once for fresh tools")
                break

            # Check for actual progress: new tool calls that aren't errors
            has_progress = _has_new_tool_calls(out_state) and not _last_tool_is_error(out_state)
            
            # Even with tool calls, if there's invalid text output, we should retry
            # to prevent the model from rambling instead of calling the right tools.
            all_new_ai = _get_all_new_ai_texts(out_state, base_msg_count)
            has_invalid_text = any(app._is_invalid_gm_text_output(t) for t in all_new_ai)
            
            if has_progress and not has_invalid_text:
                break

            no_tool_calls = not _has_new_tool_calls(out_state)
            if no_tool_calls:
                consecutive_no_tool_calls += 1
            else:
                consecutive_no_tool_calls = 0

            if logs_enabled():
                if no_tool_calls:
                    print("[trace] tick produced no tool calls; retrying with tool-call-required instruction")
                elif has_invalid_text:
                    print("[trace] tick produced invalid text alongside tool calls; retrying with tool-call-only instruction")
                else:
                    print(f"[trace] tick produced a tool error (attempt {attempt+1}/{max_retries}); retrying with context-aware tool guidance")

            if no_tool_calls and consecutive_no_tool_calls >= max_no_tool_retries:
                if logs_enabled():
                    print(
                        "[trace] tick still produced no tool calls after "
                        f"{consecutive_no_tool_calls} retries; ending invoke_once to avoid loop"
                    )
                break

            retry_content = _tick_retry_instruction()
            injected_retry_contents.add(retry_content)
            out_state = _try_invoke(_retry_base_from_state(out_state) + [HumanMessage(content=retry_content)])

        if not _has_new_tool_calls(out_state) or _last_tool_is_error(out_state):
            # Do not abort /continue here.
            # A tick may legitimately end in a tool error (e.g., transient schema issues).
            # The auto-advance loop will keep invoking until a turn truly finalizes
            # (time advanced + scene cleared + story progressed) or until safety caps trigger.
            if logs_enabled():
                name, content = _last_tool_message(list(out_state.get("messages") or []))
                print(
                    "[trace] tick made no usable tool progress; will continue auto-advance "
                    f"(last tool: {name or '[none]'} | {str(content or '')})"
                )

    # No special nudge needed for turn finalization - it happens automatically
    # via _finalize_turn_if_ready() in invoke_once and auto-advance.
    if app._scene_all_characters_ended():
        if logs_enabled():
            print("[trace] scene ready to finalize; will be handled automatically")

    # Forward SA read-tool results into GM history so the GM stays aware of
    # world data the SA looked up without needing a separate explicit injection.
    # Only inject entries not already present in the active GM context window.
    try:
        _SA_READ_TOOLS = {"get_location", "get_npc", "get_character_detail"}
        new_msgs_for_read = list(out_state.get("messages") or [])
        base_len_for_read = len(base_invoke_messages)
        for _rm in new_msgs_for_read[base_len_for_read:]:
            if getattr(_rm, "type", "") != "tool":
                continue
            _tname = str(getattr(_rm, "name", "") or "").strip()
            if _tname not in _SA_READ_TOOLS:
                continue
            _tcontent = str(getattr(_rm, "content", "") or "").strip()
            if not _tcontent or _tcontent.lower().startswith("error"):
                continue
            try:
                _parsed = json.loads(_tcontent)
            except Exception:
                continue

            # Bulk reads: inject each named entry individually.
            # Single-entry reads: derive name from result JSON if available.
            if _tname in {"get_location", "get_npc"} and isinstance(_parsed, dict):
                _entry_name = str(_parsed.get("name") or "").strip()
                if _entry_name:
                    _kind = "location" if _tname == "get_location" else "npc"
                    _marker = f"[sa_read:{_kind}:{_entry_name}]"
                    if not app._gm_history_contains(_marker):
                        app._game_master.inject_delta(f"{_marker}\n{_tcontent}")
            elif _tname == "get_character_detail" and isinstance(_parsed, dict):
                # Tool args are not on the ToolMessage; find the preceding AIMessage
                # tool_call to recover the character name.
                _char_name = str(_parsed.get("name") or "").strip()
                if not _char_name:
                    # Scan backwards for the AI message that triggered this tool call
                    _tool_call_id = str(getattr(_rm, "tool_call_id", "") or "")
                    for _prev in reversed(new_msgs_for_read[:new_msgs_for_read.index(_rm)]):
                        if getattr(_prev, "type", "") not in {"ai", "assistant"}:
                            continue
                        for _tc in (getattr(_prev, "tool_calls", None) or []):
                            if isinstance(_tc, dict) and _tc.get("id") == _tool_call_id:
                                _char_name = str((_tc.get("args") or {}).get("name") or "").strip()
                                break
                        if _char_name:
                            break
                if _char_name:
                    _marker = f"[sa_read:player:{_char_name}]"
                    _old_marker = f"[sa_read:character:{_char_name}]"
                    if not app._gm_history_contains(_marker) and not app._gm_history_contains(_old_marker):
                        app._game_master.inject_delta(f"{_marker}\n{_tcontent}")
    except Exception:
        pass

    # Persist messages but drop our injected system context.
    new_messages = []
    for m in (out_state.get("messages") or []):
        if getattr(m, "type", "") == "system":
            continue
        if getattr(m, "type", "") in {"human", "user"}:
            content = str(getattr(m, "content", "") or "")
            if content.startswith(INTERNAL_RETRY_MARKER) or content in injected_retry_contents:
                continue
        new_messages.append(m)
    app.state = {"messages": new_messages}
    app.save_gm_history()
