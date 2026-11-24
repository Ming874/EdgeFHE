"""Static checks of the owner screen (demo/ui); Python 3.10 standard library only.

usage (repository root): python -B demo/ui/tests/check_ui.py
Prints UI_STATIC_CHECK_PASS, or one UI_STATIC_CHECK_FAIL line per problem and exits 1. Checks:
  * no http(s) URL in the page, script, module or stylesheet; the only exception is the SVG namespace identifier
    'http://www.w3.org/2000/svg' as the SVG_NS constant of app.js (an XML name passed to createElementNS, never
    fetched); no protocol-relative src/href, no @import, no CSS url() other than data:
  * the Content-Security-Policy meta tag carries the gateway's policy and comes before every script and stylesheet
  * every document.getElementById call has one string literal whose id exists in index.html; ids are unique
  * no browser storage at all (localStorage, sessionStorage, indexedDB, cookies, Cache API, service workers)
  * nothing that the policy would block or that parses markup: no inline script, style element, style attribute or
    inline event handler; no innerHTML, outerHTML, insertAdjacentHTML, document.write, eval or new Function
  * copy rules: no '機率', no reference to the class-balanced sigmoid field, no version labels; the glossary (KV260 or
    板端, never 板子; 操作人員, 加密程式, 紀錄 for the noun, '前 16 個十六進位字元'); a space between Han text and KV260;
    the cloud holds the TLS server key, so a key claim names CKKS ('沒有任何 CKKS 金鑰', never a bare '任何金鑰'); the
    PC rehearsal labels; the required zh-TW texts
  * the zh-TW copy of demo/model/form_cdc.json follows the same glossary and key rule
  * every font-size in style.css is at least 18 px; local references resolve; the fixture is marked synthetic and
    carries the current form_cdc.json as its status.form
"""
import html.parser
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.dirname(HERE)
FORM = os.path.join(os.path.dirname(UI), 'model', 'form_cdc.json')
FILES = ('index.html', 'app.js', 'lib.mjs', 'style.css')
CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
SVG_NS_LINE = "const SVG_NS = 'http://www.w3.org/2000/svg';"
TITLE = '糖尿病風險評估：KV260 本機加密推論（研究展示，非醫療診斷）'
REQUIRED = {
    'index.html': (TITLE, '請與雲端畫面及 Wireshark 比對', '誰持有什麼', '雲端看得到／看不到', '請只填寫虛構的回答',
                   '沒有任何 CKKS 金鑰，無法解密'),
    'lib.mjs': ('明文・僅本機', '晶片內明文（PS→PL DMA）', "cipher: { label: '密文'", '密文＋TLS 1.3', '公開中繼資料',
                '結果・僅擁有者', '依本次實測時間重播（×1/', 'client JSON', '板上核心計數', "gateway: '閘道'",
                'CKKS 與明文模型相差', '，不判定', '雲端沒有任何 CKKS 金鑰，無法解密',
                '本系統不會自動重試，也不會中斷加密程式。', 'PC 演練（PL 位元精確軟體模型）', 'PC 演練時間',
                "rehearsal && source === 'kernel' ? '演練容器核心計數'", '此圖示標示演練容器核心計數的即時標記（PC 演練值'),
    'app.js': ('L.timeLabels(isRehearsal())', "L.sourceLabel('kernel', isRehearsal())"),
}
BARE_KEY_CLAIM = re.compile(r'(?<!CKKS )任何金鑰|無任何金鑰|不持有任何金鑰')
FORBIDDEN_STORAGE = ('localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'caches.', 'serviceWorker',
                     'openDatabase')
FORBIDDEN_DOM = ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function',
                 "setAttribute('style'", 'setAttribute("style"', '.cssText')
FORBIDDEN_COPY = ('機率', 'V8b', 'v8b', 'rev5', 'rev 5', '這一版', '前一版', '上一版', '前兩版')
# Off-glossary words; the noun is 紀錄 (記錄 stays a verb, as in '記錄於').
GLOSSARY = ('板子', '開發板', '操作者', '加密程序', '前 16 位', '前 16 碼', 'TLS 記錄', 'TLS 1.3 記錄', '的記錄')
# Han text directly against KV260 ('保存於KV260的'); the copy writes '於 KV260 的'.
UNSPACED = re.compile(r'[\u3400-\u9fff]KV260|KV260[\u3400-\u9fff]')

problems = []


def fail(message):
    problems.append(message)


