# GCC flag support matrix

**Methodology**: each version's Option Summary page was fetched once from
`https://gcc.gnu.org/onlinedocs/gcc-X.Y.Z/gcc/Option-Summary.html`.
For flags absent from the Summary (negated forms, `--param` sub-keys,
preprocessor `-D` macros, and flags listed only in sub-pages such as
Instrumentation-Options or Optimize-Options), dedicated section pages and
the per-version Option Index were consulted.  Linker flags (`-z relro`,
`-z now`, `-pie`, `-static-pie`) are passed through by GCC to `ld`; their
presence was confirmed via the Code-Gen-Options and release-notes pages.
All 16 exact doc versions used:

| Project attr | Exact version | Docs used |
|---|---|---|
| gcc4_4 | 4.4.7 | gcc.gnu.org/onlinedocs/gcc-4.4.7/ |
| gcc4_5 | 4.5.4 | gcc.gnu.org/onlinedocs/gcc-4.5.4/ |
| gcc4_6 | 4.6.4 | gcc.gnu.org/onlinedocs/gcc-4.6.4/ |
| gcc4_8 | 4.8.5 | gcc.gnu.org/onlinedocs/gcc-4.8.5/ |
| gcc4_9 | 4.9.4 | gcc.gnu.org/onlinedocs/gcc-4.9.4/ |
| gcc5 | 5.5.0 | gcc.gnu.org/onlinedocs/gcc-5.5.0/ |
| gcc6 | 6.5.0 | gcc.gnu.org/onlinedocs/gcc-6.5.0/ |
| gcc7 | 7.5.0 | gcc.gnu.org/onlinedocs/gcc-7.5.0/ |
| gcc8 | 8.5.0 | gcc.gnu.org/onlinedocs/gcc-8.5.0/ |
| gcc9 | 9.5.0 | gcc.gnu.org/onlinedocs/gcc-9.5.0/ |
| gcc10 | 10.5.0 | gcc.gnu.org/onlinedocs/gcc-10.5.0/ |
| gcc11 | 11.5.0 | gcc.gnu.org/onlinedocs/gcc-11.5.0/ |
| gcc12 | 12.5.0 | gcc.gnu.org/onlinedocs/gcc-12.5.0/ |
| gcc13 | 13.4.0 | gcc.gnu.org/onlinedocs/gcc-13.4.0/ |
| gcc14 | 14.3.0 | gcc.gnu.org/onlinedocs/gcc-14.3.0/ |
| gcc15 | 15.2.0 | gcc.gnu.org/onlinedocs/gcc-15.2.0/ |

## Notes on table conventions

- **✓** = option is documented for that version (or is the `-fno-` negation of a
  documented positive form, which GCC always accepts for boolean options).
- **✗** = option is not documented / not yet introduced in that version.
- **~** = option is a preprocessor `-D` macro or a raw linker flag, not a GCC
  driver option; it passes through in all versions but GCC docs do not list it
  in Option-Summary.  See per-flag footnotes.
- **flags.nix** imposes its own `minGccVersion` guards (see column "flags.nix min"
  below); a ✓ in the table does not mean the project uses the flag for that
  version — check the guard.

---

## Optimization levels

All six GCC-applicable levels have been present since the earliest documented
versions.  `-Oz` is `clangOnly = true` in `flags.nix` and is omitted here.

| Flag     | flags.nix min | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|----------|---------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-O0`    | none          | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O1`    | none          | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O2`    | none          | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O3`    | none          | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Os`    | none          | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Ofast` | none          | ✗   | ✗   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |

> `-Ofast` was introduced in GCC 4.6 (confirmed absent from 4.4 and 4.5 Option
> Summaries; present in 4.6 and all later versions).

---

## Flag sets (code-generation flags)

| Flag                              | flags.nix min  | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|-----------------------------------|----------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-fno-inline`                     | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-funroll-loops`                  | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-ffunction-sections`             | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fdata-sections`                 | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-omit-frame-pointer` [^neg]  | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-PIC` [^neg]                 | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-tree-vectorize` [^neg]      | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-flto`                           | gcc ≥ 4.6      | ✗   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-ffast-math`                     | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fPIE`                           | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |

[^neg]: The Option Summary lists only the positive form (`-fomit-frame-pointer`,
`-fPIC`, `-ftree-vectorize`).  GCC accepts the `-fno-` negation for every
boolean optimization or code-generation flag; `-fno-omit-frame-pointer`,
`-fno-PIC`, and `-fno-tree-vectorize` are valid since at least GCC 4.4.
The Optimize-Options page for GCC 4.4 explicitly cross-references
`-fno-omit-frame-pointer` and `-fno-tree-vectorize` as negations.

