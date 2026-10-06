"""Generate the mobile contract: every API call the phone app makes.

The mobile adapter (integrations/mobile_adapter) serves the phone's cloud
paths on the desktop, so the list of those paths has to be complete and has
to come from the phone's code, never from memory.  This reads the phone repo
(Hevolve_React_Native) and writes one row per call site:

    {"method", "path", "base", "client", "caller"}

* native: Retrofit annotations (@GET/@POST/... with literal or BuildConfig
  paths), the base URL of every client the interface is created on, and the
  raw HttpURLConnection / OkHttp calls to a HARTOS path;
* js: the request helpers in services/*.js (get/post/..., codingGet, adminPost,
  _mailerPost, ...) and fetch() calls with a literal or `${base}` path.

A call whose base cannot be resolved is written with base "unresolved", never
dropped: the point of the file is that nothing the phone calls is missing.

Usage:
  python scripts/gen_mobile_contract.py <phone_repo> [--out path] [--check]

--check compares a fresh scan with the committed contract and exits 1 when the
phone calls a (method, path) the contract does not list.
"""
import argparse
import json
import os
import re
import sys

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'integrations', 'mobile_adapter', 'contract.json')

_HTTP = ('GET', 'POST', 'PUT', 'DELETE', 'PATCH')


def _read(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, '/')


def _line(text, index):
    return text.count('\n', 0, index) + 1


# --- native ----------------------------------------------------------------

def build_config(root):
    """BuildConfig string fields from app/build.gradle: name -> sorted values."""
    gradle = _read(os.path.join(root, 'android', 'app', 'build.gradle'))
    fields = {}
    for name, value in re.findall(
            r'buildConfigField\s+"String",\s+"(\w+)",\s+"\\"([^"\\]*)\\""', gradle):
        fields.setdefault(name, set()).add(value)
    return {k: sorted(v) for k, v in fields.items()}


def _java_files(root):
    base = os.path.join(root, 'android', 'app', 'src', 'main', 'java')
    for d, _dirs, files in os.walk(base):
        for f in files:
            if f.endswith(('.java', '.kt')):
                yield os.path.join(d, f)


def _arg_count(params):
    """Top-level parameters in a Java/Kotlin parameter list."""
    depth, count, seen = 0, 0, False
    for ch in params:
        if ch in '<([':
            depth += 1
        elif ch in '>)]':
            depth -= 1
        elif ch == ',' and depth == 0:
            count += 1
        if not ch.isspace():
            seen = True
    return count + 1 if seen else 0


def retrofit_interfaces(root, bc):
    """interface name -> [(method, path, file:line, java method name, arg count)]"""
    found = {}
    ann = re.compile(r'@(GET|POST|PUT|DELETE|PATCH)\(\s*(?:"([^"]*)"|BuildConfig\.(\w+))\s*\)')
    # whitespace, // and /* */ comments and further annotations, then the
    # return type and the method name (a trailing "// For books PDFs" after an
    # annotation is common in these interfaces)
    gap = r'(?:\s+|//[^\n]*\n|/\*.*?\*/|@\w+(?:\([^)]*\))?)*'
    sig = re.compile(gap + r'[\w<>, ?.\[\]]+\s+(\w+)\s*\(', re.S)
    for path in _java_files(root):
        text = _read(path)
        m = re.search(r'\binterface\s+(\w+)', text)
        if not m:
            continue
        rows = []
        for a in ann.finditer(text):
            method, literal, const = a.group(1), a.group(2), a.group(3)
            s = sig.match(text, a.end())
            name, nargs = None, None
            if s:
                name = s.group(1)
                close, depth = s.end(), 1
                while close < len(text) and depth:
                    depth += {'(': 1, ')': -1}.get(text[close], 0)
                    close += 1
                nargs = _arg_count(re.sub(r'@\w+(\([^)]*\))?', '', text[s.end():close - 1]))
            values = [literal] if literal is not None else bc.get(const, ['BuildConfig.' + const])
            for v in values:
                rows.append((method, v, f'{_rel(root, path)}:{_line(text, a.start())}', name, nargs))
        if rows:
            found[m.group(1)] = rows
    return found


_MANAGER_BASE = {  # RetrofitManager getters -> the client's base
    'getDefaultRetrofit': 'base_url_db',
    'getSignLoginRetrofit': 'base_url_SignLogin',
    'getUserRetrofit': 'user_retrofit(resolved: local > LAN > cloud)',
}


def _calls_on(text, var, start, span=4000):
    """{(method name, arg count)} called on `var` in the code after `start`."""
    used = set()
    window = text[start:start + span]
    for c in re.finditer(r'\b' + re.escape(var) + r'\.(\w+)\(', window):
        close, depth = c.end(), 1
        while close < len(window) and depth:
            depth += {'(': 1, ')': -1}.get(window[close], 0)
            close += 1
        used.add((c.group(1), _arg_count(window[c.end():close - 1])))
    return used


