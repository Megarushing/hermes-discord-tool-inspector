"""Extract ONLY explicitly provider-designated summary_text, never raw thinking."""
import json


def provider_summary(message):
    items = (message.get('codex_reasoning_items') if isinstance(message, dict)
             else getattr(message, 'codex_reasoning_items', None))
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            return ''
    if not isinstance(items, list):
        return ''
    texts = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'reasoning':
            continue
        summaries = item.get('summary')
        if not isinstance(summaries, list):
            continue
        for part in summaries:
            if isinstance(part, dict) and part.get('type') == 'summary_text':
                text = part.get('text')
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    return '\n\n'.join(texts)