> `-flto` first appeared in the GCC 4.5 Option Summary; the 4.4 Summary did not
> list it.  `flags.nix` sets `minGccVersion = { major = 4; minor = 6; }` for the
> `lto` flagset, which is conservative and safe.

---

## Hardening flags

| Flag                                        | flags.nix min  | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---------------------------------------------|----------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-fstack-protector-all`                     | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fstack-protector-strong`                  | none           | ✗   | ✗   | ✗   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `--param=ssp-buffer-size=4` [^param]        | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-D_FORTIFY_SOURCE=2` [^macro]              | none           | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |
| `-D_FORTIFY_SOURCE=3` [^macro]              | gcc ≥ 12       | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |
| `-fPIE`                                     | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fcf-protection=full`                      | gcc ≥ 8        | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-mbranch-protection=standard`              | gcc ≥ 9        | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✗ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fstack-clash-protection`                  | gcc ≥ 8        | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fzero-call-used-regs=used-gpr`            | gcc ≥ 11       | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✗ | ✗ | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Wformat`                                  | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Wformat-security`                         | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Werror=format-security` [^werror]         | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-strict-overflow` [^neg2]              | none           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |

[^param]: `--param=ssp-buffer-size=4` is a sub-key of the general `--param
name=value` facility.  It is listed explicitly in the GCC 4.4 and 4.5
Optimize-Options pages.  Later versions moved the individual `--param` entries
out of the main manual into a separate table (still valid; shown in the GCC 12
Option-Summary).  The parameter is available in all GCC versions that support
`-fstack-protector` (4.1+).

[^macro]: `-D_FORTIFY_SOURCE=N` is a C preprocessor `-D` define, not a GCC
compiler option.  It is not listed in any GCC Option Summary.  GCC passes it to
the preprocessor unconditionally — the actual fortification behaviour is
implemented in glibc headers.  Level 2 works with glibc ≥ 2.3.4 and any GCC.
Level 3 requires glibc ≥ 2.35; the GCC driver does not restrict it.  `flags.nix`
guards `fortify3` with `minGccVersion = { major = 12; minor = 0; }` matching
the glibc version that ships with nixpkgs-22.11, not a compiler requirement.

[^werror]: `-Werror=<name>` (convert a specific warning to an error) is
documented in GCC 4.4's Warning-Options page: "Make the specified warning into
an error."  It does not appear as a standalone entry in the Option Summary; the
keyword is `-Werror=`.

[^neg2]: `-fno-strict-overflow` is the negation of `-fstrict-overflow`.  Both
are explicitly documented in GCC 4.4's and 4.6's Optimize-Options pages.

---

## Linker flags

These flags are passed to `ld` directly (via `NIX_LDFLAGS` / `extraLdflags`),
not as GCC driver arguments.  GCC does not list them in its option docs.

| Flag            | flags.nix context       | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|-----------------|-------------------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-z relro`      | relro / relro-bindnow   | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |
| `-z now`        | bindnow / relro-bindnow | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |
| `-pie`          | pie                     | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |
| `-static-pie`   | staticpie               | ~   | ~   | ~   | ~   | ~   | ~ | ~ | ~ | ~ | ~ | ~  | ~  | ~  | ~  | ~  | ~  |

> `-z relro` and `-z now` are ELF linker options supported by GNU ld ≥ 2.15
> (2004); they are available with all GCC versions in scope.  `-pie` is a
> linker flag; the GCC driver exposes `-fPIE` (compiler) and `-pie` (linker
> stage), both present since GCC 4.x.  `-static-pie` as a **GCC driver** flag
> (combining `-fPIE -static`) was introduced in GCC 8; `flags.nix` passes it
> as an `ldflags` entry so it goes to `ld` directly — in that usage it requires
> BFD linker support, not a minimum GCC version.  `flags.nix` guards
> `staticpie` with `minGccVersion = { major = 8; minor = 0; }`.

---

## Sanitizer flags

