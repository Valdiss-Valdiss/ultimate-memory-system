# -*- coding: utf-8 -*-
"""Замер качества поиска на своём эталоне: nDCG@10, P@10 и разница с базовым вариантом (95% интервал).

  python eval_search.py grades.json runs.json [--base <имя варианта>]

grades.json — разметка пула:  {"тема": {"путь/заметки.md": 2, "другая.md": 1, ...}, ...}
              (2 — прямо по теме, 1 — частично; неоценённые = 0)
runs.json   — выдача вариантов: {"вариант": {"тема": ["путь1.md", "путь2.md", ...], ...}, ...}
Темы без единой оценки ≥1 пропускаются (по ним нечего мерить).
"""
import json
import math
import sys


def ndcg(grades, ranked, k=10):
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    if not idcg:
        return None
    return sum((2 ** grades.get(p, 0) - 1) / math.log2(i + 2) for i, p in enumerate(ranked[:k])) / idcg


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    G = json.load(open(sys.argv[1], encoding="utf-8"))
    R = json.load(open(sys.argv[2], encoding="utf-8"))
    base = sys.argv[sys.argv.index("--base") + 1] if "--base" in sys.argv else next(iter(R))
    topics = [t for t in G if any(v >= 1 for v in G[t].values())]
    res = {}
    for name, runs in R.items():
        n, p = [], []
        for t in topics:
            ranked = runs.get(t, [])
            n.append(ndcg(G[t], ranked))
            p.append(sum(1 for x in ranked[:10] if G[t].get(x, 0) >= 1) / 10)
        res[name] = n
        print(f"{name:30} nDCG@10 {sum(n) / len(n):.3f}   P@10 {sum(p) / len(p):.3f}   (тем {len(n)})")
    print(f"\nРазница с «{base}» (95% интервал; значимо, если интервал не пересекает 0):")
    for name in res:
        if name == base:
            continue
        d = [b - a for a, b in zip(res[base], res[name])]
        m = sum(d) / len(d)
        sd = (sum((x - m) ** 2 for x in d) / (len(d) - 1)) ** 0.5 if len(d) > 1 else 0
        ci = 1.96 * sd / len(d) ** 0.5
        print(f"  {name:28} {m:+.3f} ± {ci:.3f}   лучше {sum(x > 0.02 for x in d)}, хуже {sum(x < -0.02 for x in d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
