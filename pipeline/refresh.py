"""Monthly refresh for Medline Industries - Private Label Portfolio (runs in GitHub Actions).

1. FDA AccessGUDID release files -> Medline item numbers newly published / no longer in distribution
   (plus, once, the GUDID full release -> every device record Medline Industries has ever labeled)
2. medline.com catalog search API -> Medline categories, subcategories, products; item -> category
3. openFDA registration & listing -> manufacturing sites (country of origin) per FDA product code
4. openFDA NDC directory -> Medline-labeled OTC drug products
5. Build data.json, encrypt with the passcode -> data.enc.json (the site) and state/state.enc.json (for next run)

Every source falls back to the previous run's data if it can't be reached, so a blocked source never
empties the site. Only counts are printed: this repo's Actions logs are public.
Env: LABEL_WATCH_PASSCODE (required), AS_OF (optional YYYY-MM-DD), FULL_BACKFILL=1 (re-read the GUDID full
release; it is read automatically on the first run), SKIP_CATALOG=1 / SKIP_COO=1 / SKIP_DRUGS=1 (optional)
"""
import collections, datetime, io, json, os, re, sys, tempfile, time, zipfile
import xml.etree.ElementTree as ET
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crypto_util import encrypt_obj, decrypt_obj
from build_data import build

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, 'state', 'state.enc.json')
PASS = os.environ['LABEL_WATCH_PASSCODE']
AS_OF = os.environ.get('AS_OF') or datetime.date.today().isoformat()
WINDOW = 90
START = (datetime.date.fromisoformat(AS_OF) - datetime.timedelta(days=WINDOW + 5)).isoformat()
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'
S = requests.Session()
S.headers.update({'User-Agent': UA})
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


def load_state():
    if os.path.exists(STATE):
        return decrypt_obj(json.load(open(STATE)), PASS)
    return {'gudid': {}, 'catalog': None, 'coo': None}


# ---------------------------------------------------------------- 1. GUDID
NS = '{http://www.fda.gov/cdrh/gudid}'
MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}


GUDID_DL = 'https://accessgudid.nlm.nih.gov/release_files/download/'


def gudid_index():
    return S.get('https://accessgudid.nlm.nih.gov/download', timeout=60).text


def release_files():
    html = gudid_index()
    names = sorted(set(re.findall(r'release_files/download/(gudid_(?:daily|weekly|monthly)_update_[a-z0-9_]+\.zip)', html)))
    pick = []
    for n in names:
        m = re.match(r'gudid_daily_update_(\d{8})\.zip', n)
        if m and f'{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}' >= START:
            pick.append(n); continue
        m = re.match(r'gudid_weekly_update_(\d{8})_(\d{8})\.zip', n)
        if m and f'{m[2][:4]}-{m[2][4:6]}-{m[2][6:]}' >= START:
            pick.append(n); continue
        m = re.match(r'gudid_monthly_update_([a-z]+)_(\d{4})\.zip', n)
        if m and m[1] in MONTHS:
            y, mo = int(m[2]), MONTHS[m[1]]
            month_end = (datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1)).isoformat()
            if month_end >= START:
                pick.append(n)
    return pick


def txt(el, path):
    v = el.findtext(path)
    return (v or '').strip()


def device_row(el):
    """One GUDID <device> element -> (primary DI, row). Row layout is shared with build_data.py."""
    di = ''
    for ident in el.iter(NS + 'identifier'):
        if txt(ident, NS + 'deviceIdType') == 'Primary':
            di = txt(ident, NS + 'deviceId'); break
    status = txt(el, NS + 'deviceCommDistributionStatus')
    row = [txt(el, NS + 'versionModelNumber') or txt(el, NS + 'catalogNumber'), txt(el, NS + 'devicePublishDate'),
           'D' if status.lower().startswith('not in') else 'N', txt(el, NS + 'deviceCommDistributionEndDate'),
           txt(el, NS + 'brandName'), txt(el, NS + 'deviceDescription'),
           txt(el, f'{NS}gmdnTerms/{NS}gmdn/{NS}gmdnPTName'),
           txt(el, f'{NS}productCodes/{NS}fdaProductCode/{NS}productCode'),
           txt(el, f'{NS}productCodes/{NS}fdaProductCode/{NS}productCodeName'),
           txt(el, NS + 'deviceCount'), di,
           1 if txt(el, f'{NS}sterilization/{NS}deviceSterile') == 'true' else 0,
           1 if txt(el, NS + 'rx') == 'true' else 0]
    return di, row + [txt(el, NS + 'publicVersionDate')]


