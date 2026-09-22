"""Login gate, brand assets and the shared UI kit.

The security tests here are about the properties that actually matter for a
shared-password gate: the plaintext never reaches disk, a wrong password is
rejected, guessing is slowed down, and — the one that is easy to get wrong —
*every* page carries the gate, because Streamlit runs each page as its own
script and a gate on the home page alone is bypassed by a direct URL.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ui import auth

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "Home.py"] + sorted((ROOT / "pages").glob("*.py"))


# ================= password handling =================
class TestPasswordHashing:
    def test_same_password_and_salt_gives_the_same_hash(self):
        salt, digest, iters = auth.hash_password("hunter2000")
        assert auth.hash_password("hunter2000", salt, iters)[1] == digest

    def test_different_salts_give_different_hashes(self):
        _, a, _ = auth.hash_password("hunter2000")
        _, b, _ = auth.hash_password("hunter2000")
        assert a != b, "a fresh salt must be generated per password"

    def test_verify_accepts_the_right_password(self):
        salt, digest, iters = auth.hash_password("hunter2000")
        assert auth.verify_password("hunter2000", salt, digest, iters)

    @pytest.mark.parametrize("wrong", ["hunter2001", "", "HUNTER2000", " hunter2000"])
    def test_verify_rejects_everything_else(self, wrong):
        salt, digest, iters = auth.hash_password("hunter2000")
        assert not auth.verify_password(wrong, salt, digest, iters)

    def test_verify_survives_a_corrupt_stored_salt(self):
        assert not auth.verify_password("x", "not-hex", "abc")

    def test_iteration_count_is_not_trivially_low(self):
        """A single fast digest of a short human password is brute-forced in
        seconds; the work factor is the whole point of using PBKDF2."""
        assert auth.ITERATIONS >= 100_000


class TestCredentialFile:
    def test_plaintext_never_reaches_disk(self, tmp_path):
        secret = "correct horse battery staple"
        path = auth.save_credential(secret, tmp_path / "s.toml")
        assert secret not in path.read_text()

    def test_round_trip(self, tmp_path):
        path = tmp_path / "s.toml"
        auth.save_credential("hunter2000", path, hint="the usual")
        cred = auth.load_credential(path)
        assert cred["hint"] == "the usual"
        assert auth.verify_password("hunter2000", cred["salt"], cred["hash"],
                                    cred["iterations"])

    def test_file_is_owner_only(self, tmp_path):
        path = auth.save_credential("hunter2000", tmp_path / "s.toml")
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_missing_file_means_unconfigured(self, tmp_path):
        assert auth.load_credential(tmp_path / "absent.toml") is None
        assert not auth.is_configured(tmp_path / "absent.toml")

    def test_malformed_file_is_treated_as_unconfigured(self, tmp_path):
        path = tmp_path / "s.toml"
        path.write_text("this is not toml [[[")
        assert auth.load_credential(path) is None

    def test_file_without_an_auth_section_is_unconfigured(self, tmp_path):
        path = tmp_path / "s.toml"
        path.write_text('[other]\nkey = "value"\n')
        assert auth.load_credential(path) is None

    def test_resetting_the_password_keeps_other_sections(self, tmp_path):
        path = tmp_path / "s.toml"
        path.write_text('[other]\nkey = "value"\n')
        auth.save_credential("hunter2000", path)
        auth.save_credential("hunter3000", path)
        text = path.read_text()
        assert "[other]" in text and text.count("[auth]") == 1
        cred = auth.load_credential(path)
        assert auth.verify_password("hunter3000", cred["salt"], cred["hash"],
                                    cred["iterations"])


class TestLoginFlow:
    def test_correct_password_authenticates(self, tmp_path):
        path = auth.save_credential("hunter2000", tmp_path / "s.toml")
        session: dict = {}
        ok, _ = auth.attempt_login(session, "hunter2000", path)
        assert ok and auth.is_authenticated(session)

    def test_wrong_password_does_not_authenticate(self, tmp_path):
        path = auth.save_credential("hunter2000", tmp_path / "s.toml")
        session: dict = {}
        ok, msg = auth.attempt_login(session, "nope", path)
        assert not ok and not auth.is_authenticated(session)
        assert "Incorrect" in msg

    def test_repeated_guessing_is_slowed_down(self, tmp_path):
        path = auth.save_credential("hunter2000", tmp_path / "s.toml")
        session: dict = {}
        for _ in range(auth.MAX_ATTEMPTS):
            auth.attempt_login(session, "nope", path)
        ok, msg = auth.attempt_login(session, "hunter2000", path)
        assert not ok, "a correct password must not bypass the cool-off"
        assert "Too many attempts" in msg

    def test_unconfigured_app_refuses_entry(self, tmp_path):
        """Failing open would be worse than having no gate: it looks locked."""
        session: dict = {}
        ok, msg = auth.attempt_login(session, "anything", tmp_path / "absent.toml")
        assert not ok and "No password" in msg
        assert not auth.is_authenticated(session)

    def test_logout_clears_the_session(self, tmp_path):
        path = auth.save_credential("hunter2000", tmp_path / "s.toml")
        session: dict = {}
        auth.attempt_login(session, "hunter2000", path)
        auth.logout(session)
        assert not auth.is_authenticated(session)


# ================= every page is gated =================
class TestEveryPageIsGated:
    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
    def test_page_calls_require_login(self, page):
        assert "kit.require_login()" in page.read_text(), (
            f"{page.name} is reachable by URL without a gate")

    @pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
    def test_gate_runs_before_any_page_content(self, page):
        """The gate must sit directly after set_page_config, before anything
        that reads or writes the project."""
        text = page.read_text()
        gate = text.index("kit.require_login()")
        for marker in ("st.header(", "st.subheader(", "kit.page_header("):
            if marker in text:
                assert gate < text.index(marker), f"{page.name}: {marker} before gate"


# ================= brand assets =================
class TestBrandAssets:
    ASSETS = ROOT / "assets"

    @pytest.mark.parametrize("name", ["gi_logo_sidebar.png", "gi_logo_login.png",
                                      "gi_logo_header.png", "gi_favicon.png"])
    def test_asset_exists_and_is_a_png(self, name):
        path = self.ASSETS / name
        assert path.exists(), f"{name} missing — run bin/build_assets.py"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_assets_are_small_enough_to_inline(self):
        """They are base64'd into every page render; the 12 MB master is not."""
        for path in self.ASSETS.glob("*.png"):
            assert path.stat().st_size < 200_000, f"{path.name} too large to inline"

    def test_transparency_is_preserved(self):
        from PIL import Image
        assert Image.open(self.ASSETS / "gi_logo_sidebar.png").mode in ("RGBA", "LA")

    def test_build_is_reproducible_from_the_master(self, tmp_path):
        master = ROOT / "Logo" / "GI_Logo.tiff"
        if not master.exists():
            pytest.skip("master artwork not present")
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_assets", ROOT / "bin" / "build_assets.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert len(mod.build()) == 4


