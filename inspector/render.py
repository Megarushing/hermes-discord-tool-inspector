"""Per-call display with separate labels and lossless code-block pagination."""
import json
import re

PAGE_SIZE = 1700


def _json(value):
    return json.dumps(value, ensure_ascii=True, indent=2).replace('`', '\\u0060').replace('@', '\\u0040')


def _display(value):
    """Readable JSON-like structure, not a JSON export; decode only our own tokens."""
    if isinstance(value, str):
        return value
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    return re.sub(r'"(?:\\.|[^"\\])*"',
                  lambda match: '"' + json.loads(match.group()) + '"', encoded)


def _units(text):
    return len(text.encode('utf-16-le')) // 2


def _part(text, offset, budget):
    end = offset
    used = 0
    while end < len(text):
        width = 2 if ord(text[end]) > 0xFFFF else 1
        if used + width > budget and end > offset:
            break
        used += width
        end += 1
        if used >= budget:
            break
    part = text[offset:end]
    fence = part.find('```')
    # Split actual fence delimiters across independently fenced pages. No inserted
    # escapes/zero-width characters that would alter copied command contents.
    return part[:fence + 2] if fence >= 0 else part


def sections(record):
    result = record['result']
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            pass
    label = 'Output'
    if isinstance(result, dict) and isinstance(result.get('exit_code'), (int, type(None))):
        if 'exit_code' in result:
            label += f" (exit code: {result['exit_code']})"
    return [('Input', _display(record['args'])), (label, _display(result))]


def document(record):
    return '\n\n'.join(pages(record))


def pages(record, page_size=PAGE_SIZE):
    if not isinstance(page_size, int) or not 1 <= page_size <= PAGE_SIZE:
        raise ValueError('page_size must be between 1 and 1700')
    if record.get('kind') == 'reasoning_summary':
        from discord.utils import escape_markdown, escape_mentions
        text = escape_markdown(escape_mentions(str(record['result'])))
        # Half the character budget is safe even for astral Unicode (2 UTF-16 units).
        size = max(1, page_size // 2)
        return [text[start:start + size] for start in range(0, len(text), size)] or ['']
    # Budget includes both fence pairs and labels; payload chunks remain lossless.
    chunks = []
    current = ''
    for label, text in sections(record):
        offset = 0
        while True:
            heading = label + (' (continued)' if offset else '')
            overhead = len(heading) + 9
            remaining = page_size - _units(current) - (2 if current else 0) - overhead
            if remaining <= 0 and current:
                chunks.append(current)
                current = ''
                continue
            # Tiny explicit page_size values still make progress; normal runtime
            # uses 1700 and stays under Discord's content budget.
            part = _part(text, offset, max(1, remaining))
            if current and _units(part) > remaining:
                chunks.append(current)
                current = ''
                continue
            block = heading + '\n```\n' + part + '\n```'
            current += ('\n\n' if current else '') + block
            offset += len(part)
            if offset < len(text):
                chunks.append(current)
                current = ''
            else:
                break
    if current:
        chunks.append(current)
    return chunks


def summary(record):
    if record.get('kind') == 'reasoning_summary':
        from discord.utils import escape_markdown, escape_mentions
        preview = ' '.join(str(record['result']).split())
        preview = preview[:67] + '...' if len(preview) > 70 else preview
        return '**Reasoning** · ' + escape_markdown(escape_mentions(preview))
    name = ''.join(c for c in str(record['tool']) if c.isalnum() or c in '_-.')[:80] or 'tool'
    args = record.get('args')
    preview = ''
    if isinstance(args, dict):
        for key in ('command', 'path', 'pattern', 'query', 'name', 'action', 'url'):
            if isinstance(args.get(key), str) and args[key].strip():
                preview = _json(' '.join(args[key].split()))[1:-1]
                break
    if len(preview) > 70:
        preview = preview[:67] + '...'
    return f'**{name}**' + (f' · `{preview}`' if preview else '')


def render(record, page=None):
    heading = summary(record)
    if page is None:
        return heading
    chunks = pages(record)
    if not isinstance(page, int) or not 0 <= page < len(chunks):
        raise ValueError('page out of range')
    counter = f'\nPage {page + 1}/{len(chunks)}' if len(chunks) > 1 else ''
    return f'{heading}{counter}\n{chunks[page]}'
