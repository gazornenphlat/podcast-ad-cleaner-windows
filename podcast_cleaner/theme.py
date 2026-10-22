"""Warm Cream Editorial style for the Tk window.

A cream-paper page with white cards on a hairline border; one warm honey-amber accent; headings in
DM Serif Display, everything else in Instrument Sans; section labels in wide-tracked capitals.
(Both fonts ship in podcast_cleaner/fonts under the SIL Open Font License and are loaded privately -
nothing is installed on the PC. Georgia / Segoe UI are used if they can't be loaded.)

Tk has no rounded corners, so the rounded parts (pill buttons, cards, the progress bar, the tab
indicator) are drawn with Pillow at 4x size, scaled down for smooth edges, and used as 9-slice images.
Widgets inside a card get a white "Card." variant of their style (see cardify()), so the card reads as
one white sheet on the cream page.
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

# ---- colour roles (names kept from the earlier theme so the rest of the app needn't change)
PRIMARY = "#ECAE36"              # honey amber - the one accent
ON_PRIMARY = "#292524"
PRIMARY_HOVER = "#F0BA52"
PRIMARY_PRESSED = "#D99A22"
PRIMARY_TEXT = "#A86B0C"         # amber dark enough to read as text on white / cream
PRIMARY_CONTAINER = "#FEF3C7"    # butter tint: selection, highlight cards
ON_PRIMARY_CONTAINER = "#451A03"
SECONDARY_CONTAINER = "#EFEBE6"  # warm taupe: secondary buttons
SECONDARY_HOVER = "#E7E2DB"
SECONDARY_PRESSED = "#DED7CE"
ON_SECONDARY_CONTAINER = "#44403C"
SURFACE = "#F9F7F2"              # cream paper (the page)
SURFACE_LOWEST = "#FFFFFF"       # cards
SURFACE_CONTAINER = "#F5F5F4"
SURFACE_HIGH = "#EFEBE6"
ON_SURFACE = "#44403C"           # warm graphite - never pure black
ON_SURFACE_VARIANT = "#78716C"
MUTED = "#78716C"
OUTLINE = "#E7E5E4"              # hairline
OUTLINE_STRONG = "#A8A29E"
DISABLED_BG = "#F0EDE8"
DISABLED_FG = "#B8B2AC"
ERROR = "#DC2626"
SUCCESS = "#2A9D8F"
WARNING = "#B45309"
CHART = ("#ECAE36", "#F4A261", "#2A9D8F", "#457B9D", "#E76F51")

STATE_COLORS = {"idle": SUCCESS, "busy": PRIMARY, "paused": OUTLINE_STRONG, "error": ERROR}

SERIF_FAMILIES = ("DM Serif Display", "Georgia", "Times New Roman", "DejaVu Serif")
SANS_FAMILIES = ("Instrument Sans", "Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans")

_SCALE = 4
_keep: list = []   # Tk forgets images that Python doesn't hold on to
_FONT_DIR = Path(__file__).with_name("fonts")


def load_fonts() -> None:
    """Make the bundled fonts available to this app only (Windows: private to the process)."""
    if sys.platform != "win32" or not _FONT_DIR.is_dir():
        return
    try:
        import ctypes
        FR_PRIVATE = 0x10
        for f in _FONT_DIR.glob("*.ttf"):
            ctypes.windll.gdi32.AddFontResourceExW(str(f), FR_PRIVATE, 0)
    except Exception:  # noqa: BLE001 - the fallback fonts are fine
        pass


def tracked(text: str) -> str:
    """Wide-tracked capitals (Tk has no letter-spacing): 'Up next' -> 'U P  N E X T' with thin spaces."""
    words = str(text).strip().upper().split()
    return " ".join(" ".join(w) for w in words)


def tab_text(title: str) -> str:
    return f"  {tracked(title)}  "


def _rounded(size: tuple[int, int], radius: int, fill: str, outline: str | None = None, width: int = 1,
             bar: tuple[str, int] | None = None, shadow: bool = False) -> Image.Image:
    w, h = size
    s = _SCALE
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if shadow:       # a whisper of shadow under cards
        d.rounded_rectangle((0, 1.2 * s, w * s - 1, h * s - 1), radius=radius * s, fill=(0, 0, 0, 12))
    if radius or fill != "":
        d.rounded_rectangle((0, 0, w * s - 1, (h - (1 if shadow else 0)) * s - 1), radius=radius * s, fill=fill,
                            outline=outline, width=width * s if outline else 0)
    if bar:          # tab indicator: a short pill under the label
        colour, thick = bar
        d.rounded_rectangle((w * s * 0.22, (h - thick) * s, w * s * 0.78 - 1, h * s - 1),
                            radius=thick * s, fill=colour)
    return img.resize((w, h), Image.LANCZOS)


def _check(on: bool, radio: bool, colour: str, size: int = 18, mark: str = ON_PRIMARY) -> Image.Image:
    s = _SCALE
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 1.5 * s
    box = (pad, pad, n - pad, n - pad)
    if radio:
        d.ellipse(box, outline=colour, width=int(1.6 * s))
        if on:
            c = n / 2
            d.ellipse((c - 4.5 * s, c - 4.5 * s, c + 4.5 * s, c + 4.5 * s), fill=colour)
    elif on:
        d.rounded_rectangle(box, radius=5 * s, fill=colour)
        d.line([(n * 0.28, n * 0.52), (n * 0.44, n * 0.67), (n * 0.73, n * 0.35)], fill=mark,
               width=int(2.1 * s), joint="curve")
    else:
        d.rounded_rectangle(box, radius=5 * s, outline=colour, width=int(1.6 * s))
    small = img.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size + 8, size), (0, 0, 0, 0))   # room before the text
    out.paste(small, (0, 0))
    return out


def _photo(root, img: Image.Image) -> ImageTk.PhotoImage:
    p = ImageTk.PhotoImage(img, master=root)
    _keep.append(p)
    return p


def _first(root, names) -> str:
    families = set(tkfont.families(root))
    return next((f for f in names if f in families), "TkDefaultFont")


def pick_font(root) -> str:
    return _first(root, SANS_FAMILIES)


def pick_serif(root) -> str:
    return _first(root, SERIF_FAMILIES)


# every style that may sit inside a card, so it gets a white "Card." twin
_CARD_STYLES = ("TFrame", "TLabel", "Muted.TLabel", "Big.TLabel", "Title.TLabel", "Kicker.TLabel", "TCheckbutton",
                "TRadiobutton", "TButton", "Accent.TButton", "TPanedwindow", "TMenubutton", "TSeparator",
                "TLabelframe", "TEntry", "TCombobox", "TSpinbox")


def apply(root: tk.Tk) -> dict:
    """Style every ttk widget, plus the plain Tk ones (Text, Listbox, menus). Returns fonts."""
    load_fonts()
    st = ttk.Style(root)
    st.theme_use("clam")
    fam, serif = pick_font(root), pick_serif(root)
    body = (fam, 10)
    semibold = (fam, 10, "bold")
    fonts = {"family": fam, "serif": serif, "body": body, "small": (fam, 9), "label": (fam, 8, "bold"),
             "kicker": (fam, 8, "bold"), "title": (serif, 19), "big": (serif, 15), "headline": (serif, 22),
             "semibold": semibold,
             "mono": ("Cascadia Mono", 9) if sys.platform == "win32" else ("DejaVu Sans Mono", 9)}
    root.configure(background=SURFACE)

    # plain Tk widgets (dialogs, text boxes, dropdown lists)
    # Tk's named default fonts (an option-database "*Font" would override the ttk styles' fonts)
    for name, spec in (("TkDefaultFont", body), ("TkTextFont", body), ("TkMenuFont", body),
                       ("TkHeadingFont", semibold), ("TkCaptionFont", semibold), ("TkSmallCaptionFont", fonts["small"]),
                       ("TkTooltipFont", fonts["small"]), ("TkIconFont", body)):
        try:
            tkfont.nametofont(name, root=root).configure(family=spec[0], size=spec[1],
                                                         weight=spec[2] if len(spec) > 2 else "normal")
        except tk.TclError:
            pass
    for pattern, value in {
        "*Toplevel.background": SURFACE,
        "*Text.background": SURFACE_LOWEST, "*Text.foreground": ON_SURFACE, "*Text.relief": "flat",
        "*Text.borderWidth": 0, "*Text.highlightThickness": 1, "*Text.highlightBackground": OUTLINE,
        "*Text.highlightColor": PRIMARY, "*Text.padX": 12, "*Text.padY": 10,
        "*Text.selectBackground": PRIMARY_CONTAINER, "*Text.selectForeground": ON_PRIMARY_CONTAINER,
        "*Text.insertBackground": ON_SURFACE,
        "*Listbox.background": SURFACE_LOWEST, "*Listbox.foreground": ON_SURFACE, "*Listbox.relief": "flat",
        "*Listbox.highlightThickness": 1, "*Listbox.highlightBackground": OUTLINE,
        "*Listbox.selectBackground": PRIMARY_CONTAINER, "*Listbox.selectForeground": ON_PRIMARY_CONTAINER,
        "*TCombobox*Listbox.background": SURFACE_LOWEST, "*TCombobox*Listbox.font": body,
        "*TCombobox*Listbox.selectBackground": PRIMARY_CONTAINER,
        "*TCombobox*Listbox.selectForeground": ON_PRIMARY_CONTAINER,
        "*Menu.background": SURFACE_LOWEST, "*Menu.foreground": ON_SURFACE, "*Menu.relief": "flat",
        "*Menu.activeBackground": PRIMARY_CONTAINER, "*Menu.activeForeground": ON_PRIMARY_CONTAINER,
        "*Menu.activeBorderWidth": 0, "*Menu.borderWidth": 1,
    }.items():
        root.option_add(pattern, value)

    st.configure(".", background=SURFACE, foreground=ON_SURFACE, font=body, bordercolor=OUTLINE,
                 lightcolor=SURFACE, darkcolor=SURFACE, troughcolor=SURFACE_CONTAINER,
                 focuscolor=PRIMARY, selectbackground=PRIMARY_CONTAINER, selectforeground=ON_PRIMARY_CONTAINER,
                 insertcolor=ON_SURFACE)
    st.map(".", foreground=[("disabled", DISABLED_FG)])

    # ---- labels: serif for headlines and named values, tracked capitals for kickers
    st.configure("TLabel", background=SURFACE, foreground=ON_SURFACE)
    st.configure("Title.TLabel", font=fonts["title"])
    st.configure("Big.TLabel", font=fonts["big"])
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Kicker.TLabel", foreground=MUTED, font=fonts["kicker"])

    # ---- buttons: soft taupe pill (default) and honey-amber pill (Accent)
    size, r = (36, 36), 17

    def button_style(name, base, hover, pressed, fg, focus_ring, shadow=False):
        imgs = {k: _photo(root, _rounded(size, r, c)) for k, c in
                (("n", base), ("h", hover), ("p", pressed), ("d", DISABLED_BG))}
        imgs["f"] = _photo(root, _rounded(size, r, base, focus_ring, 2))
        elem = f"{name}.pill"
        st.element_create(elem, "image", imgs["n"], ("disabled", imgs["d"]), ("pressed", imgs["p"]),
                          ("active", imgs["h"]), ("focus", imgs["f"]), border=r, padding=0, sticky="nsew")
        st.layout(name, [(elem, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        st.configure(name, padding=(14, 7), anchor="center", foreground=fg, background=SURFACE,
                     font=semibold, borderwidth=0)
        st.map(name, foreground=[("disabled", DISABLED_FG)], background=[("active", SURFACE)])

    button_style("TButton", SECONDARY_CONTAINER, SECONDARY_HOVER, SECONDARY_PRESSED, ON_SECONDARY_CONTAINER,
                 PRIMARY)
    button_style("Accent.TButton", PRIMARY, PRIMARY_HOVER, PRIMARY_PRESSED, ON_PRIMARY, ON_PRIMARY_CONTAINER)
    st.configure("TMenubutton", background=SECONDARY_CONTAINER, foreground=ON_SURFACE, padding=(14, 7),
                 arrowcolor=ON_SURFACE_VARIANT, relief="flat", borderwidth=0, font=semibold)
    st.map("TMenubutton", background=[("active", SECONDARY_HOVER)])

    # ---- cards (every LabelFrame): white, hairline border, generous radius; kicker label above
    card = _photo(root, _rounded((56, 56), 20, SURFACE_LOWEST, OUTLINE, 1, shadow=True))
    st.element_create("Card.border", "image", card, border=21, padding=10, sticky="nsew")
    st.layout("TLabelframe", [("Card.border", {"sticky": "nsew"})])
    st.configure("TLabelframe", background=SURFACE, labeloutside=True, labelmargins=(6, 2, 0, 8), padding=8)
    st.configure("TLabelframe.Label", background=SURFACE, foreground=MUTED, font=fonts["kicker"])

    # ---- tabs: tracked capitals, a short amber pill under the selected one
    tab_n = _photo(root, _rounded((40, 38), 0, SURFACE))
    tab_h = _photo(root, _rounded((40, 38), 10, SECONDARY_CONTAINER))
    tab_s = _photo(root, _rounded((40, 38), 0, SURFACE, bar=(PRIMARY, 3)))
    st.element_create("Tab.indicator", "image", tab_n, ("selected", tab_s), ("active", tab_h),
                      border=(10, 6, 10, 6), padding=0, sticky="nsew")
    st.layout("TNotebook.Tab", [("Tab.indicator", {"sticky": "nsew", "children": [
        ("Notebook.padding", {"sticky": "nsew", "children": [("Notebook.label", {"sticky": ""})]})]})])
    st.configure("TNotebook", background=SURFACE, borderwidth=0, tabmargins=(8, 4, 8, 0))
    st.configure("TNotebook.Tab", padding=(8, 10), foreground=MUTED, font=fonts["kicker"], background=SURFACE)
    st.map("TNotebook.Tab", foreground=[("selected", ON_SURFACE), ("active", ON_SURFACE)])
    st.layout("TNotebook", [("Notebook.client", {"sticky": "nsew"})])

    # ---- lists: borderless white rows, tracked caps headings, butter selection
    st.configure("Treeview", background=SURFACE_LOWEST, fieldbackground=SURFACE_LOWEST, foreground=ON_SURFACE,
                 rowheight=32, borderwidth=0, bordercolor=SURFACE_LOWEST, lightcolor=SURFACE_LOWEST,
                 darkcolor=SURFACE_LOWEST, font=body)
    st.map("Treeview", background=[("selected", PRIMARY_CONTAINER)],
           foreground=[("selected", ON_PRIMARY_CONTAINER)])
    st.configure("Treeview.Heading", background=SURFACE_LOWEST, foreground=MUTED, font=fonts["label"],
                 relief="flat", borderwidth=0, padding=(8, 8), bordercolor=OUTLINE, lightcolor=SURFACE_LOWEST,
                 darkcolor=OUTLINE)
    st.map("Treeview.Heading", background=[("active", SURFACE_CONTAINER)])

    # ---- text fields: white, hairline, amber focus ring
    field = dict(fieldbackground=SURFACE_LOWEST, background=SURFACE_LOWEST, foreground=ON_SURFACE,
                 bordercolor=OUTLINE, lightcolor=SURFACE_LOWEST, darkcolor=SURFACE_LOWEST,
                 padding=(8, 6), arrowcolor=ON_SURFACE_VARIANT, insertcolor=ON_SURFACE)
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        st.configure(name, **field)
        st.map(name, bordercolor=[("focus", PRIMARY)], lightcolor=[("focus", SURFACE_LOWEST)],
               darkcolor=[("focus", SURFACE_LOWEST)], fieldbackground=[("readonly", SURFACE_LOWEST),
                                                                       ("disabled", DISABLED_BG)],
               background=[("active", SURFACE_LOWEST), ("pressed", SURFACE_LOWEST)],
               selectbackground=[("readonly", SURFACE_LOWEST), ("!readonly", PRIMARY_CONTAINER)],
               selectforeground=[("readonly", ON_SURFACE), ("!readonly", ON_PRIMARY_CONTAINER)],
               foreground=[("disabled", DISABLED_FG)])
    st.configure("TCombobox", padding=(8, 6, 4, 6))

    # ---- checkboxes and radio buttons: amber when on
    for name, radio in (("TCheckbutton", False), ("TRadiobutton", True)):
        off = _photo(root, _check(False, radio, OUTLINE_STRONG))
        on = _photo(root, _check(True, radio, PRIMARY))
        off_d = _photo(root, _check(False, radio, DISABLED_FG))
        on_d = _photo(root, _check(True, radio, DISABLED_FG, mark="#FFFFFF"))
        elem = f"W.{name}.indicator"
        st.element_create(elem, "image", off, ("disabled", "selected", on_d), ("disabled", off_d),
                          ("selected", on), sticky="w", padding=0)
        st.layout(name, [(f"{name.split('T', 1)[1]}.padding", {"sticky": "nswe", "children": [
            (elem, {"side": "left", "sticky": ""}),
            (f"{name.split('T', 1)[1]}.label", {"side": "left", "sticky": "nswe"})]})])
        st.configure(name, background=SURFACE, foreground=ON_SURFACE, padding=(2, 3, 2, 3))
        st.map(name, background=[("active", SURFACE)])

    # ---- progress bar: 8 px amber pill on a stone track
    trough = _photo(root, _rounded((24, 8), 4, SURFACE_CONTAINER))
    fill = _photo(root, _rounded((24, 8), 4, PRIMARY))
    st.element_create("Amber.trough", "image", trough, border=4, sticky="ew")
    st.element_create("Amber.pbar", "image", fill, border=4, sticky="nsw")
    st.layout("Horizontal.TProgressbar", [("Amber.trough", {"sticky": "nsew", "children": [
        ("Amber.pbar", {"side": "left", "sticky": "ns"})]})])
    st.configure("Horizontal.TProgressbar", thickness=8, background=SURFACE, borderwidth=0)

    # ---- scrollbars, dividers, panes
    for orient in ("Vertical", "Horizontal"):
        st.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {"sticky": "nsew", "children": [
            (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nsew"})]})])
        st.configure(f"{orient}.TScrollbar", troughcolor=SURFACE_LOWEST, background=SECONDARY_CONTAINER,
                     bordercolor=SURFACE_LOWEST, lightcolor=SECONDARY_CONTAINER, darkcolor=SECONDARY_CONTAINER,
                     gripcount=0, arrowsize=8, width=8)
        st.map(f"{orient}.TScrollbar", background=[("active", OUTLINE_STRONG)],
               lightcolor=[("active", OUTLINE_STRONG)], darkcolor=[("active", OUTLINE_STRONG)])
    st.configure("TSeparator", background=OUTLINE)
    st.configure("TPanedwindow", background=SURFACE)
    st.configure("Sash", sashthickness=12, gripcount=0, background=SURFACE, lightcolor=SURFACE,
                 bordercolor=SURFACE)

    # ---- white twins for everything that sits inside a card
    for name in _CARD_STYLES:
        st.configure(f"Card.{name}", background=SURFACE_LOWEST)
        if name in ("TCheckbutton", "TRadiobutton", "TButton", "Accent.TButton"):
            st.map(f"Card.{name}", background=[("active", SURFACE_LOWEST)])
    st.configure("Card.TLabelframe.Label", background=SURFACE_LOWEST)
    st.configure("Sash", background=SURFACE)
    return fonts


# ---------- put widgets inside cards on white ----------
_done: set = set()


def _in_card(w) -> bool:
    p = w.master
    while p is not None:
        if isinstance(p, ttk.Labelframe):
            return True
        if isinstance(p, (tk.Toplevel, tk.Tk)) or isinstance(p, ttk.Notebook):
            return False
        p = p.master
    return False


def cardify(root) -> None:
    """Give every widget inside a card (LabelFrame) the white 'Card.' version of its style, turn card
    titles into tracked capitals, and paint plain Tk text boxes and canvases in cards white. Cheap to
    call again: widgets already done are skipped (titles are re-checked, as some change)."""
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, ttk.Labelframe):
            t = str(w.cget("text"))
            if t.strip() and any(c.islower() for c in t):
                w.configure(text=tracked(t))
        key = str(w)
        if key in _done:
            continue
        _done.add(key)
        if not _in_card(w):
            continue
        if isinstance(w, (tk.Text, tk.Canvas, tk.Listbox)):
            try:
                if str(w.cget("background")) in (SURFACE, "SystemButtonFace"):
                    w.configure(background=SURFACE_LOWEST)
            except tk.TclError:
                pass
            continue
        if not isinstance(w, ttk.Widget) or isinstance(w, (ttk.Treeview, ttk.Scrollbar, ttk.Progressbar,
                                                           ttk.Notebook)):
            continue
        try:
            style = str(w.cget("style")) or w.winfo_class()
        except tk.TclError:
            continue
        if style.startswith("Card.") or style.split(".", 1)[-1] not in {s.split(".")[-1] for s in _CARD_STYLES}:
            continue
        if style in _CARD_STYLES:
            try:
                w.configure(style=f"Card.{style}")
            except tk.TclError:
                pass
