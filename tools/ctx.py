# Print regex matches in a big minified file with surrounding context.
# Usage: ctx.py FILE REGEX [context_chars=200] [max_matches=10]
import sys,re
path,pat=sys.argv[1],sys.argv[2]
w=int(sys.argv[3]) if len(sys.argv)>3 else 200
n=int(sys.argv[4]) if len(sys.argv)>4 else 10
s=open(path,encoding='utf-8',errors='replace').read()
for i,m in enumerate(re.finditer(pat,s)):
    if i>=n: break
    print(f"--- @{m.start()}\n"+s[max(0,m.start()-w):m.end()+w].replace('\n',' '))