def client_uses(root, bc, interfaces):
    """interface -> [(base, {(method name, arg count)} or None for every method)]

    Attributed per `.create(Iface.class)` site: an interface created on three
    bases (ChatAttachImageUploadApi) maps each method to the base of the site
    that calls it, not to all three."""
    uses = {name: [] for name in interfaces}
    create = re.compile(r'(\w+)\(\)\s*\.create\((\w+)\.class\)|(?:(\w+)\s*=\s*)?(\w+)\.create\((\w+)\.class\)')
    for path in _java_files(root):
        text = _read(path)
        for m in create.finditer(text):
            if m.group(1):
                getter, iface, var = m.group(1), m.group(2), None
            else:
                getter, iface, var = None, m.group(5), m.group(3)
            if iface not in uses:
                continue
            if getter in _MANAGER_BASE:
                key = _MANAGER_BASE[getter]
                uses[iface].append((', '.join(bc.get(key, [key])), None))
                continue
            # an inline client: the nearest baseUrl / URL assignment above it
            window = text[max(0, m.start() - 2500):m.start()]
            lit = re.findall(r'\.baseUrl\(\s*"([^"]+)"', window)
            const = re.findall(r'URL\s*=\s*BuildConfig\.(\w+)', window)
            strres = re.findall(r'\.baseUrl\(\s*getString\(R\.string\.(\w+)\)', window)
            literal_url = re.findall(r'URL\s*=\s*"([^"]+)"', window)
            if const:
                base = ', '.join(bc.get(const[-1], [const[-1]]))
            elif lit:
                base = lit[-1]
            elif literal_url:
                base = literal_url[-1]
            elif strres:
                base = 'R.string.' + strres[-1]
            else:
                base = 'unresolved'
            used = _calls_on(text, var, m.end()) if var else set()
            uses[iface].append((base, used or None))
    return uses


def _bases_for(uses, name, nargs):
    out = set()
    for base, used in uses:
        if used is None:
            out.add(base)
        elif (name, nargs) in used:
            out.add(base)
    return out


def _raw_method(window):
    """The HTTP method a raw HttpURLConnection / OkHttp call uses: the
    connection's setRequestMethod, else the Request.Builder verb, else GET.
    A JSONObject's .put( is not a verb."""
    m = re.search(r'setRequestMethod\(\s*"(\w+)"\s*\)', window)
    if m:
        return m.group(1).upper()
    m = re.search(r'(?:[Bb]uilder\(\)|\.url\([^)]*\)|\bbuilder)\s*\.(post|put|delete|patch)\(', window)
    return m.group(1).upper() if m else 'GET'


def raw_native_calls(root):
    """HttpURLConnection / OkHttp / Uri calls to a HARTOS path on a resolved base."""
    rows = []
    concat = re.compile(r'(?:getBaseUrl\(\)|\bbaseUrl)\s*\+\s*"(/[^"]+)"')
    uri = re.compile(r'\.append(?:Encoded)?Path\(\s*"([^"]+)"\s*\)')
    for path in _java_files(root):
        text = _read(path)
        if 'HartosBaseUrlResolver' not in text and 'getBaseUrl' not in text:
            continue
        for pat in (concat, uri):
            for m in pat.finditer(text):
                p = '/' + m.group(1).lstrip('/')
                rows.append({'method': _raw_method(text[m.end():m.end() + 900]), 'path': p,
                             'base': 'resolved desktop (HartosBaseUrlResolver)',
                             'client': 'raw', 'caller': f'{_rel(root, path)}:{_line(text, m.start())}'})
    return rows


# --- js --------------------------------------------------------------------

_SOCIAL = '/api/social (resolved peer, else central)'
_RESOLVED = 'resolved base (endpointResolver)'


def _helper_method(name, body):
    m = re.search(r"method:\s*['\"](\w+)['\"]", body)
    if m:
        return m.group(1).upper()
    low = name.lower().lstrip('_')
    for prefix, verb in (('get', 'GET'), ('post', 'POST'), ('put', 'PUT'),
                         ('patch', 'PATCH'), ('del', 'DELETE'), ('remove', 'DELETE')):
        if low.startswith(prefix) or low.endswith(prefix):
            return verb
    return 'GET'


def js_helpers(text):
    """name -> (http method, base) for every request helper a file defines:
    an `async (path, ...)` function whose body fetches `path` on some base.
    The base is read from the body, so a new helper is picked up without
    editing this script."""
    consts = {}
    for name, value in re.findall(r'(?:let|const)\s+([A-Z_]+)\s*=\s*`([^`]+)`', text):
        consts[name] = value.replace('${_apiBase}', 'mailer _apiBase ')
    helpers = {}
    for m in re.finditer(r'(?:const|let)\s+(\w+)\s*=\s*async\s*\(\s*path\b', text):
        end = text.find('\n};', m.end())
        body = text[m.end(): end if end != -1 else m.end() + 1500]
        if '_fetchSocial(' in body:
            base = _SOCIAL
        else:
            b = re.search(r'buildUrl\(\s*path\s*,[^,]*,\s*(\w+)', body)
            if b and b.group(1) in consts:
                base = consts[b.group(1)].strip()
            elif 'getAdminBase()' in body:
                base = 'mailer _apiBase /api/admin'
            elif re.search(r'getApiBaseUrl\(\)|_resolveChatBase\(\)|_resolve\w*Base\(\)', body):
                base = _RESOLVED
            elif '${_apiBase}' in body:
                base = 'mailer _apiBase'
            else:
                base = 'unresolved'
        helpers[m.group(1)] = (_helper_method(m.group(1), body), base)
    return helpers


