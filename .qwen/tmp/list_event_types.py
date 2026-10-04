import glob, re, collections

fams = collections.Counter()
events = set()
for f in glob.glob('core/**/*.py', recursive=True):
    if 'migrations' in f:
        continue
    s = open(f, encoding='utf-8').read()
    for m in re.finditer(r'''event_type\s*[=:]\s*["']([\w.]+)["']''', s):
        events.add(m.group(1))
    for m in re.finditer(r'''event_type=["']\{([^}]*)\}["']|f["'][{"']''', s):
        pass
for e in sorted(events):
    print(e)
print('---- prefixes ----')
for p, n in sorted(fams.items()):
    print(p, n)
