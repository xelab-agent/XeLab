"""Validate local Markdown links and the supported GitHub-style anchors offline."""
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote, urlparse


FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
QUOTE_MARKER = re.compile(r' {0,3}>[ \t]?')
LIST_MARKER = re.compile(r'^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+')
LIST_QUOTE = re.compile(r'^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+(?=>)')
REFERENCE_START = re.compile(r'^ {0,3}\[[^\]]+\]:')
INLINE_CODE = re.compile(r'(?<![\\`]) (?:\\\\)* (`+) (?!`) (.*?) (?<!`) \1 (?!`)', re.X)
HTML_TAG = re.compile(r'''</?[A-Za-z][A-Za-z0-9-]*(?:\s+(?:[^>"']|"[^"]*"|'[^']*')*)?\s*/?>''')
ATX = re.compile(r'^ {0,3}#{1,6}(?:[ \t]+(.*)|[ \t]*)$')
SETEXT = re.compile(r'^ {0,3}(?:=+|-+)[ \t]*$')
INERT_HTML = re.compile(r'<!--|<(pre|script|style|textarea)\b[^>]*>', re.I)
HTML_BLOCK_TAGS = (
    r'address|article|aside|base|basefont|blockquote|body|caption|center|'
    r'col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|'
    r'form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|'
    r'menuitem|nav|noframes|ol|optgroup|option|p|param|pre|script|search|section|style|'
    r'summary|table|tbody|td|textarea|tfoot|th|thead|title|tr|track|ul'
)
HTML_BLOCK_START = re.compile(
    r'^ {0,3}<(?:/?(?:' + HTML_BLOCK_TAGS + r')(?=[ \t/>]|$)|!|\?)',
    re.I,
)
HTML_BLOCK_TAG = re.compile(
    r'^ {0,3}</?(?:' + HTML_BLOCK_TAGS + r')(?=[ \t/>]|$)', re.I
)


def _blockquote(line, limit=None):
    """Return the quote depth and content after at most `limit` quote markers."""
    marker = LIST_QUOTE.match(line) if limit is None else None
    pos = marker.end() if marker else 0
    depth = 0
    while limit is None or depth < limit:
        match = QUOTE_MARKER.match(line, pos)
        if match is None:
            break
        pos = match.end()
        depth += 1
    return depth, line[pos:]


def _fenced_container(line, quote_depth, outer_indent, inner_indent):
    """Return fence content, or None if its quote/list container ended."""
    expanded = line.expandtabs(4)
    if outer_indent:
        if expanded.startswith(' ' * outer_indent):
            expanded = expanded[outer_indent:]
        elif not expanded.strip() and not quote_depth:
            return ''
        else:
            return None
    depth, quoted = _blockquote(expanded, quote_depth)
    if depth < quote_depth:
        return None
    if inner_indent:
        if quoted.startswith(' ' * inner_indent):
            quoted = quoted[inner_indent:]
        elif not quoted.strip():
            return ''
        else:
            return None
    return quoted


def _can_continue_paragraph(line):
    """Recognize common paragraph/list lines that permit lazy quote continuation."""
    marker = re.match(r'^ {0,3}(?:[-+*]|\d+[.)])[ \t]+', line)
    if marker:
        line = line[marker.end():]
    if not line.strip() or ATX.match(line) or SETEXT.match(line) or FENCE.match(line):
        return False
    if re.match(r'^ {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$', line):
        return False
    return not (REFERENCE_START.match(line) or HTML_BLOCK_START.match(line))


def _strip_inert_html(line, state, preserve_pre=False):
    """Remove comment/literal HTML sections while respecting inline code spans."""
    spans = [(match.start(), match.end()) for match in INLINE_CODE.finditer(line)]
    result = []
    pos = 0
    while pos < len(line):
        if state:
            end = (re.search(r'-->', line[pos:]) if state == 'comment' else
                   re.search(r'</' + state + r'\s*>', line[pos:], re.I))
            if end is None:
                if preserve_pre and state == 'pre':
                    result.append(line[pos:])
                return ''.join(result), state
            if preserve_pre and state == 'pre':
                result.append(line[pos:pos + end.end()])
            pos += end.end()
            state = None
            continue
        match = INERT_HTML.search(line, pos)
        if match is None:
            result.append(line[pos:])
            break
        if any(start <= match.start() < end for start, end in spans):
            result.append(line[pos:match.end()])
            pos = match.end()
            continue
        result.append(line[pos:match.start()])
        state = 'comment' if match.group() == '<!--' else match.group(1).lower()
        if preserve_pre and state == 'pre':
            result.append(match.group())
        pos = match.end()
    return ''.join(result), state