def _js_files(root):
    for top in ('services', 'components', 'hooks', 'utils'):
        base = os.path.join(root, top)
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in ('__tests__', 'node_modules')]
            for f in files:
                if f.endswith(('.js', '.jsx', '.ts', '.tsx')) and '.test.' not in f:
                    yield os.path.join(d, f)
    for f in os.listdir(root):
        if f.endswith('.js'):
            yield os.path.join(root, f)


def _norm_template(path):
    return re.sub(r'\$\{[^}]*\}', '{param}', path)


def js_calls(root):
    rows = []
    for path in _js_files(root):
        text = _read(path)
        helpers = js_helpers(text)
        if helpers:
            pat = re.compile(r'(?<![.\w])(' + '|'.join(map(re.escape, helpers)) + r')\(\s*([\'"`])([^\'"`]*)\2')
            for m in pat.finditer(text):
                method, base = helpers[m.group(1)]
                p = '/' + _norm_template(m.group(3)).lstrip('/')
                rows.append({'method': method, 'path': p.split('?')[0], 'base': base,
                             'client': f'js {m.group(1)}', 'caller': f'{_rel(root, path)}:{_line(text, m.start())}'})
        for m in re.finditer(r'fetch\(\s*([\'"`])([^\'"`]+)\1', text):
            target = m.group(2)
            if target.upper() in _HTTP:
                # RNFetchBlob.fetch(method, url, ...): the URL is the next argument
                arg = re.match(r'\s*,\s*([\w.]+)', text[m.end():])
                rows.append({'method': target.upper(),
                             'path': '{' + (arg.group(1) if arg else 'url') + '}',
                             'base': 'variable url (RNFetchBlob download)',
                             'client': 'js RNFetchBlob',
                             'caller': f'{_rel(root, path)}:{_line(text, m.start())}'})
                continue
            window = text[m.end():m.end() + 300]
            meth = re.search(r"method:\s*['\"](\w+)['\"]", window)
            method = meth.group(1).upper() if meth else 'GET'
            url = _norm_template(target)
            mm = re.match(r'(https?://[^/]+)(/.*)?$', url)
            if mm:
                base, p = mm.group(1), (mm.group(2) or '/')
            elif url.startswith('{param}'):
                base, p = 'resolved base (endpointResolver / module base)', url[len('{param}'):] or '/'
            else:
                base, p = 'unresolved', url
            rows.append({'method': method, 'path': p.split('?')[0], 'base': base,
                         'client': 'js fetch', 'caller': f'{_rel(root, path)}:{_line(text, m.start())}'})
    return rows


def scan(root):
    bc = build_config(root)
    interfaces = retrofit_interfaces(root, bc)
    uses = client_uses(root, bc, interfaces)
    rows = []
    for iface, calls in sorted(interfaces.items()):
        for method, p, caller, name, nargs in calls:
            bases = _bases_for(uses.get(iface, []), name, nargs)
            if not bases:
                # created nowhere, or created only where none of its calls
                # match this method: reported, never dropped
                bases = {'not called' if uses.get(iface) else 'not created anywhere'}
            for base in sorted(bases):
                rows.append({'method': method, 'path': '/' + p.lstrip('/'), 'base': base,
                             'client': f'retrofit {iface}.{name}', 'caller': caller})
    rows.extend(raw_native_calls(root))
    rows.extend(js_calls(root))
    seen, unique = set(), []
    for r in rows:
        k = (r['method'], r['path'], r['base'], r['client'], r['caller'])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    return sorted(unique, key=lambda r: (r['path'], r['method'], r['caller']))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('phone_repo')
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args(argv)
    rows = scan(args.phone_repo)
    if args.check:
        with open(args.out, encoding='utf-8') as fh:
            listed = {(r['method'], r['path']) for r in json.load(fh)['calls']}
        missing = sorted({(r['method'], r['path']) for r in rows} - listed)
        for method, p in missing:
            print(f'NOT IN CONTRACT: {method} {p}')
        return 1 if missing else 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'generated_by': 'scripts/gen_mobile_contract.py', 'calls': rows}, fh, indent=1)
        fh.write('\n')
    paths = {(r['method'], r['path']) for r in rows}
    unresolved = sum(1 for r in rows if r['base'] == 'unresolved')
    print(f'{len(rows)} call sites, {len(paths)} distinct (method, path), {unresolved} unresolved base')
    return 0


if __name__ == '__main__':
    sys.exit(main())