def keep_row(recs, stats, di, row):
    if di and (di not in recs or row[-1] >= recs[di][-1]):
        recs[di] = row
        stats['medline'] += 1


def is_medline(company):
    return company.upper().startswith('MEDLINE INDUSTRIES')


def parse_xml(fobj, recs, stats):
    """Slow path: full XML parse of every device. Used only if the fast scan finds nothing in a file."""
    for _, el in ET.iterparse(fobj, events=('end',)):
        if el.tag != NS + 'device':
            continue
        stats['devices'] += 1
        company = txt(el, NS + 'companyName')
        if 'medline' in company.lower():
            stats['names'][company] += 1
        if is_medline(company):
            keep_row(recs, stats, *device_row(el))
        el.clear()


DEV_END = b'</device>'
DEV_OPEN = re.compile(rb'<device[\s>]')
CO_NAME = re.compile(rb'<companyName>([^<]*)</companyName>')
ROOT_TAG = re.compile(rb'<(?![?!])[\w:.-]+([^>]*)>')
XMLNS = re.compile(rb'\sxmlns(?::[\w.-]+)?="[^"]*"')


def scan_xml(f, recs, stats):
    """Fast path: split the stream on </device>, check the company name with a regex, and fully parse
    only Medline devices. The full release has 5M+ devices, so parsing every one would take hours."""
    buf, decls = b'', None
    while True:
        block = f.read(1 << 24)
        buf += block
        if decls is None:
            m = ROOT_TAG.search(buf)
            decls = b''.join(XMLNS.findall(m.group(1))) if m else b''
        parts = buf.split(DEV_END)
        buf = parts.pop()
        for p in parts:
            m = CO_NAME.search(p)
            if not m:
                continue
            stats['devices'] += 1
            company = m.group(1).decode('utf-8', 'replace').strip()
            if 'medline' in company.lower():
                stats['names'][company] += 1
            if not is_medline(company):
                continue
            o = DEV_OPEN.search(p)
            if not o:
                stats['bad'] += 1; continue
            frag = p[o.start():] + DEV_END
            if b'xmlns' not in frag[:frag.index(b'>')]:
                frag = frag[:7] + decls + frag[7:]
            try:
                keep_row(recs, stats, *device_row(ET.fromstring(frag)))
            except ET.ParseError:
                stats['bad'] += 1
        if not block:
            break


def new_stats():
    return {'devices': 0, 'medline': 0, 'bad': 0, 'names': collections.Counter()}


def walk_zip(src, recs, stats):
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            name = info.filename.lower()
            if name.endswith('.zip'):
                walk_zip(io.BytesIO(z.read(info)), recs, stats)
            elif name.endswith('.xml'):
                before = stats['devices']
                with z.open(info) as f:
                    scan_xml(f, recs, stats)
                if stats['devices'] == before and info.file_size > 10000:
                    with z.open(info) as f:
                        parse_xml(f, recs, stats)