def _mask_html_blocks(value):
    """Blank raw HTML blocks while leaving inline HTML and its Markdown visible."""
    lines = value.splitlines(keepends=True)
    result = []
    block = False
    block_depth = 0
    previous_blank = True
    for line in lines:
        raw = line.rstrip('\r\n')
        depth, quoted = _blockquote(raw)
        if block and depth < block_depth:
            block = False
        if block:
            result.append(re.sub(r'[^\r\n]', ' ', line))
            if not quoted.strip():
                block = False
                previous_blank = True
            continue
        if HTML_BLOCK_TAG.match(quoted):
            block = True
            block_depth = depth
            result.append(re.sub(r'[^\r\n]', ' ', line))
            previous_blank = False
            continue
        if previous_blank and HTML_TAG.fullmatch(quoted.strip()):
            block = True
            block_depth = depth
            result.append(re.sub(r'[^\r\n]', ' ', line))
            previous_blank = False
            continue
        result.append(line)
        previous_blank = not quoted.strip()
    return ''.join(result)


def without_code(value, preserve_pre=False):
    """Blank code blocks and HTML comments without joining surrounding lines."""
    value = _mask_multiline_code(value, preserve_structure=True)
    lines = []
    fence = None
    fence_depth = 0
    fence_outer_indent = 0
    fence_inner_indent = 0
    indented_code = None
    indented_depth = 0
    list_indent = None
    previous_blank = True
    previous_depth = 0
    previous_paragraph = False
    html_state = None
    html_depth = 0
    for line in value.splitlines():
        if fence is not None:
            quoted = _fenced_container(line, fence_depth, fence_outer_indent, fence_inner_indent)
            if quoted is None:
                fence = None
                previous_blank = True
            else:
                match = FENCE.match(quoted)
                if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                    if not quoted[match.end():].strip():
                        fence = None
                        previous_blank = True
                lines.append('')
                continue
        if indented_code is not None:
            depth, quoted = _blockquote(line, indented_depth)
            expanded = quoted.expandtabs(4)
            indent = len(expanded) - len(expanded.lstrip(' '))
            if depth == indented_depth and (not quoted.strip() or indent >= indented_code):
                lines.append('')
                continue
            indented_code = None
        depth, quoted = _blockquote(line)
        if html_state is not None and depth < html_depth:
            if preserve_pre and html_state == 'pre':
                lines.append('</pre>')
            html_state = None
            previous_blank = True
        if depth != previous_depth:
            list_indent = None
            lazy = depth < previous_depth and not previous_blank and previous_paragraph
            previous_blank = not lazy
            previous_depth = depth
        expanded = quoted.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(' '))
        if not html_state and list_indent is not None and quoted.strip() and indent < list_indent:
            list_indent = None
        threshold = (list_indent or 0) + 4
        if not html_state and quoted.strip() and previous_blank and indent >= threshold:
            indented_code = threshold
            indented_depth = depth
            lines.append('')
            continue
        old_html_state = html_state
        was_pre = html_state == 'pre'
        line, html_state = _strip_inert_html(line, html_state, preserve_pre=preserve_pre)
        if html_state is not None and html_state != old_html_state:
            html_depth = depth
        if preserve_pre and (was_pre or html_state == 'pre'):
            lines.append(line)
            if html_state is None:
                previous_blank = True
            continue
        depth, quoted = _blockquote(line)
        if not quoted.strip():
            lines.append('')
            previous_blank = True
            previous_paragraph = False
            continue
        expanded = quoted.expandtabs(4)
        outer_marker = LIST_QUOTE.match(line.expandtabs(4))
        list_marker = LIST_MARKER.match(expanded)
        inner_indent = 0
        if list_marker:
            inner_indent = list_marker.end()
        elif list_indent is not None and expanded.startswith(' ' * list_indent):
            inner_indent = list_indent
        match = FENCE.match(expanded[inner_indent:]) if inner_indent else None
        if match is None:
            inner_indent = 0
            match = FENCE.match(expanded)
        if match:
            fence = match.group(1)
            fence_depth = depth
            fence_outer_indent = outer_marker.end() if outer_marker else 0
            fence_inner_indent = inner_indent
            lines.append('')
        else:
            if list_marker:
                list_indent = list_marker.end()
            lines.append(line)
            previous_paragraph = _can_continue_paragraph(quoted)
        previous_blank = False
    return '\n'.join(lines)


