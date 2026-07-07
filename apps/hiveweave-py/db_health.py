import os
os.environ['NO_PROXY'] = 'localhost,127.0.0.1,0.0.0.0'
os.environ['no_proxy'] = 'localhost,127.0.0.1,0.0.0.0'
import urllib.request, json
url = "http://localhost:4000/api/chat/3ce8495d-7ebd-4395-ac71-91540703f009/messages?limit=10"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
r = opener.open(url, timeout=5)
d = json.loads(r.read().decode())
print("messages:", len(d))
team = [m for m in d if m.get('isBackground') in (True, 1) or m.get('is_background') in (True, 1)]
print("team:", len(team))
if team:
    m = team[0]
    print("\n=== sample team msg ===")
    for k, v in m.items():
        if k == 'content': v = v[:80]
        print(f"  {k}: {v}")
