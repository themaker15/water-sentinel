"""Static checks that keep the frontend consistent as it grows (no browser needed).

See docs/DESIGN.md for the rules these enforce."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "frontend" / "app"
DASH = ROOT / "frontend" / "dashboard"


def _app_js():
    return {p.name: p.read_text(encoding="utf-8") for p in APP.glob("*.js")}


def test_dashboard_tokens_match_app():
    """The public dashboard is deployed on its own, so it carries a copy of the tokens. Keep them identical."""
    assert (APP / "tokens.css").read_bytes() == (DASH / "tokens.css").read_bytes(), \
        "copy frontend/app/tokens.css to frontend/dashboard/tokens.css"


def test_css_uses_only_defined_tokens():
    defined = set(re.findall(r"(--[\w-]+)\s*:", (APP / "tokens.css").read_text(encoding="utf-8")))
    # variables set inline in markup (e.g. style="--at:.5" on the cylinder graduations)
    defined |= set(re.findall(r"(--[\w-]+)\s*:", (APP / "index.html").read_text(encoding="utf-8")))
    for css in (APP / "style.css", DASH / "index.html"):
        text = css.read_text(encoding="utf-8")
        local = set(re.findall(r"(--[\w-]+)\s*:", text))   # component-scoped variables, e.g. --state, --level
        used = set(re.findall(r"var\((--[\w-]+)", text))
        missing = sorted(used - defined - local)
        assert not missing, f"{css.name} uses undefined tokens: {missing}"


def test_js_has_no_hard_coded_colours():
    """Charts must read colours from tokens.css (C.ok, MODEL_COLORS.mspc, seriesColor(i)) so themes stay in sync."""
    for name, text in _app_js().items():
        hexes = re.findall(r"""["']#[0-9a-fA-F]{6}["']""", text)
        assert not hexes, f"{name} hard-codes colours {hexes[:3]}: use tokens"


def test_ui_text_is_english_and_emoji_free():
    thai = re.compile(r"[฀-๿]")
    emoji = re.compile(r"[\U0001F300-\U0001FAFF]")
    files = [*APP.glob("*.js"), APP / "index.html", DASH / "index.html", *ROOT.joinpath("sentinel").rglob("*.py")]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not thai.search(text), f"{f.relative_to(ROOT)} contains Thai text: UI and code are English"
        if f.suffix != ".py":   # phone push titles in app/hub.py may keep their emoji
            assert not emoji.search(text), f"{f.relative_to(ROOT)} contains emoji in the UI"


def test_index_references_existing_static_files():
    html = (APP / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="/static/([^"]+)"', html)
    assert refs, "no /static references found"
    for ref in refs:
        assert (APP / ref).is_file(), f"index.html points at missing /static/{ref}"
    for css in ("fonts.css",):
        for url in re.findall(r'url\("([^"]+)"\)', (APP / css).read_text(encoding="utf-8")):
            assert (APP / url).is_file(), f"{css} points at missing {url}"
    for url in re.findall(r'url\("([^"]+)"\)', (DASH / "fonts.css").read_text(encoding="utf-8")):
        assert (DASH / url).is_file(), f"dashboard/fonts.css points at missing {url}"