def pull_full():
    """GUDID full release: every device record on file, including items that haven't changed in years."""
    hits = re.findall(r'release_files/download/(gudid_full_release_(\d{8})\.zip)', gudid_index())
    if not hits:
        raise RuntimeError('no full release listed')
    name, date = sorted(set(hits))[-1]
    path = os.path.join(tempfile.gettempdir(), name)
    t0 = time.time()
    with S.get(GUDID_DL + name, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(path, 'wb') as out:
            for chunk in r.iter_content(1 << 22):
                out.write(chunk)
    log(f'Full release: {name} downloaded ({os.path.getsize(path) // 2**20} MB, {time.time() - t0:.0f}s)')
    recs, stats = {}, new_stats()
    walk_zip(path, recs, stats)
    os.remove(path)
    log(f"Full release: scanned {stats['devices']} device records in {time.time() - t0:.0f}s, "
        f"{len(recs)} Medline Industries records, {stats['bad']} unreadable")
    log('Labeler names containing "Medline": ' + '; '.join(f'{n} ({c})' for n, c in stats['names'].most_common(12)))
    return recs, f'{date[:4]}-{date[4:6]}-{date[6:]}'


def pull_gudid(state):
    recs = dict(state.get('gudid') or {})
    files = release_files()
    log(f'GUDID: {len(files)} release files since {START}')
    stats = new_stats()
    for n in files:
        for attempt in range(3):
            try:
                r = S.get(GUDID_DL + n, timeout=300)
                r.raise_for_status()
                walk_zip(io.BytesIO(r.content), recs, stats)
                break
            except Exception as e:
                log(f'  retry {n}: {type(e).__name__}')
                time.sleep(10)
    log(f"GUDID: scanned {stats['devices']} device records, {stats['medline']} Medline updates, {stats['bad']} unreadable")
    cutoff = START
    keep = {di: r for di, r in recs.items()
            if (r[2] == 'N' and r[1] >= cutoff) or (r[2] == 'D' and (r[3] or r[-1]) >= cutoff)}
    return keep, recs


# ---------------------------------------------------------------- 2. medline.com catalog
API = 'https://apic.medline.com/ecom/catalog-search/products/v1'
MH = {'Accept': 'application/json', 'Origin': 'https://www.medline.com', 'Referer': 'https://www.medline.com/'}


def mq(search_type, term, start=0, n=200, facets=''):
    params = {'searchType': search_type, 'catalog': 'catalog30003', 'startRecord': start, 'numOfRecords': n,
              'searchTerm': term, 'sortField': '', 'sortOrder': '', 'soldTo': '', 'selectedFacets': facets,
              'url': f'https://www.medline.com/category/x/{term}/products', 'refUrl': '', 'formulary': 'false',
              'formularyType': '', 'accountLinked': 'false', 'showExactMatch': '', 'showALBadge': 'true',
              'segment': '', 'shipTo': '', 'availability': 'false', 'onPromo': 'false', 'discontinued': 'false',
              'latexFree': 'false', 'shopNow': 'false'}
    for attempt in range(3):
        r = S.get(API, params=params, headers=MH, timeout=60)
        if r.status_code == 200:
            return r.json()
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f'medline api {r.status_code}')


def is_real(v):
    return not re.search(r'campaign|CCPTEST|brg_dyn', (v.get('name') or '') + (v.get('categoryId') or ''), re.I)


