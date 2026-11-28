"""Consume the public Responses stream used by ChatGPT plan access."""

import json

from .openai_siwc import RESOURCE, SiwcError, responses_error


_STREAM_LIMIT = 16 * 1024 * 1024


async def _events(response):
    # Buffer bytes until newline so UTF-8 characters can straddle HTTP chunks.
    partial = b""
    data = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _STREAM_LIMIT:
            raise SiwcError("SIWC_STREAM_LIMIT", 502, "ChatGPT 输出超过本机读取上限。")
        lines = (partial + chunk).split(b"\n")
        partial = lines.pop()
        for line in lines:
            line = line.removesuffix(b"\r")
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif not line and data:
                payload = b"\n".join(data)
                data.clear()
                if payload == b"[DONE]":
                    return
                try:
                    item = json.loads(payload)
                except (ValueError, UnicodeError, RecursionError):
                    raise SiwcError("SIWC_INVALID_STREAM", 502, "ChatGPT 返回的流式数据无效。") from None
                if not isinstance(item, dict):
                    raise SiwcError("SIWC_INVALID_STREAM", 502, "ChatGPT 返回的流式事件无效。")
                yield item


def _visible_text(response):
    parts = {}
    output = response.get("output")
    for output_index, item in enumerate(output if isinstance(output, list) else []):
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") not in (None, "assistant"):
            continue
        content = item.get("content")
        for content_index, part in enumerate(content if isinstance(content, list) else []):
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts[output_index, content_index] = part["text"]
    return parts


def _parse_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, UnicodeError, RecursionError):
            pass
    return {}


def _build_responses_input(messages, prompt):
    if not messages:
        return [{"role": "user", "content": prompt}]
    items = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            items.append({"role": "user", "content": str(msg.get("content") or "")})
        elif role == "assistant":
            state = msg.get("providerState")
            if (isinstance(state, dict) and state.get("transport") == "openai-responses-siwc"
                    and isinstance(state.get("output"), list) and state["output"]):
                items.extend(item for item in state["output"] if isinstance(item, dict))
                continue
            content = msg.get("content")
            if isinstance(content, str) and content:
                items.append({"role": "assistant", "content": content})
            for call in msg.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                name = call.get("name")
                if not call_id or not name:
                    continue
                raw_args = call.get("rawArguments")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
                items.append({
                    "type": "function_call",
                    "call_id": str(call_id),
                    "name": str(name),
                    "arguments": raw_args,
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("toolCallId") or ""),
                "output": str(msg.get("content") or ""),
            })
    return items or [{"role": "user", "content": prompt}]


def _format_responses_tools(tools):
    formatted = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        formatted.append({
            "type": "function",
            "name": str(tool["name"]),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            "strict": bool(tool.get("strict", False)),
        })
    return formatted


def _extract_tool_calls(output_items):
    calls = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
            continue
        raw_args = item.get("arguments")
        raw_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, ensure_ascii=False)
        calls.append({
            "id": call_id,
            "name": name,
            "arguments": _parse_arguments(raw_args),
            "rawArguments": raw_str,
        })
    return calls


async def _stream_responses(service, epoch, body):
    chunks, done, done_items = {}, {}, {}
    refused = False
    async with service.authorized("POST", RESOURCE + "/responses", epoch=epoch, body=body) as stream:
        media = stream.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media and media != "text/event-stream":
            raise SiwcError("SIWC_INVALID_STREAM", 502, "ChatGPT 没有返回预期的事件流。")
        async for event in _events(stream):
            event_type = event.get("type")
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            if event_type in ("response.output_text.delta", "response.output_text.done"):
                key = (event.get("output_index", 0), event.get("content_index", 0))
                if not all(type(index) is int and index >= 0 for index in key):
                    continue
                if event_type.endswith(".delta") and isinstance(event.get("delta"), str):
                    chunks.setdefault(key, []).append(event["delta"])
                elif isinstance(event.get("text"), str):
                    done[key] = event["text"]
            elif event_type == "response.output_item.done":
                item = event.get("item")
                output_index = event.get("output_index", len(done_items))
                if isinstance(item, dict) and type(output_index) is int and output_index >= 0:
                    done_items[output_index] = item
            elif event_type in ("response.refusal.delta", "response.refusal.done"):
                refused = True
            elif event_type in ("response.failed", "error"):
                error = responses_error(
                    stream.status_code, response if event_type == "response.failed" else event, headers=stream.headers,
                )
                service.record_error(error, epoch)
                error.retryable, error.no_replay = False, True
                raise error
            elif event_type == "response.incomplete":
                details = response.get("incomplete_details")
                reason = details.get("reason") if isinstance(details, dict) else None
                if reason == "max_output_tokens":
                    return {"text": "", "finishReason": reason, "usage": {}, "toolCalls": [], "rawOutput": []}
                raise SiwcError("SIWC_INCOMPLETE", 502, "ChatGPT 未完成该回复，已保留原内容。")
            elif event_type == "response.completed":
                service.require_epoch(epoch)
                if not response or response.get("status", "completed") != "completed" or response.get("error"):
                    raise SiwcError("SIWC_INVALID_STREAM", 502, "ChatGPT 未返回有效的完成事件。")
                parts = {key: "".join(value) for key, value in chunks.items()}
                parts.update(done)
                parts.update(_visible_text(response))
                text = "\n".join(parts[key] for key in sorted(parts))
                raw_output = response.get("output")
                if not isinstance(raw_output, list) or not raw_output:
                    raw_output = [done_items[idx] for idx in sorted(done_items)]
                else:
                    raw_output = [item for item in raw_output if isinstance(item, dict)]
                tool_calls = _extract_tool_calls(raw_output)
                if refused or (not text.strip() and not tool_calls):
                    raise SiwcError("SIWC_EMPTY_OUTPUT", 502, "ChatGPT 未返回可见回复。")
                service.quota_limited = False
                return {
                    "text": text,
                    "finishReason": "tool_calls" if tool_calls else "stop",
                    "usage": response.get("usage"),
                    "toolCalls": tool_calls,
                    "rawOutput": raw_output,
                }
    service.require_epoch(epoch)
    raise SiwcError("SIWC_STREAM_INTERRUPTED", 502, "ChatGPT 事件流提前结束，已保留原内容。")


async def call_responses(service, model, system, prompt, epoch, *,
                         messages=None, tools=None, reasoning_effort=None, cache_key=None):
    body = {"model": model, "input": _build_responses_input(messages, prompt), "store": False, "stream": True}
    if system:
        body["instructions"] = system
    formatted_tools = _format_responses_tools(tools)
    if formatted_tools:
        body["tools"] = formatted_tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    optional_keys = []
    if reasoning_effort in ("low", "medium", "high"):
        body["reasoning"] = {"effort": reasoning_effort}
        body["include"] = ["reasoning.encrypted_content"]
        optional_keys.extend(("reasoning", "include"))
    if isinstance(cache_key, str) and cache_key.strip():
        body["prompt_cache_key"] = cache_key.strip()[:64]
        optional_keys.append("prompt_cache_key")
    try:
        return await _stream_responses(service, epoch, body)
    except SiwcError as error:
        if (optional_keys and not error.no_replay and error.status == 400
                and error.code in ("SIWC_CAPABILITY_UNSUPPORTED", "SIWC_REQUEST_FAILED")
                and "（tools）" not in str(error)):
            fallback_body = {k: v for k, v in body.items() if k not in optional_keys}
            return await _stream_responses(service, epoch, fallback_body)
        raise