def _visible_heading(value, references=None):
    """Extract text from the inline forms supported in a heading."""
    code = []

    def save(value):
        code.append(value)
        return f'\x00{len(code)-1}\x00'

    def code_text(match):
        text = match.group(2)
        if text.startswith(' ') and text.endswith(' ') and text.strip(' '):
            text = text[1:-1]
        return save(text)

    value = INLINE_CODE.sub(code_text, value)
    value = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])',
                   lambda match: save(match.group(1)), value)
    if references:
        def reference(match):
            label = match.group(2)
            target = (match.group(3) or label) if match.lastindex == 3 else label
            if _reference_label(target) not in references:
                return match.group()
            return '' if match.group(1) else label

        value = re.sub(r'(!?)\[([^\]]+)\]\[([^\]]*)\]', reference, value)
        value = re.sub(r'(!?)\[([^\]]+)\](?![\[(])', reference, value)
    value = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', value)
    value = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'<((?:https?://|mailto:)[^>]+)>', r'\1', value)
    value = re.sub(r'<[^>]*>', '', value)
    # Underscores inside words are literal in GitHub-flavored Markdown.
    value = re.sub(r'(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)', r'\1', value)
    value = re.sub(r'(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)', r'\1', value)
    value = unescape(value)
    return re.sub(r'\x00(\d+)\x00',
                  lambda m: code[int(m.group(1))] if int(m.group(1)) < len(code) else m.group(), value)


def _slug(value, references=None):
    value = _visible_heading(value.strip(), references).lower()
    # github-slugger 2.0.0 also retains these letter-like Unicode symbols.
    letter_symbols = ((0x24B6, 0x24E9), (0x1F130, 0x1F149),
                      (0x1F150, 0x1F169), (0x1F170, 0x1F189))
    return ''.join('-' if c == ' ' else c for c in value
                   if c in ' -' or unicodedata.category(c)[0] in 'LM'
                   or unicodedata.category(c) in {'Nd', 'Nl', 'Pc'}
                   or any(start <= ord(c) <= end for start, end in letter_symbols))


class _ExplicitAnchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = set()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        table_children = {'tbody', 'td', 'tfoot', 'th', 'thead', 'tr'}
        if tag in {'a', 'b', 'blockquote', 'br', 'code', 'dd', 'del',
                   'details', 'div', 'dl', 'dt', 'em',
                   'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'ins',
                   'kbd', 'li', 'mark', 'ol', 'p', 'pre', 'q', 'rp', 'rt', 'ruby',
                   's', 'samp', 'section', 'span', 'strike', 'strong', 'sub',
                   'summary', 'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead',
                   'tr', 'tt', 'ul', 'var'} and (tag not in table_children or 'table' in self.stack):
            self.anchors.update(value for name, value in attrs
                                if value and (name == 'id' or (tag == 'a' and name == 'name')))
        if tag not in {'br', 'hr', 'img'}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


