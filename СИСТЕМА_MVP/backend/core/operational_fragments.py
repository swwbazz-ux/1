from html.parser import HTMLParser

from django.http import JsonResponse


OPERATIONAL_FRAGMENT_CONTRACT = 'operational-fragment-v1'
_VOID_TAGS = {
    'area',
    'base',
    'br',
    'col',
    'embed',
    'hr',
    'img',
    'input',
    'link',
    'meta',
    'param',
    'source',
    'track',
    'wbr',
}


def _selector_matcher(selector):
    selector = str(selector or '').strip()
    if selector.startswith('[') and selector.endswith(']'):
        attribute = selector[1:-1].strip()
        if attribute and '=' not in attribute:
            return lambda _tag, attrs: attribute in dict(attrs)
    if selector.startswith('.') and len(selector) > 1:
        class_name = selector[1:]

        def matches_class(_tag, attrs):
            classes = dict(attrs).get('class') or ''
            return class_name in classes.split()

        return matches_class
    if selector.startswith('#') and len(selector) > 1:
        element_id = selector[1:]
        return lambda _tag, attrs: dict(attrs).get('id') == element_id
    raise ValueError(f'Unsupported operational fragment selector: {selector!r}')


class _OuterHtmlExtractor(HTMLParser):
    def __init__(self, selector):
        super().__init__(convert_charrefs=False)
        self._matches = _selector_matcher(selector)
        self._capture_depth = 0
        self._suppressed_depth = 0
        self._parts = []
        self.result = ''

    def _append(self, value):
        if self._capture_depth and not self._suppressed_depth and not self.result:
            self._parts.append(value)

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        if self._capture_depth:
            if tag.lower() in {'script', 'style'} or self._suppressed_depth:
                if tag.lower() not in _VOID_TAGS:
                    self._capture_depth += 1
                    self._suppressed_depth += 1
                return
            self._append(raw)
            if tag.lower() not in _VOID_TAGS:
                self._capture_depth += 1
            return
        if not self.result and self._matches(tag, attrs):
            self._parts = [raw]
            if tag.lower() in _VOID_TAGS:
                self.result = ''.join(self._parts)
            else:
                self._capture_depth = 1

    def handle_startendtag(self, tag, attrs):
        raw = self.get_starttag_text()
        if self._capture_depth:
            self._append(raw)
        elif not self.result and self._matches(tag, attrs):
            self.result = raw

    def handle_endtag(self, tag):
        if not self._capture_depth or self.result:
            return
        if self._suppressed_depth:
            self._capture_depth -= 1
            self._suppressed_depth -= 1
            return
        self._append(f'</{tag}>')
        self._capture_depth -= 1
        if self._capture_depth == 0:
            self.result = ''.join(self._parts)

    def handle_data(self, data):
        self._append(data)

    def handle_entityref(self, name):
        self._append(f'&{name};')

    def handle_charref(self, name):
        self._append(f'&#{name};')

    def handle_comment(self, data):
        self._append(f'<!--{data}-->')

    def handle_decl(self, decl):
        self._append(f'<!{decl}>')

    def handle_pi(self, data):
        self._append(f'<?{data}>')


def extract_outer_html(html, selector):
    parser = _OuterHtmlExtractor(selector)
    parser.feed(str(html or ''))
    parser.close()
    return parser.result


def operational_fragment_response(
    rendered_response,
    *,
    screen,
    selector,
    version,
    extra=None,
):
    source = rendered_response.content.decode(rendered_response.charset or 'utf-8')
    fragment = extract_outer_html(source, selector)
    if not fragment:
        response = JsonResponse(
            {
                'contract': OPERATIONAL_FRAGMENT_CONTRACT,
                'screen': screen,
                'error': 'fragment_root_missing',
            },
            status=500,
        )
    else:
        payload = {
            'contract': OPERATIONAL_FRAGMENT_CONTRACT,
            'screen': screen,
            'version': int(version or 0),
            'html': fragment,
        }
        if extra:
            payload.update(extra)
        response = JsonResponse(payload)
    response['Cache-Control'] = 'no-store'
    response['X-Operational-Fragment'] = OPERATIONAL_FRAGMENT_CONTRACT
    return response
