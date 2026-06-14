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


def test_known_bad_combo_fastmath_flagset_with_san_undefined() -> None:
    # Same root cause as Ofast+san-undefined — the dedicated fastmath
    # flag set also injects -ffast-math, conflicts with UBSan.
    reason = is_known_bad_combo(_meta(flags="fastmath", sanitizer="san-undefined"))
    assert reason is not None
    assert "fast" in reason.lower()


def test_known_bad_combo_ofast_with_san_address_is_fine() -> None:
    # Only san-undefined collides with -ffast-math; san-address is OK.
    assert is_known_bad_combo(_meta(optimization="Ofast", sanitizer="san-address")) is None


def test_known_bad_combo_fastmath_flagset_with_san_address_is_fine() -> None:
    assert is_known_bad_combo(
        _meta(
            flags="fastmath",
            sanitizer="san-address",
            optimization="O2",
            compiler="clang20",
            compilerFamily="clang",
            compilerVersion="20.1.8",
        )
    ) is None


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


# ---------------------------------------------------------------------------
# Rule A: bzip2 + clang 3.{4..8} + non-x86_64
# ---------------------------------------------------------------------------

def test_rule_a_bzip2_clang3_5_mipsel_is_bad() -> None:
    reason = is_known_bad_combo(
        _meta(
            package="bzip2",
            compilerFamily="clang",
            compilerVersion="3.5.0",
            arch="mipsel",
        )
    )
    assert reason is not None
    assert "bzip2" in reason.lower() or "BZ_EXTERN" in reason or "attribute" in reason.lower()


def test_rule_a_near_miss_bzip2_clang3_9_aarch64_is_fine() -> None:
    # minor=9 is NOT in the bad set {4,5,6,7,8} — near miss
    assert is_known_bad_combo(
        _meta(
            package="bzip2",
            compilerFamily="clang",
            compilerVersion="3.9.1",
            arch="aarch64",
        )
    ) is None


# ---------------------------------------------------------------------------
# Rule C1: xz + clang 3.9 (all arches)
# ---------------------------------------------------------------------------

def test_rule_c1_xz_clang3_9_aarch64_is_bad() -> None:
    reason = is_known_bad_combo(
        _meta(
            package="xz",
            compilerFamily="clang",
            compilerVersion="3.9.0",
            arch="aarch64",
        )
    )
    assert reason is not None
    assert "xz" in reason.lower() or "Werror" in reason or "3.9" in reason


def test_rule_c1_near_miss_xz_clang3_8_x86_64_is_fine() -> None:
    # minor=8 — only 3.9 is affected (near miss)
    assert is_known_bad_combo(
        _meta(
            package="xz",
            compilerFamily="clang",
            compilerVersion="3.8.1",
            arch="x86_64",
        )
    ) is None


# ---------------------------------------------------------------------------
# Rule C2: xz + stackclash hardening + clang 11..17 + cross arches
# ---------------------------------------------------------------------------

def test_rule_c2_xz_stackclash_clang14_mipsel_is_bad() -> None:
    reason = is_known_bad_combo(
        _meta(
            package="xz",
            compilerFamily="clang",
            compilerVersion="14.0.6",
            hardening="stackclash",
            arch="mipsel",
        )
    )
    assert reason is not None
    assert "xz" in reason.lower() or "stack" in reason.lower() or "Werror" in reason


def test_rule_c2_near_miss_xz_stackclash_clang18_mipsel_is_fine() -> None:
    # major=18 is outside the 11..17 window (near miss)
    assert is_known_bad_combo(
        _meta(
            package="xz",
            compilerFamily="clang",
            compilerVersion="18.0.0",
            hardening="stackclash",
            arch="mipsel",
        )
    ) is None


# ---------------------------------------------------------------------------
# Rule D2/E: staticpie + cross sysroot arches (i686 excluded)
# ---------------------------------------------------------------------------

def test_rule_d2_staticpie_ppc64_clang18_is_bad() -> None:
    reason = is_known_bad_combo(
        _meta(
            compilerFamily="clang",
            compilerVersion="18.0.0",
            flags="staticpie",
            arch="ppc64",
        )
    )
    assert reason is not None
    assert "static" in reason.lower() or "rcrt1" in reason.lower() or "Scrt1" in reason


def test_rule_d2_near_miss_staticpie_x86_64_is_fine() -> None:
    # x86_64 cross sysroot has rcrt1.o (near miss)
    assert is_known_bad_combo(
        _meta(
            compilerFamily="clang",
            compilerVersion="18.0.0",
            flags="staticpie",
            arch="x86_64",
        )
    ) is None


# ---------------------------------------------------------------------------
# Rule D3: hardening=zerocallregs + clang + cross arches
# ---------------------------------------------------------------------------

def test_rule_d3_zerocallregs_ppc64_clang18_is_bad() -> None:
    reason = is_known_bad_combo(
        _meta(
            compilerFamily="clang",
            compilerVersion="18.0.0",
            hardening="zerocallregs",
            arch="ppc64",
        )
    )
    assert reason is not None
    assert "zero" in reason.lower() or "call" in reason.lower() or "clang" in reason.lower()


def test_rule_d3_near_miss_zerocallregs_ppc64_gcc15_is_fine() -> None:
    # GCC handles zerocallregs on these arches — clang-only rule (near miss)
    assert is_known_bad_combo(
        _meta(
            compilerFamily="gcc",
            compilerVersion="15.2.0",
            hardening="zerocallregs",
            arch="ppc64",
        )
    ) is None


# ---------------------------------------------------------------------------
# Cross-check regression guards: combos with real successes that must NOT
# be excluded (drawn from the 12,421 verified successes).
# ---------------------------------------------------------------------------

def test_regression_lz4_i686_clang18_staticpie_not_excluded() -> None:
    # lz4 + i686 + staticpie builds successfully — i686 removed from D2/E
    assert is_known_bad_combo(
        _meta(
            package="lz4",
            compilerFamily="clang",
            compilerVersion="18.0.0",
            flags="staticpie",
            arch="i686",
        )
    ) is None


def test_regression_bzip2_mipsel_gcc12_zerocallregs_not_excluded() -> None:
    # bzip2 + mipsel + gcc12 + zerocallregs: gcc works there (D3 is clang-only)
    assert is_known_bad_combo(
        _meta(
            package="bzip2",
            compilerFamily="gcc",
            compilerVersion="12.3.0",
            hardening="zerocallregs",
            arch="mipsel",
        )
    ) is None


def test_regression_bzip2_mips64el_clang18_lto_not_excluded() -> None:
    # bzip2 + mips64el + clang18 + lto: 109 successes — D1 was dropped
    assert is_known_bad_combo(
        _meta(
            package="bzip2",
            compilerFamily="clang",
            compilerVersion="18.0.0",
            flags="lto",
            arch="mips64el",
        )
    ) is None


def test_regression_m4_mips64el_clang16_not_excluded() -> None:
    # m4 + mips64el + clang16: 86 successes — Rule G was dropped
    assert is_known_bad_combo(
        _meta(
            package="m4",
            compilerFamily="clang",
            compilerVersion="16.0.0",
            arch="mips64el",
        )
    ) is None


def test_regression_bzip2_aarch64_clang3_9_not_excluded() -> None:
    # bzip2 + aarch64 + clang3.9: minor=9 not in A's bad set {4,5,6,7,8}
    assert is_known_bad_combo(
        _meta(
            package="bzip2",
            compilerFamily="clang",
            compilerVersion="3.9.1",
            arch="aarch64",
        )
    ) is None