def markdown_anchors(content):
    """Collect supported heading and explicit anchors; custom anchors do not deduplicate headings."""
    text = _mask_multiline_code(without_code(content))
    text = _mask_html_blocks(text)
    parser = _ExplicitAnchors()
    html = without_code(content, preserve_pre=True)
    parts = re.split(r'(<pre\b[^>]*>.*?</pre\s*>)', html, flags=re.I | re.S)
    parser.feed(''.join(part if i % 2 else INLINE_CODE.sub('', _mask_multiline_code(part))
                        for i, part in enumerate(parts)))
    anchors = {'top'} | parser.anchors
    references, _ = _reference_definitions(INLINE_CODE.sub('', text))
    used = set()
    lines = [_blockquote(line) for line in text.splitlines()]
    for index, (depth, line) in enumerate(lines):
        match = ATX.match(line)
        if match:
            heading = re.sub(r'[ \t]+#+[ \t]*$', '', match.group(1) or '')
        elif (SETEXT.match(line) and index and lines[index - 1][0] == depth
              and lines[index - 1][1].strip() and not ATX.match(lines[index - 1][1])
              and not SETEXT.match(lines[index - 1][1])
              and not re.match(r'^ {0,3}(?:>|[-+*] |\d+[.)] |<)', lines[index - 1][1])):
            heading = lines[index - 1][1].strip()
        else:
            continue
        base = _slug(heading, references)
        candidate = base
        counter = 0
        while candidate in used:
            counter += 1
            candidate = f'{base}-{counter}'
        used.add(candidate)
        if candidate:
            anchors.add(candidate)
    return anchors


def _is_escaped(text, pos):
    start = pos
    while start and text[start - 1] == '\\':
        start -= 1
    return (pos - start) % 2 == 1


def _mask_multiline_code(text, preserve_structure=False):
    """Mask multiline code spans within a paragraph without crossing block boundaries."""
    spans = []
    start = None
    end = 0
    offset = 0
    previous_depth = None
    previous_paragraph = False
    literal_tag = None
    literal_depth = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        depth, quoted = _blockquote(raw)
        if literal_tag is not None and depth < literal_depth:
            literal_tag = None
        if literal_tag is not None:
            if re.search(r'</' + literal_tag + r'\s*>', quoted, re.I):
                literal_tag = None
            if start is not None:
                spans.append((start, end))
                start = None
            previous_depth = None
            previous_paragraph = False
            offset += len(line)
            continue
        literal = re.match(r'^ {0,3}<(pre|script|style|textarea)\b', quoted, re.I)
        if literal:
            tag = literal.group(1).lower()
            if not re.search(r'</' + tag + r'\s*>', quoted, re.I):
                literal_tag = tag
                literal_depth = depth
            if start is not None:
                spans.append((start, end))
                start = None
            previous_depth = None
            previous_paragraph = False
            offset += len(line)
            continue
        list_start = LIST_MARKER.match(quoted.expandtabs(4)) or LIST_QUOTE.match(raw.expandtabs(4))
        paragraph = _can_continue_paragraph(quoted)
        lazy = (previous_depth is not None and depth < previous_depth and previous_paragraph)
        if (REFERENCE_START.match(quoted) and start is not None and not list_start
                and (depth == previous_depth or lazy)):
            paragraph = True
        if (not paragraph or list_start or
                (previous_depth is not None and depth != previous_depth and not lazy)):
            if start is not None:
                spans.append((start, end))
                start = None
        if paragraph:
            if start is None:
                start = offset
            end = offset + len(line)
        previous_depth = depth if paragraph else None
        previous_paragraph = paragraph
        offset += len(line)
    if start is not None:
        spans.append((start, end))

    masked = list(text)
    for begin, end in spans:
        segment = text[begin:end]
        runs = list(re.finditer(r'`+', segment))
        following = [None] * len(runs)
        latest = {}
        for index in range(len(runs) - 1, -1, -1):
            width = runs[index].end() - runs[index].start()
            following[index] = latest.get(width)
            latest[width] = index
        index = 0
        while index < len(runs):
            closing = following[index]
            if closing is None or _is_escaped(segment, runs[index].start()):
                index += 1
                continue
            first = runs[index].start()
            last = runs[closing].end()
            if '\n' in segment[first:last]:
                first += begin
                last += begin
                if not preserve_structure:
                    for pos in range(first, last):
                        if masked[pos] != '\n':
                            masked[pos] = ' '
                else:
                    cursor = first
                    while cursor < last:
                        line_start = text.rfind('\n', 0, cursor) + 1
                        newline = text.find('\n', cursor, last)
                        line_end = last if newline < 0 else newline
                        raw = text[line_start:line_end]
                        _, quoted = _blockquote(raw)
                        indent = len(raw) - len(raw.lstrip(' \t'))
                        prefix = max(indent, len(raw) - len(quoted))
                        mask_start = max(cursor, line_start + prefix)
                        for pos in range(mask_start, line_end):
                            masked[pos] = ' '
                        if mask_start < line_end:
                            masked[mask_start] = 'x'
                        cursor = line_end if newline < 0 else newline + 1
            index = closing + 1
    return ''.join(masked)


