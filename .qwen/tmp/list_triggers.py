import glob, re

for f in sorted(glob.glob('core/migrations/*.py')):
    s = open(f, encoding='utf-8').read()
    for m in re.finditer(r"CREATE\s+TRIGGER\s+`?(\w+)`?\s+(\w+)\s+(\w+)\s+ON\s+`?([\w]+)`?", s, re.I):
        print("%-52s %-30s %-7s %-7s -> %s" % (f.split('\\')[-1], m.group(1), m.group(2), m.group(3), m.group(4)))
