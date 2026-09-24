"""Turn the raw pulls into the site's data object.
gudid   {rows}: Medline GUDID records in the 90-day window (new publishes + not-in-distribution)
catalog {tops, subs, fams, itemCat}: medline.com Medline-brand products and item -> category lookups
coo     {productCode: ["CC|Site name|CM"]}: FDA-listed manufacturing sites
archive [rows]: every Medline GUDID record seen (full release + monthly updates), same row layout as gudid
drugs   [{ndc, brand, generic, form, type, start, exp, pkgs}]: Medline-labeled NDC drug products
"""
import collections, datetime, re

SOURCES = {
    'launches': 'FDA Global Unique Device Identification Database (AccessGUDID) daily/weekly/monthly release files: Medline Industries device records by publish date.',
    'items': 'AccessGUDID full release: every device record labeled by Medline Industries, read once and kept current with the monthly updates.',
    'drugs': 'FDA NDC Directory (openFDA): OTC drug products with Medline as the labeler.',
    'categories': "medline.com catalog search (Medline-brand filter): Medline's own category tree.",
    'origin': 'FDA Establishment Registration & Device Listing (openFDA): Medline-owned and contract manufacturing sites registered for each FDA product code.',
}
# How each item got its category (column 11 of an 'all' row)
SRC = {'m': 'medline.com', 'p': 'inferred (FDA product code)', 'g': 'inferred (device type)',
       'd': 'inferred (drug type)', 'u': 'not matched'}
UNMATCHED = 'Not matched to a medline.com category'
TOPICAL = re.compile(r'CREAM|LOTION|OINTMENT|GEL|PASTE|SOAP|SHAMPOO|CLOTH|SWAB|SPONGE|POWDER|SPRAY|STICK|'
                     r'LIQUID|SOLUTION|FOAM|EMULSION|WASH', re.I)


def iso(d):
    return f'{d[:4]}-{d[4:6]}-{d[6:]}' if len(d) == 8 and d.isdigit() else d


def build(g, c, coo, as_of, window_days=90, deep=None, archive=None, drugs=None):
    cutoff = (datetime.date.fromisoformat(as_of) - datetime.timedelta(days=window_days)).isoformat()
    fam_loc, portfolio = {}, []
    for nm, cid in c['tops']:
        fams = c['fams'].get(cid, [])
        portfolio.append({'id': cid, 'name': nm,
                          'subs': [[s[1], s[2]] for s in c['subs'].get(cid, [])],
                          'fams': [[f[0], f[1], (f[2].split(' | ')[0] if f[2] else '')] for f in fams]})
        for fid, fname, sub in fams:
            fam_loc.setdefault(fid, (nm, sub.split(' | ')[0] if sub else '', fname))
    top_names = {nm for nm, _ in c['tops']}

    def countries(pc):
        out = []
        for s in coo.get(pc, []):
            p = s.split('|')
            out.append({'cc': p[0], 'site': p[1], 'cm': len(p) > 2})
        return out

    item_cat = {}
    for item, v in c['itemCat'].items():
        if not v or v.get('err') or not v.get('n'):
            continue
        fid, fname, mfr = (v.get('fam', '').split('|') + ['', '', ''])[:3]
        if v['n'] > 3 or 'MEDLINE' not in mfr.upper():
            continue
        top = v['top'].split(' | ')[0] if v.get('top') else ''
        loc = fam_loc.get(fid)
        sub = loc[1] if loc else ''
        if not top and loc:
            top = loc[0]
        if top:
            item_cat[item] = (top, sub, fid, fname)

    # Items matched on medline.com teach which category each FDA product code / device type belongs to
    known = {r[0]: r for r in archive or []}
    known.update({r[0]: r for r in g['rows']})
    by_pc, by_gm = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    for r in known.values():
        ic = item_cat.get(r[0])
        if ic:
            by_pc[r[7]][(ic[0], ic[1])] += 1
            by_gm[r[6]][(ic[0], ic[1])] += 1

    def majority(cnt):
        tops = collections.Counter()
        for (t, s), n in cnt.items():
            tops[t] += n
        top = tops.most_common(1)[0][0]
        subs = [(n, s) for (t, s), n in cnt.items() if t == top and s]
        return top, (max(subs)[1] if subs else '')

    def classify(item, pc, gmdn):
        """-> (top, sub, product id, product name, source code)"""
        if item in item_cat:
            return item_cat[item] + ('m',)
        if pc and by_pc.get(pc):
            return majority(by_pc[pc]) + ('', '', 'p')
        if gmdn and by_gm.get(gmdn):
            return majority(by_gm[gmdn]) + ('', '', 'g')
        return UNMATCHED, gmdn, '', '', 'u'

    rows_in = [r for r in g['rows'] if r[2] == 'D' or r[1] >= cutoff]
    rows = []
    for r in rows_in:
        item, pub, st, end, brand, desc, gmdn, pc, pcn, cnt, di, sterile, rx = r
        top, sub, fid, fname, code = classify(item, pc, gmdn)
        if code == 'u':
            top = 'Not yet on medline.com'
        cs = countries(pc)
        rows.append({'item': item, 'pub': pub, 'status': st, 'end': end, 'brand': brand,
                     'desc': desc.replace('""', '"'), 'gmdn': gmdn, 'pc': pc, 'pcn': pcn, 'units': cnt,
                     'di': di, 'sterile': sterile, 'rx': rx, 'top': top, 'sub': sub, 'fid': fid,
                     'fname': fname, 'catSrc': {'u': 'not listed'}.get(code, SRC[code]), 'coo': sorted({x['cc'] for x in cs}),
                     'sites': [f"{x['site']} ({x['cc']}{', contract/partner' if x['cm'] else ''})" for x in cs][:8]})

    # Every known Medline item #, one row per item:
    # [item, description, brand, status N/D, publish date, left-distribution date, FDA product code, units,
    #  category, subcategory, medline.com product id, category source code]
    allrows, pcn_map = {}, {}
    for r in archive or []:
        item, pub, st, end, brand, desc, gmdn, pc, pcn, cnt, di, sterile, rx = r
        if not item or (item in allrows and allrows[item][4] >= pub):
            continue
        top, sub, fid, fname, code = classify(item, pc, gmdn)
        allrows[item] = [item, desc.replace('""', '"'), brand.strip(), st, pub, end, pc, cnt, top, sub, fid, code]
        if pc:
            pcn_map[pc] = pcn
    for d in drugs or []:
        text = f"{d['brand']} {d['generic']}"
        top = 'Skin Care' if TOPICAL.search(d['form']) or re.search(r'skin|sunscreen|barrier|antiperspirant|sanitiz', text, re.I) else 'Pharmacy'
        if top not in top_names:
            top = UNMATCHED
        exp = iso(d['exp'])
        desc = ' · '.join(x for x in (d['brand'], d['generic'].lower(), d['form'].lower()) if x)
        allrows['NDC ' + d['ndc']] = ['NDC ' + d['ndc'], desc, d['brand'], 'D' if exp and exp < as_of else 'N',
                                      iso(d['start']), exp if exp and exp < as_of else '', '', '; '.join(d['pkgs'][:3]),
                                      top, 'OTC drug: ' + d['form'].title(), '', 'd']
    # Manufacturing sites per FDA product code, shared by all items with that code: [[country, site, contract 0/1]]
    sites = {pc: [[x['cc'], x['site'], 1 if x['cm'] else 0] for x in countries(pc)] for pc in pcn_map}

    return {'asOf': as_of, 'sources': SOURCES, 'skus': rows, 'portfolio': portfolio, 'deep': deep or {},
            'all': list(allrows.values()), 'pcn': pcn_map, 'sites': {k: v for k, v in sites.items() if v},
            'srcNames': SRC, 'unmatched': UNMATCHED}
