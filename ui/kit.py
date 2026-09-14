"""Shared look and behaviour for every page.

Three jobs:

  * one visual language — header, chips, spacing — so the five pages read as one
    tool rather than five forms;
  * a **completeness view**, because the useful question on an estimate is not
    "did it run" but "what is still missing, and does that matter";
  * small helpers that keep pages working on both Streamlit 1.39 and 1.58.

The completeness weighting is an estimator's judgement, not a technical one: a
foundation BOQ without excavation or concrete is not an estimate, whereas one
without a sump may be perfectly complete for the drawing in hand.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import streamlit as st

from core.models import Project
from ui import auth

ASSETS = Path(__file__).resolve().parent.parent / "assets"
BRAND = "CIVIL ESTIMATOR"
BRAND_GOLD = "#D59E0E"
BRAND_NAVY = "#1F4E78"


@lru_cache(maxsize=8)
def logo_data_uri(name: str = "gi_logo_header.png") -> str:
    """Logo as a data URI.

    Inlined because `st.markdown` cannot reference a local file, and the header
    has to be one HTML block for the sticky positioning to apply to it as a
    unit. Cached so the base64 work happens once per process, not once per
    rerun — Streamlit reruns the whole script on every widget interaction.
    """
    path = ASSETS / name
    if not path.exists():
        return ""
    return ("data:image/png;base64,"
            + base64.b64encode(path.read_bytes()).decode("ascii"))


def favicon():
    """Page icon for `st.set_page_config`, falling back to an emoji."""
    path = ASSETS / "gi_favicon.png"
    return str(path) if path.exists() else "🏗️"

# --- What a foundation BOQ needs, in the order an estimator thinks about it ---
CRITICAL = {
    "grade_slabs": "Grade slab",
    "pedestals": "Pedestals",
    "excavations": "Excavation",
    "pcc_blindings": "PCC / blinding",
}
EXPECTED = {
    "joints": "Joints",
    "curb_walls": "Curb wall",
    "hdpe_liners": "HDPE liner",
    "epoxy_coatings": "Epoxy coating",
    "compacted_soils": "Compacted fill",
}
SITUATIONAL = {
    "sumps": "Sump",
    "embedments": "Embedments",
    "rebar_bars": "Rebar (manual BBS)",
    "waterstop_runs": "Waterstop",
    "sump_ancillaries": "Sump ancillaries",
    "formwork_loose": "Loose formwork",
}
ALL_GROUPS = (("Critical", CRITICAL), ("Expected", EXPECTED),
              ("Situational", SITUATIONAL))

PLACEHOLDER_HEIGHT_M = 0.800


THEME_CSS = """
<style>
  :root {
      --ce-navy:#1F4E78; --ce-gold:#D59E0E; --ce-line:#E3E7EC;
      --ce-top:60px;              /* height of Streamlit's fixed toolbar */
  }

  /* Streamlit's own toolbar sits above everything; make it opaque so the
     sticky header does not show through it while scrolling. */
  header[data-testid="stHeader"] { background:#FFFFFF; }

  /* ---- Brand header ---- */
  .ce-sticky { background:#FFFFFF; padding-top:.15rem; }
  .ce-head {
      display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
      padding:.35rem 0 .5rem 0; border-bottom:3px solid var(--ce-navy);
  }
  .ce-logo { height:52px; width:auto; flex:none; }
  .ce-brandtext { display:flex; flex-direction:column; line-height:1.08; }
  .ce-title {
      font-size:2.15rem; font-weight:800; color:var(--ce-navy);
      letter-spacing:.045em; margin:0;
  }
  .ce-page {
      font-size:1.02rem; font-weight:600; color:var(--ce-gold);
      letter-spacing:.02em; margin-top:.12rem;
  }
  .ce-chips { display:flex; gap:.4rem; flex-wrap:wrap; margin-left:auto; }

  .ce-chip {
      background:#EEF3F9; color:var(--ce-navy); border:1px solid #C9D8EA;
      border-radius:999px; padding:.14rem .62rem; font-size:.78rem;
      font-weight:600; white-space:nowrap;
  }
  .ce-chip.warn { background:#FFF4E5; color:#8A5300; border-color:#F0D5AC; }
  .ce-chip.bad  { background:#FDECEC; color:#A11B1B; border-color:#F2C3C3; }
  .ce-chip.good { background:#E9F6EE; color:#1B6B3A; border-color:#BFE3CC; }

  .ce-steps {
      display:flex; gap:.4rem; flex-wrap:wrap;
      padding:.45rem 0 .5rem 0; border-bottom:1px solid var(--ce-line);
  }
  .ce-step {
      font-size:.76rem; padding:.2rem .6rem; border-radius:6px;
      background:#F3F5F8; color:#6B7280; border:1px solid var(--ce-line);
  }
  .ce-step.on { background:var(--ce-navy); color:#fff; border-color:var(--ce-navy); }
  .ce-step.done { background:#E9F6EE; color:#1B6B3A; border-color:#BFE3CC; }

  /* ---- Freeze the header + step bar ----
     Streamlit's own toolbar is `position:fixed` and 60 px tall, and the page
     scrolls inside section[data-testid="stMain"]. A sticky element therefore
     has to be offset by the toolbar height or it pins *underneath* it and
     disappears. `stElementContainer` is the wrapper Streamlit puts around each
     element (measured, not guessed — the older `element-container` test id no
     longer matches); the class selector is kept as a fallback for other
     versions, and a selector that matches nothing is harmless. */
  [data-testid="stElementContainer"]:has(.ce-sticky),
  .element-container:has(.ce-sticky) {
      position: sticky;
      top: var(--ce-top);
      z-index: 900;
      background:#FFFFFF;
      padding-bottom:.15rem;
      box-shadow: 0 8px 12px -10px rgba(31,78,120,.55);
  }

  /* ---- Sidebar ---- */
  section[data-testid="stSidebar"] div[data-testid="stMarkdownContainer"] p {
      margin-bottom:.25rem;
  }
  .ce-sidelogo { text-align:center; padding:.2rem 0 .6rem 0; }
  .ce-sidelogo img { width:82%; max-width:220px; height:auto; }
  /* st.logo renders at ~32 px, which is a favicon rather than a brand mark. */
  [data-testid="stLogo"] { height:46px !important; width:auto !important;
                           margin:.35rem 0 .2rem .25rem; }
  [data-testid="stSidebarNav"] a span { font-size:.95rem; }
  .ce-sidefoot {
      font-size:.72rem; color:#8A94A0; text-align:center;
      border-top:1px solid var(--ce-line); margin-top:.6rem; padding-top:.5rem;
  }

  div[data-testid="stMetricValue"] { font-size:1.35rem; }

  /* ---- Login ---- */
  .ce-login { text-align:center; padding:1.2rem 0 .2rem 0; }
  .ce-login img { width:300px; max-width:70%; height:auto; }
  .ce-login h1 {
      font-size:2.3rem; font-weight:800; color:var(--ce-navy);
      letter-spacing:.05em; margin:.9rem 0 .1rem 0;
  }
  .ce-login p { color:#6B7280; margin:0; }
</style>
"""

# "Project" used to lead this list and pointed at the entry script's setup
# form. That page is gone — the app opens on Drawing → BOQ — so a chip for
# it would be a step the user cannot take.
STEPS = ["Drawing → BOQ", "Input", "Review", "BOM", "Costing"]


def inject_theme() -> None:
    st.markdown(THEME_CSS, unsafe_allow_html=True)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def page_header(title: str, project: Project | None = None,
                subtitle: str = "", step: str | None = None) -> None:
    """Brand bar, page name and status chips — pinned to the top of the page.

    Header and step bar are emitted as a single markdown block so the sticky
    rule applies to them as one unit; splitting them would leave the step bar
    scrolling out from under a pinned header.
    """
    inject_theme()
    sidebar_logo()

    chips = []
    if project is not None:
        chips.append(f'<span class="ce-chip">'
                     f'{_esc(project.drawing_no or "no drawing no")}</span>')
        if project.revision:
            chips.append(f'<span class="ce-chip">Rev {_esc(project.revision)}</span>')
        n = element_count(project)
        chips.append(f'<span class="ce-chip {"good" if n else "warn"}">'
                     f'{n} element(s)</span>')
        if placeholder_heights(project):
            chips.append('<span class="ce-chip bad">placeholder heights</span>')

    logo = logo_data_uri()
    logo_html = (f'<img class="ce-logo" src="{logo}" alt="General Industries">'
                 if logo else "")
    steps_html = _steps_html(step) if step else ""

    st.markdown(
        f'<div class="ce-sticky">'
        f'  <div class="ce-head">'
        f'    {logo_html}'
        f'    <div class="ce-brandtext">'
        f'      <div class="ce-title">{BRAND}</div>'
        f'      <div class="ce-page">{_esc(title)}</div>'
        f'    </div>'
        f'    <div class="ce-chips">{"".join(chips)}</div>'
        f'  </div>'
        f'  {steps_html}'
        f'</div>',
        unsafe_allow_html=True)

    if subtitle:
        st.caption(subtitle)


def sidebar_logo() -> None:
    """Company mark at the very top of the sidebar, above the page nav.

    `st.logo` is the only API that places an image above Streamlit's generated
    navigation; the markdown fallback covers older builds by rendering inside
    the sidebar instead.
    """
    if st.session_state.get("_ce_logo_done"):
        return
    st.session_state["_ce_logo_done"] = True

    path = ASSETS / "gi_logo_sidebar.png"
    if not path.exists():
        return
    try:
        st.logo(str(path), size="large")
        return
    except (AttributeError, TypeError):
        pass
    try:
        st.logo(str(path))
        return
    except (AttributeError, TypeError):
        pass
    uri = logo_data_uri("gi_logo_sidebar.png")
    if uri:
        st.sidebar.markdown(
            f'<div class="ce-sidelogo"><img src="{uri}" '
            f'alt="General Industries"></div>', unsafe_allow_html=True)


def _steps_html(current: str) -> str:
    seen_current = False
    out = []
    for label in STEPS:
        if label == current:
            seen_current = True
            cls = "on"
        else:
            cls = "done" if not seen_current else ""
        out.append(f'<span class="ce-step {cls}">{_esc(label)}</span>')
    return f'<div class="ce-steps">{"".join(out)}</div>'


def render_steps(current: str) -> None:
    st.markdown(_steps_html(current), unsafe_allow_html=True)


# ---------- Completeness ----------
@dataclass
class Coverage:
    filled: dict[str, int] = field(default_factory=dict)
    missing_critical: list[str] = field(default_factory=list)
    missing_expected: list[str] = field(default_factory=list)
    score: float = 0.0
    blockers: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing_critical and not self.blockers


def element_count(project: Project) -> int:
    total = 0
    for group in (CRITICAL, EXPECTED, SITUATIONAL):
        for fieldname in group:
            total += len(getattr(project, fieldname, []) or [])
    return total


def placeholder_heights(project: Project) -> list[str]:
    """Pedestals still sitting at the extractor's placeholder height."""
    return [p.tag for p in project.pedestals
            if abs(p.height_m - PLACEHOLDER_HEIGHT_M) < 1e-9]


def assess(project: Project) -> Coverage:
    """What is present, what is missing, and whether it is safe to price."""
    cov = Coverage()
    for group in (CRITICAL, EXPECTED, SITUATIONAL):
        for fieldname, label in group.items():
            cov.filled[label] = len(getattr(project, fieldname, []) or [])

    cov.missing_critical = [l for f, l in CRITICAL.items()
                            if not getattr(project, f, [])]
    cov.missing_expected = [l for f, l in EXPECTED.items()
                            if not getattr(project, f, [])]

    got_c = len(CRITICAL) - len(cov.missing_critical)
    got_e = len(EXPECTED) - len(cov.missing_expected)
    cov.score = round(100 * (0.75 * got_c / len(CRITICAL)
                             + 0.25 * got_e / len(EXPECTED)))

    if not project.drawing_no:
        cov.blockers.append("No drawing number — the BOM page will refuse to run.")
    ph = placeholder_heights(project)
    if ph:
        cov.blockers.append(
            f"Pedestal height is still the {PLACEHOLDER_HEIGHT_M:.3f} m "
            f"placeholder for {', '.join(ph)} — concrete, formwork and rebar "
            f"for these are wrong until you enter the real heights.")
    return cov


def readiness_panel(project: Project, *, compact: bool = False) -> Coverage:
    """Render the completeness view and return it."""
    cov = assess(project)
    c1, c2, c3 = st.columns([1, 1, 2])
    c1.metric("Estimate readiness", f"{cov.score}%")
    c2.metric("Elements entered", element_count(project))
    with c3:
        if cov.blockers:
            for b in cov.blockers:
                st.error(b, icon="🚫")
        elif cov.missing_critical:
            st.warning("Missing: " + ", ".join(cov.missing_critical), icon="⚠️")
        else:
            st.success("Nothing critical missing.", icon="✅")

    if compact:
        return cov

    cols = st.columns(3)
    for col, (group_name, group) in zip(cols, ALL_GROUPS):
        with col:
            st.markdown(f"**{group_name}**")
            for fieldname, label in group.items():
                n = len(getattr(project, fieldname, []) or [])
                mark = "✅" if n else ("⛔" if group is CRITICAL else "—")
                st.markdown(f"{mark} {label}"
                            + (f" &nbsp;`{n}`" if n else ""),
                            unsafe_allow_html=True)
    return cov


def sidebar_summary(project: Project) -> None:
    with st.sidebar:
        st.markdown("### Current estimate")
        st.markdown(f"**{project.project_name or '(unnamed project)'}**")
        st.caption(f"{project.drawing_no or 'no drawing no'} "
                   f"· Rev {project.revision or '-'}")
        cov = assess(project)
        st.progress(cov.score / 100, text=f"Readiness {cov.score}%")
        for label, n in cov.filled.items():
            if n:
                st.markdown(f"- {label}: **{n}**")
        if cov.blockers:
            st.markdown("---")
            st.markdown("**Needs attention**")
            for b in cov.blockers:
                st.caption(f"• {b}")


# ---------- Version-safe widgets ----------
def image(target, data, **kwargs):
    from extractors.st_compat import image as _image
    return _image(target, data, **kwargs)


# ---------- Login ----------
def require_login() -> None:
    """Gate the page. Renders the login screen and stops if not signed in.

    Call immediately after `st.set_page_config` on every page: Streamlit runs
    each page as its own script, so a gate on the home page alone would be
    bypassed by navigating straight to any other page. Session state is shared
    across pages, so signing in once is enough.
    """
    inject_theme()
    if auth.is_authenticated(st.session_state):
        # Every page carries this, not just the entry script: Streamlit renders
        # the page list per page, so hiding it in one place would leave the
        # forwarding entry visible everywhere else.
        hide_entry_from_nav()
        return

    # Hide the page list while signed out. It is not a security control — each
    # page gates itself — but showing a signed-out visitor the full navigation
    # makes the door look decorative.
    st.markdown(
        "<style>"
        "section[data-testid='stSidebar'], div[data-testid='stSidebarNav'],"
        "div[data-testid='collapsedControl'] { display:none !important; }"
        "</style>", unsafe_allow_html=True)
    _login_screen()
    st.stop()


def _login_screen() -> None:
    uri = logo_data_uri("gi_logo_login.png")
    left, mid, right = st.columns([1, 1.5, 1])
    with mid:
        st.markdown(
            f'<div class="ce-login">'
            f'{f"<img src=\'{uri}\' alt=\'General Industries\'>" if uri else ""}'
            f'<h1>{BRAND}</h1>'
            f'<p>Drawing takeoff and BOQ generation</p>'
            f'</div>', unsafe_allow_html=True)

        cred = auth.load_credential()
        if cred is None:
            st.error("No password has been set for this app yet.", icon="🔒")
            st.markdown("Set one, then restart:")
            st.code("venv/bin/python bin/set_password.py", language="bash")
            st.caption("The password is stored as a PBKDF2 hash in "
                       "`.streamlit/secrets.toml`, which git ignores.")
            return

        with st.form("ce_login", clear_on_submit=False):
            password = st.text_input("Password", type="password",
                                     placeholder="Enter the team password")
            submitted = st.form_submit_button("Sign in", type="primary",
                                              use_container_width=True)
        if submitted:
            ok, message = auth.attempt_login(st.session_state, password)
            if ok:
                st.rerun()
            else:
                st.error(message, icon="⚠️")
        if cred.get("hint"):
            st.caption(f"Hint: {cred['hint']}")
        st.caption("A shared password keeps the estimate from being edited by "
                   "accident. It is not protection for sensitive tender data "
                   "on a shared network.")


def sidebar_account() -> None:
    """Sign-out control, at the foot of the sidebar."""
    if not auth.is_authenticated(st.session_state):
        return
    with st.sidebar:
        st.markdown('<div class="ce-sidefoot">Signed in</div>',
                    unsafe_allow_html=True)
        if st.button("Sign out", use_container_width=True, key="_ce_signout"):
            auth.logout(st.session_state)
            st.rerun()


def hide_entry_from_nav() -> None:
    """Drop the entry script from the sidebar page list.

    Streamlit always lists the script it was started from, and there is no
    supported way to say "this one is not a page". Since that script now only
    forwards to Drawing → BOQ, leaving it in the list gives the user a link
    that flickers and returns them to where they already were.

    Matched on the first item of the nav rather than on a generated class name,
    because Streamlit's emotion classes change between releases and a selector
    built on one would silently stop working on the next upgrade.
    """
    st.markdown(
        """
        <style>
          [data-testid="stSidebarNav"] ul li:first-child { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )
