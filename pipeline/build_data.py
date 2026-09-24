"""Turn the three raw pulls into the site's data object.
gudid   {asOf, cols, rows}: Medline GUDID records (new publishes in window + not-in-distribution)
catalog {tops, subs, fams, itemCat}: medline.com Medline-brand products and item -> category lookups
coo     {productCode: ["CC|Site name|CM"]}: FDA-listed manufacturing sites
"""
import collections, datetime

SOURCES = {
    'launches': 'FDA Global Unique Device Identification Database (AccessGUDID) daily/weekly/monthly release files: Medline Industries device records by publish date.',
    'categories': "medline.com catalog search (Medline-brand filter): Medline's own category tree.",
    'origin': 'FDA Establishment Registration & Device Listing (openFDA): Medline-owned and contract manufacturing sites registered for each FDA product code.',
}


def build(g, c, coo, as_of, window_days=90, deep=None, archive=None):
    cutoff = (datetime.date.fromisoformat(as_of) - datetime.timedelta(days=window_days)).isoformat()
    fam_loc, portfolio = {}, []
    for nm, cid in c['tops']:
        fams = c['fams'].get(cid, [])
        portfolio.append({'id': cid, 'name': nm,
                          'subs': [[s[1], s[2]] for s in c['subs'].get(cid, [])],
                          'fams': [[f[0], f[1], (f[2].split(' | ')[0] if f[2] else '')] for f in fams]})
        for fid, fname, sub in fams:
            fam_loc.setdefault(fid, (nm, sub.split(' | ')[0] if sub else '', fname))

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

    rows_in = [r for r in g['rows'] if r[2] == 'D' or r[1] >= cutoff]
    by_pc, by_pc_sub = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    for r in rows_in:
        if r[0] in item_cat:
            by_pc[r[7]][item_cat[r[0]][0]] += 1
            if item_cat[r[0]][1]:
                by_pc_sub[r[7]][item_cat[r[0]][1]] += 1

    rows = []
    for r in rows_in:
        item, pub, st, end, brand, desc, gmdn, pc, pcn, cnt, di, sterile, rx = r
        if item in item_cat:
            top, sub, fid, fname = item_cat[item]; src = 'medline.com'
        elif by_pc.get(pc):
            top = by_pc[pc].most_common(1)[0][0]
            sub = by_pc_sub[pc].most_common(1)[0][0] if by_pc_sub.get(pc) else ''
            fid = fname = ''; src = 'inferred (FDA product code)'
        else:
            top, sub, fid, fname, src = 'Not yet on medline.com', gmdn, '', '', 'not listed'
        cs = countries(pc)
        rows.append({'item': item, 'pub': pub, 'status': st, 'end': end, 'brand': brand,
                     'desc': desc.replace('""', '"'), 'gmdn': gmdn, 'pc': pc, 'pcn': pcn, 'units': cnt,
                     'di': di, 'sterile': sterile, 'rx': rx, 'top': top, 'sub': sub, 'fid': fid,
                     'fname': fname, 'catSrc': src, 'coo': sorted({x['cc'] for x in cs}),
                     'sites': [f"{x['site']} ({x['cc']}{', contract/partner' if x['cm'] else ''})" for x in cs][:8]})

    # Item #s under each medline.com product: every archived GUDID record whose item # was matched to a product.
    # One row per item #: [item, description, status N/D, publish date, left-distribution date, units, [countries]]
    by_prod = {}
    for r in archive or []:
        item, pub, st, end, brand, desc, gmdn, pc, pcn, cnt, di, sterile, rx = r
        ic = item_cat.get(item)
        if not ic or not ic[2]:
            continue
        prod = by_prod.setdefault(ic[2], {})
        prev = prod.get(item)
        if prev is None or pub > prev[3]:
            prod[item] = [item, desc.replace('""', '"'), st, pub, end, cnt, sorted({x['cc'] for x in countries(pc)})]
    items = {fid: sorted(v.values()) for fid, v in by_prod.items()}

    return {'asOf': as_of, 'sources': SOURCES, 'skus': rows, 'portfolio': portfolio, 'deep': deep or {},
            'items': items}
