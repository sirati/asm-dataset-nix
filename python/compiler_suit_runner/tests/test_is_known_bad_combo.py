"""Unit tests for :func:`compiler_suit_runner.preflight.is_known_bad_combo`.

Split out of ``test_preflight.py`` because the rule list has grown to
its own substantive section. No subprocess stubs needed — the function
is pure, takes a meta-entry dict and returns a reason string or None.
"""

from __future__ import annotations

from compiler_suit_runner.preflight import is_known_bad_combo


def _meta(**overrides) -> dict:
    base = {
        "compiler": "gcc15",
        "compilerFamily": "gcc",
        "compilerVersion": "15.2.0",
        "optimization": "O2",
        "flags": "baseline",
        "hardening": "default",
        "sanitizer": "san-off",
        "march": "march-default",
        "package": "hello",
        "arch": "x86_64",
    }
    base.update(overrides)
    return base


def test_known_bad_combo_o0_with_sanitizer() -> None:
    reason = is_known_bad_combo(_meta(optimization="O0", sanitizer="san-undefined"))
    assert reason is not None
    assert "O0" in reason or "optim" in reason.lower()


def test_known_bad_combo_o0_with_san_address() -> None:
    assert is_known_bad_combo(_meta(optimization="O0", sanitizer="san-address")) is not None


def test_known_bad_combo_o0_with_san_off_is_fine() -> None:
    assert is_known_bad_combo(_meta(optimization="O0", sanitizer="san-off")) is None


def test_known_bad_combo_old_clang_with_sanitizer() -> None:
    reason = is_known_bad_combo(
        _meta(
            compiler="clang5",
            compilerFamily="clang",
            compilerVersion="5.0.2",
            optimization="O2",
            sanitizer="san-undefined",
        )
    )
    assert reason is not None
    assert "clang" in reason


def test_known_bad_combo_modern_clang_with_sanitizer_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            optimization="O2",
            sanitizer="san-undefined",
        )
    ) is None


def test_known_bad_combo_old_gcc_with_sanitizer() -> None:
    reason = is_known_bad_combo(
        _meta(
            compiler="gcc4_8",
            compilerFamily="gcc",
            compilerVersion="4.8.5",
            optimization="O2",
            sanitizer="san-address",
        )
    )
    assert reason is not None
    assert "gcc" in reason


def test_known_bad_combo_modern_gcc_with_sanitizer_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="gcc15",
            compilerFamily="gcc",
            compilerVersion="15.2.0",
            optimization="O2",
            sanitizer="san-thread",
        )
    ) is None


def test_known_bad_combo_ofast_with_san_undefined() -> None:
    reason = is_known_bad_combo(_meta(optimization="Ofast", sanitizer="san-undefined"))
    assert reason is not None
    assert "fast" in reason.lower() or "Ofast" in reason


def test_known_bad_combo_ofast_with_san_address_is_fine() -> None:
    # Only san-undefined collides with -ffast-math; san-address is OK.
    assert is_known_bad_combo(_meta(optimization="Ofast", sanitizer="san-address")) is None


def test_known_bad_combo_san_off_never_fails() -> None:
    # Spot-check: any combo with san-off should be acceptable regardless
    # of optimization / compiler / hardening.
    for opt in ("O0", "O1", "O2", "O3", "Os", "Oz", "Ofast"):
        assert is_known_bad_combo(_meta(optimization=opt, sanitizer="san-off")) is None
    for cc, ver in (("clang", "3.4.1"), ("clang", "20.1.8"), ("gcc", "4.4"), ("gcc", "15.2.0")):
        assert is_known_bad_combo(
            _meta(compilerFamily=cc, compilerVersion=ver, sanitizer="san-off")
        ) is None


def test_known_bad_combo_old_clang_with_lto() -> None:
    # clang10 from nixpkgs-22.11 + lto: archive index missing because
    # the cross binutils there has no LLVMgold plugin wired in.
    reason = is_known_bad_combo(
        _meta(
            compiler="clang10",
            compilerFamily="clang",
            compilerVersion="10.0.1",
            flags="lto",
            sanitizer="san-off",
        )
    )
    assert reason is not None
    assert "lto" in reason.lower() or "ar" in reason.lower()


def test_known_bad_combo_old_clang_with_ltothin() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang14",
            compilerFamily="clang",
            compilerVersion="14.0.6",
            flags="ltothin",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_modern_clang_with_lto_is_fine() -> None:
    # Current unstable nixpkgs ships clang ≥ 18 with working LTO.
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            flags="lto",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_old_gcc_with_lto() -> None:
    reason = is_known_bad_combo(
        _meta(
            compiler="gcc11",
            compilerFamily="gcc",
            compilerVersion="11.4.0",
            flags="lto",
            sanitizer="san-off",
        )
    )
    assert reason is not None
    assert "lto" in reason.lower() or "ar" in reason.lower()


def test_known_bad_combo_modern_gcc_with_lto_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="gcc15",
            compilerFamily="gcc",
            compilerVersion="15.2.0",
            flags="lto",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_lto_only_filters_lto_flagsets() -> None:
    # Only ``lto`` / ``ltothin`` are affected; old compilers with other
    # flag sets stay sampled.
    assert is_known_bad_combo(
        _meta(
            compiler="clang10",
            compilerFamily="clang",
            compilerVersion="10.0.1",
            flags="baseline",
            sanitizer="san-off",
        )
    ) is None
    assert is_known_bad_combo(
        _meta(
            compiler="clang10",
            compilerFamily="clang",
            compilerVersion="10.0.1",
            flags="unroll",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_old_clang_with_staticpie() -> None:
    # legacy clang's cc-wrapper lacks rcrt1.o; configure link fails.
    assert is_known_bad_combo(
        _meta(
            compiler="clang10",
            compilerFamily="clang",
            compilerVersion="10.0.1",
            flags="staticpie",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_modern_clang_with_staticpie_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            flags="staticpie",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_old_clang_with_pie_hardening() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang11",
            compilerFamily="clang",
            compilerVersion="11.1.0",
            hardening="pie",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_modern_clang_with_pie_hardening_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            hardening="pie",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_old_clang_with_cet_hardening() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang11",
            compilerFamily="clang",
            compilerVersion="11.1.0",
            hardening="cet",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_modern_clang_with_cet_hardening_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            hardening="cet",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_old_clang_with_march_v2() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang12",
            compilerFamily="clang",
            compilerVersion="12.0.1",
            march="march-v2",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_old_gcc_with_march_v3() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="gcc11",
            compilerFamily="gcc",
            compilerVersion="11.4.0",
            march="march-v3",
            sanitizer="san-off",
        )
    ) is not None


def test_known_bad_combo_modern_clang_with_march_v4_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
            march="march-v4",
            sanitizer="san-off",
        )
    ) is None


def test_known_bad_combo_default_hardening_and_march_never_fails() -> None:
    # Old compilers with the default hardening + march should remain
    # buildable for non-LTO / non-sanitizer combos.
    assert is_known_bad_combo(
        _meta(
            compiler="clang10",
            compilerFamily="clang",
            compilerVersion="10.0.1",
            flags="baseline",
            hardening="default",
            march="march-default",
            sanitizer="san-off",
        )
    ) is None
