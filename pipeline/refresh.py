"""Monthly refresh for Medline Industries - Private Label Portfolio (runs in GitHub Actions).

1. FDA AccessGUDID release files -> Medline item numbers newly published / no longer in distribution
2. medline.com catalog search API -> Medline categories, subcategories, product families; item -> category
3. openFDA registration & listing -> manufacturing sites (country of origin) per FDA product code
4. Build data.json, encrypt with the passcode -> data.enc.json (the site) and state/state.enc.json (for next run)

Every source falls back to the previous run's data if it can't be reached, so a blocked source never
empties the site. Only counts are printed: this repo's Actions logs are public.
Env: LABEL_WATCH_PASSCODE (required), AS_OF (optional YYYY-MM-DD), SKIP_CATALOG=1 / SKIP_COO=1 (optional)
"""
import datetime, io, json, os, re, sys, time, zipfile
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


def release_files():
    html = S.get('https://accessgudid.nlm.nih.gov/download', timeout=60).text
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


def parse_xml(fobj, recs, stats):
    for _, el in ET.iterparse(fobj, events=('end',)):
        if el.tag != NS + 'device':
            continue
        stats['devices'] += 1
        company = txt(el, NS + 'companyName')
        if company.upper().startswith('MEDLINE INDUSTRIES'):
            di = ''
            for ident in el.iter(NS + 'identifier'):
                if txt(ident, NS + 'deviceIdType') == 'Primary':
                    di = txt(ident, NS + 'deviceId'); break
            status = txt(el, NS + 'deviceCommDistributionStatus')
            row = [txt(el, NS + 'versionModelNumber'), txt(el, NS + 'devicePublishDate'),
                   'D' if status.lower().startswith('not in') else 'N', txt(el, NS + 'deviceCommDistributionEndDate'),
                   txt(el, NS + 'brandName'), txt(el, NS + 'deviceDescription'),
                   txt(el, f'{NS}gmdnTerms/{NS}gmdn/{NS}gmdnPTName'),
                   txt(el, f'{NS}productCodes/{NS}fdaProductCode/{NS}productCode'),
                   txt(el, f'{NS}productCodes/{NS}fdaProductCode/{NS}productCodeName'),
                   txt(el, NS + 'deviceCount'), di,
                   1 if txt(el, f'{NS}sterilization/{NS}deviceSterile') == 'true' else 0,
                   1 if txt(el, NS + 'rx') == 'true' else 0]
            vd = txt(el, NS + 'publicVersionDate')
            if di and (di not in recs or vd >= recs[di][-1]):
                recs[di] = row + [vd]
                stats['medline'] += 1
        el.clear()


def walk_zip(data, recs, stats):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.filename.lower().endswith('.zip'):
                walk_zip(z.read(info), recs, stats)
            elif info.filename.lower().endswith('.xml'):
                with z.open(info) as f:
                    parse_xml(f, recs, stats)


def pull_gudid(state):
    recs = dict(state.get('gudid') or {})
    files = release_files()
    log(f'GUDID: {len(files)} release files since {START}')
    stats = {'devices': 0, 'medline': 0}
    for n in files:
        for attempt in range(3):
            try:
                r = S.get('https://accessgudid.nlm.nih.gov/release_files/download/' + n, timeout=300)
                r.raise_for_status()
                walk_zip(r.content, recs, stats)
                break
            except Exception as e:
                log(f'  retry {n}: {type(e).__name__}')
                time.sleep(10)
    log(f"GUDID: scanned {stats['devices']} device records, {stats['medline']} Medline updates")
    cutoff = START
    keep = {di: r for di, r in recs.items()
            if (r[2] == 'N' and r[1] >= cutoff) or (r[2] == 'D' and (r[3] or r[-1]) >= cutoff)}
    return keep


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
    log(f'Catalog: {len(tops)} categories, {sum(len(v) for v in fams.values())} Medline families, {len(todo)} new item lookups')
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


# ---------------------------------------------------------------- main
def main():
    state = load_state()
    try:
        recs = pull_gudid(state)
    except Exception as e:
        log(f'GUDID failed ({type(e).__name__}); keeping previous records'); recs = state['gudid']
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

    data = build({'rows': rows}, catalog, coo or {}, AS_OF, WINDOW, state.get('deep'))
    asof = datetime.date.fromisoformat(AS_OF)
    def n_new(days):
        cut = (asof - datetime.timedelta(days=days)).isoformat()
        return len({r['item'] for r in data['skus'] if r['status'] == 'N' and r['pub'] >= cut})
    log(f"New SKUs 30/60/90 days: {n_new(30)} / {n_new(60)} / {n_new(90)}; exits: {len({r['item'] for r in data['skus'] if r['status'] == 'D'})}")

    json.dump(encrypt_obj(data, PASS), open(os.path.join(ROOT, 'data.enc.json'), 'w'))
    json.dump(encrypt_obj({'gudid': recs, 'catalog': catalog, 'coo': coo, 'deep': state.get('deep'), 'asOf': AS_OF}, PASS), open(STATE, 'w'))
    summ = os.environ.get('GITHUB_STEP_SUMMARY')
    if summ:
        open(summ, 'a').write('## Private Label Portfolio refresh ' + AS_OF + '\n\n' + '\n'.join('- ' + l for l in LOG) + '\n')


if __name__ == '__main__':
    main()
