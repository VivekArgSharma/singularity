def keep_only_latest_image(messages):
    """
    Replaces every older image in the conversation messages with a text stub.
    Guarantees at most ONE image block exists in messages at any time.
    """
    seen = False
    for msg in reversed(messages):
        if not isinstance(msg.get('content'), list):
            continue

        lists = [msg['content']]
        for b in msg['content']:
            if isinstance(b, dict) and b.get('type') == 'tool_result' and isinstance(b.get('content'), list):
                lists.append(b['content'])

        for items in lists:
            for i in range(len(items) - 1, -1, -1):
                b = items[i]
                if isinstance(b, dict) and b.get('type') == 'image':
                    if seen:
                        items[i] = {'type': 'text', 'text': '[earlier frame omitted]'}
                    seen = True

def trim_history_if_long(messages, max_steps=20):
    """
    Optional cost trim: once message history grows past max_steps,
    compacts older steps into concise summary text while preserving
    the initial instruction and the final few interactions.
    """
    if len(messages) <= max_steps * 2:
        return

    # Keep initial command (messages[0]) and the latest 8 messages
    preserved_head = [messages[0]]
    preserved_tail = messages[-8:]
    summary_text = f"[{len(messages) - 9} previous intermediate search steps omitted for brevity]"

    trimmed = preserved_head + [{'role': 'user', 'content': [{'type': 'text', 'text': summary_text}]}] + preserved_tail
    messages.clear()
    messages.extend(trimmed)