def _unescape_markdown(value):
    value = re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])', r'\1', value)
    return unescape(value)


def _bracket_end(text, start):
    depth = 1
    for pos in range(start + 1, len(text)):
        if text[pos] in '[]' and not _is_escaped(text, pos):
            depth += 1 if text[pos] == '[' else -1
            if depth == 0:
                return pos
        if text[pos] == '\n':
            return None
    return None


def _destination(text, start):
    """Parse one inline destination and optional title starting at '('."""
    pos = start + 1
    while pos < len(text) and text[pos] in ' \t':
        pos += 1
    if pos >= len(text):
        return None
    if text[pos] == '<':
        begin = pos + 1
        pos = begin
        while pos < len(text) and text[pos] != '\n':
            if text[pos] == '>' and not _is_escaped(text, pos):
                break
            if text[pos] == '<' and not _is_escaped(text, pos):
                return None
            pos += 1
        if pos >= len(text) or text[pos] != '>':
            return None
        raw = text[begin:pos]
        pos += 1
    else:
        begin = pos
        depth = 0
        while pos < len(text):
            char = text[pos]
            if char in ' \t\n':
                break
            if char == '(' and not _is_escaped(text, pos):
                depth += 1
                if depth > 32:
                    return None
            elif char == ')' and not _is_escaped(text, pos):
                if depth == 0:
                    break
                depth -= 1
            elif char in '<>' and not _is_escaped(text, pos):
                return None
            pos += 1
        if depth:
            return None
        raw = text[begin:pos]
    before_space = pos
    while pos < len(text) and text[pos] in ' \t':
        pos += 1
    if pos < len(text) and text[pos] == ')':
        return raw, pos + 1
    if pos == before_space or pos >= len(text) or text[pos] not in "\"'(":
        return None
    close = ')' if text[pos] == '(' else text[pos]
    pos += 1
    while pos < len(text) and text[pos] != '\n':
        if text[pos] == close and not _is_escaped(text, pos):
            pos += 1
            break
        pos += 1
    else:
        return None
    while pos < len(text) and text[pos] in ' \t':
        pos += 1
    if pos >= len(text) or text[pos] != ')':
        return None
    return raw, pos + 1


def _reference_label(value):
    return ' '.join(_unescape_markdown(value).split()).casefold()


def _reference_definitions(text):
    definitions = {}
    remaining = []
    paragraph_open = False
    previous_depth = None
    for line in text.splitlines(keepends=True):
        depth, quoted = _blockquote(line)
        list_start = LIST_MARKER.match(quoted)
        if (previous_depth is not None and depth > previous_depth) or list_start:
            paragraph_open = False
        candidate = quoted[list_start.end():] if list_start else quoted
        match = re.match(r'^ {0,3}\[', candidate) if not paragraph_open else None
        end = _bracket_end(candidate, match.end() - 1) if match else None
        if end is not None and end + 1 < len(candidate) and candidate[end + 1] == ':':
            label = _reference_label(candidate[match.end():end])
            value = candidate[end + 2:].rstrip('\r\n').strip()
            wrapped = '(' + value + ')'
            parsed = _destination(wrapped, 0) if value else None
            if label and end - match.end() <= 999 and parsed and parsed[1] == len(wrapped):
                definitions.setdefault(label, parsed[0])
                remaining.append(''.join('\n' if char == '\n' else ' ' for char in line))
                previous_depth = depth
                continue
        remaining.append(line)
        if REFERENCE_START.match(candidate):
            paragraph_open = True
        else:
            paragraph_open = _can_continue_paragraph(quoted)
        previous_depth = depth if quoted.strip() else None
    return definitions, ''.join(remaining)


