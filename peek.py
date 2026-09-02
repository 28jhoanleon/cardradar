import json, re
import cards_data as cd

data = cd.api("matchDetails", matchId=5115967)
raw = json.dumps(data, ensure_ascii=False)

print("===== Menciones de 'yellow' en el JSON =====")
vistos = set()
for m in re.finditer(r'.{0,35}[Yy]ellow.{0,70}', raw):
    frag = m.group()
    if frag not in vistos:
        vistos.add(frag)
        print(frag)
        print("---")

print("\n===== Evento de tarjeta roja completo (valores reales) =====")
rc = cd.dig(data, "header", "events", "awayTeamRedCards")
print(json.dumps(rc, ensure_ascii=False, indent=2))

print("\n===== Existe content.matchFacts.events? =====")
ev = cd.dig(data, "content", "matchFacts", "events")
print("SI, tipo:", type(ev).__name__ if ev is not None else "NO existe")
if ev:
    print(json.dumps(ev, ensure_ascii=False, indent=2)[:2500])