def pull_catalog(prev, items):
    menu = S.get('https://apic.medline.com/ecom/nav/v1/menu-navigation/service/menu/shipto/0', headers=MH, timeout=60).json()
    tops = []
    def walk(o):
        if isinstance(o, list):
            for x in o: walk(x)
        elif isinstance(o, dict):
            url = o.get('item_url') or ''
            m = re.search(r'/category/[^/]+/((?:cat\d+)|(?:Z05-CA[\d_]+))', url)
            if m and o.get('item_depth') == 0 and [o['item_name'], m[1]] not in tops:
                tops.append([o['item_name'], m[1]])
            for v in o.values(): walk(v)
    walk(menu)
    if len(tops) < 10:
        raise RuntimeError('menu parse failed')
    subs, fams = {}, {}
    for nm, cid in tops:
        first = mq('category', cid, 0, 1, 'Manufacturer:MEDLINE')
        cf = next((f for f in first.get('facets') or [] if f.get('isCategoryFacet')), None)
        kids = [v for v in (cf or {}).get('facetValues', []) if is_real(v)]
        subs[cid] = [[k['categoryId'], k['name'], int(k['count'])] for k in kids]
        tag = {}
        for k in kids:
            start, total = 0, 1
            while start < total:
                b = mq('category', k['categoryId'], start, 200, 'Manufacturer:MEDLINE')
                total = int(b.get('totalNumRecords') or 0)
                prods = b.get('products') or []
                for p in prods:
                    tag.setdefault(p['productId'], []).append(k['name'])
                start += 200
                if not prods: break
                time.sleep(0.3)
        out, start, total = [], 0, 1
        while start < total:
            a = mq('category', cid, start, 200, 'Manufacturer:MEDLINE')
            total = int(a.get('totalNumRecords') or 0)
            prods = a.get('products') or []
            out += [[p['productId'], p['displayName'], ' | '.join(tag.get(p['productId'], []))] for p in prods]
            start += 200
            if not prods: break
            time.sleep(0.3)
        fams[cid] = out
    item_cat = dict((prev or {}).get('itemCat') or {})
    todo = [i for i in items if i not in item_cat]
    for it in todo:
        try:
            j = mq('keyword', it, 0, 3)
            cf = next((f for f in j.get('facets') or [] if f.get('isCategoryFacet')), None)
            tops_hit = [v['name'] for v in (cf or {}).get('facetValues', []) if is_real(v)]
            p = (j.get('products') or [None])[0]
            item_cat[it] = {'n': int(j.get('totalNumRecords') or 0), 'top': ' | '.join(tops_hit),
                            'fam': f"{p['productId']}|{p['displayName']}|{p['manufacturer']}" if p else ''}
        except Exception as e:
            item_cat[it] = {'err': type(e).__name__}
        time.sleep(0.3)
    log(f'Catalog: {len(tops)} categories, {sum(len(v) for v in fams.values())} Medline products, {len(todo)} new item lookups')
    return {'tops': tops, 'subs': subs, 'fams': fams, 'itemCat': item_cat}


# ---------------------------------------------------------------- 3. openFDA manufacturing sites
def pull_coo():
    est = []
    queries = ['registration.owner_operator.firm_name:"medline"',
               'proprietary_name:"medline" AND NOT registration.owner_operator.firm_name:"medline"']
    for q in queries:
        skip, total = 0, 1
        while skip < total:
            r = S.get('https://api.fda.gov/device/registrationlisting.json',
                      params={'search': q, 'limit': 1000, 'skip': skip}, timeout=120)
            r.raise_for_status()
            j = r.json(); total = j['meta']['results']['total']
            est += j['results']; skip += 1000
            time.sleep(1)
    m = {}
    for x in est:
        types = '; '.join(x.get('establishment_type') or [])
        if not re.search(r'Manufacture Medical Device|Contract Manufacturer', types, re.I):
            continue
        reg = x['registration']; owner = (reg.get('owner_operator') or {}).get('firm_name') or ''
        cm = ('medline' not in owner.lower()) or ('Contract Manufacturer' in types)
        key = f"{reg.get('iso_country_code')}|{' '.join((reg.get('name') or '').split())}" + ('|CM' if cm else '')
        for p in x.get('products') or []:
            m.setdefault(p['product_code'], set()).add(key)
    log(f'Origin: {len(est)} establishment listings, {len(m)} product codes')
    return {k: sorted(v) for k, v in m.items()}


# ---------------------------------------------------------------- 4. openFDA NDC (OTC drugs)
def pull_drugs():
    """OTC drug products (antiseptics, skin care, oral care...) are listed with FDA as drugs, not devices."""
    out, skip, total = [], 0, 1
    while skip < total:
        r = S.get('https://api.fda.gov/drug/ndc.json',
                  params={'search': 'labeler_name:"medline"', 'limit': 1000, 'skip': skip}, timeout=120)
        r.raise_for_status()
        j = r.json(); total = j['meta']['results']['total']
        for x in j['results']:
            if 'medline' not in (x.get('labeler_name') or '').lower():
                continue
            out.append({'ndc': x.get('product_ndc') or '', 'brand': x.get('brand_name') or '',
                        'generic': x.get('generic_name') or '', 'form': x.get('dosage_form') or '',
                        'type': x.get('product_type') or '', 'start': x.get('marketing_start_date') or '',
                        'exp': x.get('listing_expiration_date') or '',
                        'pkgs': [p.get('description') or '' for p in x.get('packaging') or []]})
        skip += 1000
        time.sleep(1)
    log(f'Drugs: {len(out)} Medline-labeled NDC products')
    return out


