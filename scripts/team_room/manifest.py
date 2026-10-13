"""Minimal YAML subset for agent and team manifests (stdlib only).

Supported: nested mappings by indentation, "- item" lists of scalars or
mappings, inline [a, b] lists, quoted or plain scalars, null/true/false/ints,
and # comments. Anything else is rejected rather than guessed.
"""
import json
import re

KEY = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$')
SPECIAL = re.compile(r'[:#\[\]{},&*!|>\'"%@\x60]')


class ManifestError(ValueError):
    pass


def _strip_comment(text):
    out, quote = [], None
    for i, ch in enumerate(text):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            out.append(ch)
        elif ch == '#' and (i == 0 or text[i - 1] in ' \t'):
            break
        else:
            out.append(ch)
    return ''.join(out).rstrip()


def scalar(text):
    text = text.strip()
    if text in ('', '~', 'null'):
        return None
    if text == 'true':
        return True
    if text == 'false':
        return False
    if re.fullmatch(r'-?\d+', text):
        return int(text)
    if text[0] == '"' and text[-1] == '"' and len(text) > 1:
        return json.loads(text)
    if text[0] == "'" and text[-1] == "'" and len(text) > 1:
        return text[1:-1].replace("''", "'")
    if text[0] == '[' and text[-1] == ']':
        inner = text[1:-1].strip()
        return [scalar(part) for part in inner.split(',')] if inner else []
    if text[0] in '{&*!|>':
        raise ManifestError(f'Unsupported YAML value: {text}')
    return text


def loads(text):
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if '\t' in raw[:len(raw) - len(raw.lstrip())]:
            raise ManifestError(f'Line {number}: tabs are not allowed for indentation')
        content = _strip_comment(raw)
        if content.strip():
            lines.append((number, len(content) - len(content.lstrip()), content.strip()))
    value, index = _block(lines, 0, 0)
    if index != len(lines):
        raise ManifestError(f'Line {lines[index][0]}: unexpected indentation')
    return value if value is not None else {}


def _block(lines, index, indent):
    if index >= len(lines) or lines[index][1] < indent:
        return None, index
    if lines[index][2].startswith('- ') or lines[index][2] == '-':
        return _list(lines, index, lines[index][1])
    return _map(lines, index, lines[index][1])


def _map(lines, index, indent, result=None):
    result = {} if result is None else result
    while index < len(lines) and lines[index][1] == indent:
        number, _, text = lines[index]
        match = KEY.match(text)
        if not match:
            raise ManifestError(f'Line {number}: expected "key: value"')
        key, rest = match.group(1), match.group(2)
        if key in result:
            raise ManifestError(f'Line {number}: duplicate key {key}')
        index += 1
        if rest is None or rest == '':
            child, index = _block(lines, index, indent + 1)
            result[key] = child
        else:
            result[key] = scalar(rest)
    if index < len(lines) and lines[index][1] > indent:
        raise ManifestError(f'Line {lines[index][0]}: unexpected indentation')
    return result, index


def _list(lines, index, indent):
    result = []
    while index < len(lines) and lines[index][1] == indent and (lines[index][2].startswith('- ') or lines[index][2] == '-'):
        number, _, text = lines[index]
        item = text[1:].strip()
        index += 1
        if not item:
            child, index = _block(lines, index, indent + 1)
            result.append(child)
        elif KEY.match(item):
            key, rest = KEY.match(item).groups()
            entry = {key: scalar(rest) if rest else None}
            if not rest:
                entry[key], index = _block(lines, index, indent + 3)
            if index < len(lines) and lines[index][1] == indent + 2:
                entry, index = _map(lines, index, indent + 2, entry)
            result.append(entry)
        else:
            result.append(scalar(item))
    return result, index


def _emit_scalar(value):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if (text == '' or text != text.strip() or SPECIAL.search(text)
            or text in ('null', 'true', 'false', '~') or re.fullmatch(r'-?\d+', text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def dumps(value, indent=0):
    pad = ' ' * indent
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f'{pad}{key}:')
                lines.append(dumps(item, indent + 2))
            elif isinstance(item, list) and item and any(isinstance(x, dict) for x in item):
                lines.append(f'{pad}{key}:')
                lines.append(dumps(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f'{pad}{key}: [' + ', '.join(_emit_scalar(x) for x in item) + ']')
            elif isinstance(item, dict):
                lines.append(f'{pad}{key}: null')
            else:
                lines.append(f'{pad}{key}: {_emit_scalar(item)}')
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                first = True
                for key, sub in item.items():
                    prefix = f'{pad}- ' if first else f'{pad}  '
                    first = False
                    if isinstance(sub, (dict, list)) and sub:
                        lines.append(f'{prefix}{key}:')
                        lines.append(dumps(sub, indent + 4))
                    else:
                        lines.append(f'{prefix}{key}: {_emit_scalar(sub)}')
            else:
                lines.append(f'{pad}- {_emit_scalar(item)}')
    return '\n'.join(lines)


def load(path):
    from pathlib import Path
    return loads(Path(path).read_text(encoding='utf-8'))


def dump(path, value, header=None):
    from pathlib import Path
    text = (header.rstrip() + '\n' if header else '') + dumps(value) + '\n'
    Path(path).write_text(text, encoding='utf-8', newline='\n')