| Flag                  | flags.nix min      | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|-----------------------|--------------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-fsanitize=address`  | gcc ≥ 4.8          | ✗   | ✗   | ✗   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fsanitize=undefined`| gcc ≥ 4.9          | ✗   | ✗   | ✗   | ✗   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fsanitize=thread`   | gcc ≥ 4.8          | ✗   | ✗   | ✗   | ✓   | ✓   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |

> `flags.nix` additionally restricts `-fsanitize=address` and `-fsanitize=thread`
> to `archs = ["x86_64" "aarch64"]`.  The table above reflects compiler-level
> support only.

---

## x86-64 microarchitecture levels

| Flag               | flags.nix min | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|--------------------|---------------|-----|-----|-----|-----|-----|---|---|---|---|---|----|----|----|----|----|-----|
| `-march=x86-64-v2` | gcc ≥ 11      | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✗ | ✗ | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-march=x86-64-v3` | gcc ≥ 11      | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✗ | ✗ | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-march=x86-64-v4` | gcc ≥ 11      | ✗   | ✗   | ✗   | ✗   | ✗   | ✗ | ✗ | ✗ | ✗ | ✗ | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |

> x86-64 microarchitecture level names (`x86-64-v2/v3/v4`) were defined in the
> x86-64 psABI supplement and supported by GCC starting with GCC 11 (confirmed
> in the GCC 11 release notes and the GCC 11.5.0 Option Index).

---

## Consolidated single-view table

Rows are every distinct flag string passed by `flags.nix`.  Column headers are
abbreviated GCC majors; where a minor matters the first minor to gain support
is noted in the cell key row.

| Flag                              | 4.4 | 4.5 | 4.6 | 4.8 | 4.9 | 5  | 6  | 7  | 8  | 9  | 10 | 11 | 12 | 13 | 14 | 15 |
|-----------------------------------|-----|-----|-----|-----|-----|----|----|----|----|----|----|----|----|----|----|-----|
| `-O0`                             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O1`                             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O2`                             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-O3`                             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Os`                             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Ofast`                          | ✗   | ✗   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-inline`                     | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-funroll-loops`                  | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-ffunction-sections`             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fdata-sections`                 | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-omit-frame-pointer`         | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-PIC`                        | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-tree-vectorize`             | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-flto`                           | ✗   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-ffast-math`                     | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fPIE`                           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fstack-protector-all`           | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fstack-protector-strong`        | ✗   | ✗   | ✗   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `--param=ssp-buffer-size=4`       | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-D_FORTIFY_SOURCE=2`             | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-D_FORTIFY_SOURCE=3`             | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-fcf-protection=full`            | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-mbranch-protection=standard`    | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fstack-clash-protection`        | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fzero-call-used-regs=used-gpr`  | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Wformat`                        | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Wformat-security`               | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-Werror=format-security`         | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fno-strict-overflow`            | ✓   | ✓   | ✓   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-z relro` (linker)               | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-z now` (linker)                 | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-pie` (linker)                   | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-static-pie` (linker/gcc-8+)     | ~   | ~   | ~   | ~   | ~   | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  | ~  |
| `-fsanitize=address`              | ✗   | ✗   | ✗   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fsanitize=undefined`            | ✗   | ✗   | ✗   | ✗   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-fsanitize=thread`               | ✗   | ✗   | ✗   | ✓   | ✓   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-march=x86-64-v2`                | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-march=x86-64-v3`                | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |
| `-march=x86-64-v4`                | ✗   | ✗   | ✗   | ✗   | ✗   | ✗  | ✗  | ✗  | ✗  | ✗  | ✗  | ✓  | ✓  | ✓  | ✓  | ✓  |

---

## Alignment with flags.nix minGccVersion guards

The table below summarises where the `minGccVersion` guard in `flags.nix`
matches, is conservative (allows more versions than strictly needed), or was
set for non-compiler reasons.

| flags.nix guard         | Guarded flag(s)                    | Doc-confirmed first version | Guard status |
|-------------------------|------------------------------------|----------------------------|--------------|
| `minGccVersion 4.6`     | `-flto`                            | 4.5 (Option Summary)       | Conservative by 1 minor — safe |
| `minGccVersion 8.0`     | `-static-pie`, `-fcf-protection=full`, `-fstack-clash-protection` | GCC 8 | Exact |
| `minGccVersion 9.0`     | `-mbranch-protection=standard`     | GCC 9                      | Exact |
| `minGccVersion 11.0`    | `-fzero-call-used-regs=used-gpr`, `-march=x86-64-v2/v3/v4` | GCC 11 | Exact |
| `minGccVersion 12.0`    | `-D_FORTIFY_SOURCE=3`              | Any GCC (glibc 2.35 limit) | Conservative (glibc-driven, not GCC) |
| `minGccVersion 4.8`     | `-fsanitize=address`, `-fsanitize=thread` | GCC 4.8              | Exact |
| `minGccVersion 4.9`     | `-fsanitize=undefined`             | GCC 4.9                    | Exact |
