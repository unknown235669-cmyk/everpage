"""Portable smoke test: no network, no local fixtures.

Run from the repo root:  python smoke_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from everpage import chunks, detect, rewrite
from everpage.fetcher import local_path_for


def main():
    rewrite.self_test()
    print("rewrite.self_test: ok")

    html = (
        "<html><head>"
        '<script src="/_next/static/chunks/1234.abcd1234.js"></script>'
        '<link rel="stylesheet" href="/_next/static/css/app.css">'
        "</head><body><a href=\"/about\">x</a></body></html>"
    )
    print("framework:", detect.detect_framework(html))
    out, missing = rewrite.rewrite_page(html, 0, {"/about": "about.html"}, "generic")
    assert '"/_next/' not in out, "root-absolute _next refs must be rewritten"
    assert "./_next/static/chunks/1234.abcd1234.js" in out
    print("rewrite_page: ok, len", len(out))
    out_nx, _m = rewrite.rewrite_page(html, 0, {"/about": "about.html"}, "next-webpack")
    assert "./_next/static/chunks/1234.abcd1234.js" in out_nx
    print("rewrite_page next-webpack: ok")

    bundle = (
        'geometryLoader.load(`planets/${terrain}/full_${i}.drc`);'
        'new Worker("https://example.com/assets/w-abc123.js");'
        'modelFiles:["avatar/avatar"];'
    )
    refs = chunks.discover(bundle, "generic")
    assert any("w-abc123.js" in r for r in refs), "absolute worker URL must be discovered"
    assert any(isinstance(r, dict) and r.get("type") == "template" for r in refs), \
        "template spec expected"
    assert any(isinstance(r, dict) and r.get("type") == "ids" for r in refs), \
        "ids pool expected"
    print("discover: ok,", len(refs), "refs/specs")

    assert local_path_for("OUT", "https://x.co/?p=1").endswith("index__p_1.html")
    assert local_path_for("OUT", "https://x.co/").endswith("index.html")
    assert local_path_for("OUT", "https://x.co/a.ttf?5.49.0").endswith("a.ttf")
    assert local_path_for("OUT", "https://x.co/a.js?ver=1").endswith("a.js")
    assert local_path_for("OUT", "https://x.co/n/?page=2").endswith(
        os.path.join("n", "index__page_2.html"))
    print("local_path_for: ok (query pages self-name, asset queries strip)")

    print("SMOKE_DONE")


if __name__ == "__main__":
    main()