def _bracket_pairs(text, html):
    pairs, stack = {}, []
    span = 0
    for pos, char in enumerate(text):
        while span < len(html) and html[span][1] <= pos:
            span += 1
        if span < len(html) and html[span][0] <= pos < html[span][1]:
            continue
        if char == '\n':
            stack.clear()
        elif char == '[' and not _is_escaped(text, pos):
            stack.append(pos)
        elif char == ']' and stack and not _is_escaped(text, pos):
            pairs[stack.pop()] = pos
    return pairs


def _markdown_links(content):
    """Yield destinations of inline and single-line reference links outside code."""
    content = _mask_multiline_code(without_code(content))
    content = _mask_html_blocks(content)
    content = INLINE_CODE.sub(lambda m: ' ' * len(m.group()), content)
    references, content = _reference_definitions(content)
    html = [(m.start(), m.end()) for m in HTML_TAG.finditer(content)]
    pairs = _bracket_pairs(content, html)
    pos = 0
    while pos < len(content):
        if pos not in pairs:
            pos += 1
            continue
        end = pairs[pos]
        label = content[pos + 1:end]
        image = pos > 0 and content[pos - 1] == '!' and not _is_escaped(content, pos - 1)
        after = end + 1
        if after < len(content) and content[after] == '(':
            parsed = _destination(content, after)
            if parsed:
                if not image:
                    yield parsed[0]
                pos = parsed[1]
                continue
        if after < len(content) and content[after] == '[':
            ref_end = pairs.get(after)
            if ref_end is not None:
                ref = content[after + 1:ref_end] or label
                key = _reference_label(ref) if len(ref) <= 999 else ''
                if key in references:
                    if not image:
                        yield references[key]
                pos = ref_end + 1
                continue
        key = _reference_label(label) if references and len(label) <= 999 else ''
        if key in references:
            if not image:
                yield references[key]
            pos = after
        else:
            pos += 1


def check_markdown_links(file, root, anchor_cache=None):
    """Return local Markdown graph edges and diagnostics for one source file."""
    file = Path(file).resolve()
    root = Path(root).resolve()
    if anchor_cache is None:
        anchor_cache = {}
    content = file.read_text(encoding='utf-8')
    edges, errors = [], []
    try:
        source = str(file.relative_to(root))
    except ValueError:
        source = str(file)
    for value in _markdown_links(content):
        target = _unescape_markdown(value)
        # External destinations are out of scope, including ones whose host
        # syntax urllib refuses before it can return a parsed URL.
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target) or target.startswith('//'):
            continue
        try:
            parsed = urlparse(target)
        except ValueError:
            errors.append(f'broken Markdown link {source} -> {value}')
            continue
        if parsed.scheme or parsed.netloc:
            continue
        try:
            path = unquote(parsed.path)
            if path.startswith('/'):
                destination = (root / path.lstrip('/')).resolve()
            else:
                destination = (file.parent / path).resolve() if path else file
        except (OSError, RuntimeError, ValueError):
            errors.append(f'broken Markdown link {source} -> {value}')
            continue
        if not destination.is_relative_to(root):
            errors.append(f'broken Markdown link {source} -> {value} (outside repository)')
            continue
        if not destination.exists():
            errors.append(f'broken Markdown link {source} -> {value}')
            continue
        if destination.is_file() and destination.suffix.lower() == '.md':
            edges.append(destination)
            if parsed.fragment:
                if destination not in anchor_cache:
                    anchor_cache[destination] = markdown_anchors(destination.read_text(encoding='utf-8'))
                if unquote(parsed.fragment) not in anchor_cache[destination]:
                    errors.append(f'broken Markdown anchor {source} -> {value}')
    return edges, errors