# ---------------------------------------------------------------- main
def merge(archive, recs):
    for di, r in recs.items():
        if di not in archive or r[-1] >= archive[di][-1]:
            archive[di] = r


def main():
    state = load_state()
    # Every Medline record ever seen, never pruned: the full item list and the item #s under each product
    archive = dict(state.get('archive') or {})
    full_as_of = state.get('fullAsOf')
    if os.environ.get('FULL_BACKFILL') == '1' or not full_as_of:
        try:
            full, full_as_of = pull_full()
            merge(archive, full)
        except Exception as e:
            log(f'Full release failed ({type(e).__name__}: {str(e)[:80]}); keeping previous item archive')
    seen = {}
    try:
        recs, seen = pull_gudid(state)
    except Exception as e:
        log(f'GUDID failed ({type(e).__name__}); keeping previous records'); recs = state['gudid']
    merge(archive, recs); merge(archive, seen)
    log(f'Item archive: {len(archive)} Medline device records (full release {full_as_of or "not loaded"})')
    rows = [r[:-1] for r in recs.values()]
    items = sorted({r[0] for r in rows})

    catalog = state.get('catalog')
    if os.environ.get('SKIP_CATALOG') != '1':
        try:
            catalog = pull_catalog(catalog, items)
        except Exception as e:
            log(f'Catalog failed ({type(e).__name__}: {str(e)[:80]}); keeping previous catalog')
    coo = state.get('coo')
    if os.environ.get('SKIP_COO') != '1':
        try:
            coo = pull_coo()
        except Exception as e:
            log(f'Origin failed ({type(e).__name__}); keeping previous sites')
    drugs = state.get('drugs')
    if os.environ.get('SKIP_DRUGS') != '1':
        try:
            drugs = pull_drugs()
        except Exception as e:
            log(f'Drugs failed ({type(e).__name__}); keeping previous drug list')

    data = build({'rows': rows}, catalog, coo or {}, AS_OF, WINDOW, state.get('deep'),
                 archive=[r[:-1] for r in archive.values()], drugs=drugs or [])
    allrows = data['all']
    by_src = collections.Counter(r[11] for r in allrows)
    kits = sum(r[12] for r in allrows)
    log(f"All items: {sum(r[3] == 'N' for r in allrows)} active of {len(allrows)} Medline item #s "
        f"({len(allrows) - kits} catalog items, {kits} kits/trays/packs, {by_src['d']} drug products); category from "
        f"medline.com {by_src['m']}, FDA device type rules {by_src['r']}, product code/GMDN majority "
        f"{by_src['p'] + by_src['g']}, drug type {by_src['d']}, not matched {by_src['u']}")
    asof = datetime.date.fromisoformat(AS_OF)
    def n_new(days):
        cut = (asof - datetime.timedelta(days=days)).isoformat()
        return len({r['item'] for r in data['skus'] if r['status'] == 'N' and r['pub'] >= cut})
    log(f"New SKUs 30/60/90 days: {n_new(30)} / {n_new(60)} / {n_new(90)}; exits: {len({r['item'] for r in data['skus'] if r['status'] == 'D'})}")

    json.dump(encrypt_obj(data, PASS), open(os.path.join(ROOT, 'data.enc.json'), 'w'))
    json.dump(encrypt_obj({'gudid': recs, 'archive': archive, 'fullAsOf': full_as_of, 'drugs': drugs,
                           'catalog': catalog, 'coo': coo, 'deep': state.get('deep'), 'asOf': AS_OF}, PASS),
              open(STATE, 'w'))
    for f in ('data.enc.json', 'state/state.enc.json'):
        log(f'{f}: {os.path.getsize(os.path.join(ROOT, f)) / 2**20:.1f} MB')
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        open(summ, 'a').write('## Private Label Portfolio refresh ' + AS_OF + '\n\n' + '\n'.join('- ' + l for l in LOG) + '\n')


if __name__ == '__main__':
    main()