# ================= UI kit =================
class TestUIKit:
    def test_logo_inlines_as_a_data_uri(self):
        from ui import kit
        assert kit.logo_data_uri().startswith("data:image/png;base64,")

    def test_missing_asset_returns_empty_rather_than_raising(self):
        from ui import kit
        assert kit.logo_data_uri("does_not_exist.png") == ""

    def test_header_font_is_larger_than_streamlit_default(self):
        """Request: enlarge the header. Streamlit's h1 is ~1.75 rem."""
        from ui import kit
        m = re.search(r"\.ce-title\s*\{[^}]*font-size:\s*([\d.]+)rem", kit.THEME_CSS)
        assert m and float(m.group(1)) >= 2.0

    def test_header_is_pinned(self):
        from ui import kit
        css = kit.THEME_CSS
        assert "position: sticky" in css
        assert "stElementContainer" in css, (
            "the wrapper test id was measured from a running app; "
            "the older element-container id no longer matches")

    def test_sticky_offset_clears_streamlit_toolbar(self):
        """Streamlit's toolbar is fixed and 60 px tall; a sticky header at
        top:0 pins underneath it and vanishes."""
        from ui import kit
        m = re.search(r"--ce-top:\s*(\d+)px", kit.THEME_CSS)
        assert m and int(m.group(1)) >= 48

    def test_the_header_no_longer_carries_page_step_chips(self):
        """One chip per page made sense with five pages. The pages are tabs in
        one workspace now, and a second row of navigation would be a copy of
        the tab bar that did nothing when clicked."""
        from ui import kit
        assert not hasattr(kit, "STEPS")
        assert "ce-steps" not in kit.THEME_CSS

    def test_every_badge_tone_has_a_colour(self):
        from core import drawing_status as DS
        from ui import kit
        for tone in set(DS.TONES.values()):
            assert f".ce-badge.{tone}" in kit.THEME_CSS, tone

    def test_brand_text_is_escaped(self):
        from ui import kit
        assert "&lt;script&gt;" in kit._esc("<script>")