def read(name):
    with open(os.path.join(UI, name), 'rb') as f:
        return f.read().decode('utf-8')


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.order = []
        self.meta_csp = []
        self.scripts = []
        self.links = []
        self.lang = None
        self.charset = False
        self.inline_script = False
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'html':
            self.lang = a.get('lang')
        if 'id' in a:
            self.ids.append(a['id'])
        for name in a:
            if name == 'style':
                fail('index.html: style attribute on <%s>' % tag)
            if name.startswith('on'):
                fail('index.html: inline event handler %s on <%s>' % (name, tag))
        for name in ('src', 'href'):
            value = a.get(name)
            if value is not None and value.startswith('//'):
                fail('index.html: protocol-relative %s=%s' % (name, value))
        if tag == 'meta':
            if a.get('charset', '').lower() == 'utf-8':
                self.charset = True
            if (a.get('http-equiv') or '').lower() == 'content-security-policy':
                self.meta_csp.append(a.get('content'))
                self.order.append('csp')
        if tag == 'style':
            fail('index.html: <style> element')
        if tag == 'script':
            self.order.append('resource')
            self.in_script = True
            if 'src' not in a:
                self.inline_script = True
            else:
                self.scripts.append(a)
        if tag == 'link':
            if a.get('rel') == 'stylesheet':
                self.order.append('resource')
            self.links.append(a)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.inline_script = True


def check_urls(texts):
    for name, text in texts.items():
        body = text.replace(SVG_NS_LINE, '') if name == 'app.js' else text
        for m in re.finditer(r'https?://[^\s\'")<>]*', body):
            fail('%s: external URL %s' % (name, m.group(0)))
    if texts['app.js'].count('http://www.w3.org/2000/svg') != 1 or SVG_NS_LINE not in texts['app.js']:
        fail('app.js: the SVG namespace must appear exactly once, as the SVG_NS constant')
    css = texts['style.css']
    if '@import' in css:
        fail('style.css: @import')
    for m in re.finditer(r'url\(\s*([^)]*)\)', css):
        if not m.group(1).strip('\'" ').startswith('data:'):
            fail('style.css: url(%s)' % m.group(1))


def check_page(text):
    page = Page()
    page.feed(text)
    if page.lang not in ('zh-Hant-TW', 'zh-TW'):
        fail('index.html: <html lang> is %r' % page.lang)
    if not page.charset:
        fail('index.html: <meta charset="utf-8"> missing')
    if page.meta_csp != [CSP]:
        fail('index.html: Content-Security-Policy meta is %r, expected exactly %r' % (page.meta_csp, CSP))
    if 'csp' not in page.order or ('resource' in page.order and page.order.index('csp') > page.order.index('resource')):
        fail('index.html: the CSP meta must come before every script and stylesheet')
    if page.inline_script:
        fail('index.html: inline <script>')
    dupes = sorted(set(i for i in page.ids if page.ids.count(i) > 1))
    if dupes:
        fail('index.html: duplicate ids %s' % ', '.join(dupes))
    for s in page.scripts:
        src = s.get('src', '')
        if s.get('type') != 'module' or not src.startswith('/ui/') or not os.path.isfile(os.path.join(UI, src[4:])):
            fail('index.html: script %r must be a module under /ui/ that exists' % src)
    for link in page.links:
        href = link.get('href', '')
        if link.get('rel') == 'stylesheet':
            if not href.startswith('/ui/') or not os.path.isfile(os.path.join(UI, href[4:])):
                fail('index.html: stylesheet %r must be under /ui/ and exist' % href)
        elif not href.startswith('data:'):
            fail('index.html: <link> %r' % href)
    return set(page.ids)


def check_ids(js_texts, ids):
    count = 0
    for name, text in js_texts.items():
        for m in re.finditer(r'getElementById\s*\(([^)]*)\)', text):
            arg = m.group(1).strip()
            lit = re.fullmatch(r"'([A-Za-z0-9_-]+)'|\"([A-Za-z0-9_-]+)\"", arg)
            if not lit:
                fail('%s: getElementById(%s) without a string literal' % (name, arg))
                continue
            count += 1
            target = lit.group(1) or lit.group(2)
            if target not in ids:
                fail('%s: getElementById(%r) has no element in index.html' % (name, target))
    if count == 0:
        fail('app.js: no getElementById call found')
    return count


def check_script_rules(texts):
    for name in ('app.js', 'lib.mjs', 'index.html'):
        text = texts[name]
        for word in FORBIDDEN_STORAGE:
            if word in text:
                fail('%s: browser storage (%s)' % (name, word))
        if name != 'index.html':
            for word in FORBIDDEN_DOM:
                if word in text:
                    fail('%s: %s' % (name, word))
            if 'p_balanced' in text:
                fail('%s: reads the class-balanced sigmoid field' % name)
            for m in re.finditer(r'''import\s[^'"]*['"]([^'"]+)['"]''', text):
                spec = m.group(1)
                if not spec.startswith('./') or not os.path.isfile(os.path.join(UI, spec[2:])):
                    fail('%s: import %r must be a relative file in demo/ui' % (name, spec))
    for name in FILES:
        for word in FORBIDDEN_COPY + GLOSSARY:
            if word in texts[name]:
                fail('%s: copy rule, contains %r' % (name, word))
        for m in BARE_KEY_CLAIM.finditer(texts[name]):
            fail('%s: copy rule, %r claims no key at all; the cloud holds no CKKS key' % (name, m.group(0)))
        for m in UNSPACED.finditer(texts[name]):
            fail('%s: copy rule, %r needs a space between Han text and KV260' % (name, m.group(0)))
    for name, needles in REQUIRED.items():
        for needle in needles:
            if needle not in texts[name]:
                fail('%s: required text %r missing' % (name, needle))


def check_css(css):
    sizes = 0
    for m in re.finditer(r'font-size\s*:\s*([^;}]+)', css):
        sizes += 1
        value = m.group(1).strip()
        px = re.fullmatch(r'(\d+(?:\.\d+)?)px', value)
        rel = re.fullmatch(r'(\d*(?:\.\d+)?)(em|rem)', value)
        if px:
            if float(px.group(1)) < 18:
                fail('style.css: font-size %s below 18 px' % value)
        elif rel:
            if float(rel.group(1)) < 1:
                fail('style.css: font-size %s below the 18 px base' % value)
        elif value not in ('inherit', '100%'):
            fail('style.css: font-size %s is not a px value' % value)
    for m in re.finditer(r'(?<![-\w])font\s*:\s*([^;}]+)', css):
        if m.group(1).strip() != 'inherit':
            fail('style.css: font shorthand %r' % m.group(1).strip())
    if not re.search(r':root\s*{[^}]*font-size:\s*18px', css):
        fail('style.css: the root font size must be 18px')
    if 'prefers-color-scheme: dark' not in css:
        fail('style.css: no dark variant')
    for family in ('Segoe UI', 'Microsoft JhengHei', 'Noto Sans TC'):
        if family not in css:
            fail('style.css: font family %s missing' % family)
    return sizes


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def check_form_copy(form):
    for text in strings(form):
        for word in GLOSSARY:
            if word in text:
                fail('form_cdc.json: copy rule, contains %r' % word)
        for m in BARE_KEY_CLAIM.finditer(text):
            fail('form_cdc.json: copy rule, %r claims no key at all' % m.group(0))
        for m in UNSPACED.finditer(text):
            fail('form_cdc.json: copy rule, %r needs a space between Han text and KV260' % m.group(0))


def check_fixture(form):
    path = os.path.join(UI, 'fixtures', 'run_M_pl.json')
    with open(path, 'rb') as f:
        data = json.loads(f.read().decode('utf-8'))
    if data.get('schema') != 'edgefhe.demo.ui-fixture.v1' or data.get('synthetic') is not True:
        fail('fixtures/run_M_pl.json: schema or synthetic flag')
    if not isinstance(data.get('events'), list) or not data['events']:
        fail('fixtures/run_M_pl.json: no events')
    if (data.get('status') or {}).get('form') != form:
        fail('fixtures/run_M_pl.json: status.form differs from demo/model/form_cdc.json; copy the current form into it')


def main():
    # A failure message quotes zh-TW copy; a cp1252 console must print it escaped instead of raising.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='backslashreplace')
    texts = dict((name, read(name)) for name in FILES)
    check_urls(texts)
    ids = check_page(texts['index.html'])
    lookups = check_ids({'app.js': texts['app.js'], 'lib.mjs': texts['lib.mjs']}, ids)
    if 'document' in texts['lib.mjs'] or 'window' in texts['lib.mjs']:
        fail('lib.mjs: pure logic must not touch document or window')
    check_script_rules(texts)
    sizes = check_css(texts['style.css'])
    with open(FORM, 'rb') as f:
        form = json.loads(f.read().decode('utf-8'))
    check_form_copy(form)
    check_fixture(form)
    for message in problems:
        print('UI_STATIC_CHECK_FAIL %s' % message)
    if problems:
        return 1
    print('UI_STATIC_CHECK_PASS files=%d ids=%d lookups=%d font_sizes=%d' % (len(FILES), len(ids), lookups, sizes))
    return 0


if __name__ == '__main__':
    sys.exit(main())
