# Curated package list, grouped by build-complexity / asm-pattern category.
# All packages are expected to support cross-compilation and stdenv override.
#
# The matrix only consumes ``all``; the per-category lists are informational
# (they keep the file readable as it grows past ~20 entries). New packages
# go in the most specific category; if an attribute turns out to be broken
# under the legacy nixpkgs cross stdenvs, drop it from ``all`` rather than
# adding per-package compiler-version constraints — the matrix-side filter
# already handles that uniformly.

{ }:

let
  # Tier 1: minimal, fast-building — used for smoke tests
  smoke = [
    { attr = "hello"; label = "hello"; }
    { attr = "busybox"; label = "busybox"; }
  ];

  # Tier 2: small libraries (compression, storage, scripting)
  smallLibs = [
    { attr = "zlib"; label = "zlib"; }
    { attr = "bzip2"; label = "bzip2"; }
    { attr = "lz4"; label = "lz4"; }
    { attr = "xz"; label = "xz"; }
    { attr = "lzo"; label = "lzo"; }
    { attr = "zstd"; label = "zstd"; }
    { attr = "brotli"; label = "brotli"; }
    { attr = "sqlite"; label = "sqlite"; }
    { attr = "lmdb"; label = "lmdb"; }
    { attr = "lua5_4"; label = "lua"; }
  ];

  # Tier 3: classic GNU utilities — autotools-heavy, exercise stdlib
  gnuUtils = [
    { attr = "coreutils"; label = "coreutils"; }
    { attr = "findutils"; label = "findutils"; }
    { attr = "gawk"; label = "gawk"; }
    { attr = "gnused"; label = "gnused"; }
    { attr = "gnugrep"; label = "gnugrep"; }
    { attr = "diffutils"; label = "diffutils"; }
    { attr = "patch"; label = "patch"; }
    { attr = "which"; label = "which"; }
    { attr = "file"; label = "file"; }
  ];

  # Parsers / regex — switch tables, state machines, dispatch loops.
  parsers = [
    { attr = "jq"; label = "jq"; }
    { attr = "cjson"; label = "cjson"; }
    { attr = "expat"; label = "expat"; }
    { attr = "libyaml"; label = "libyaml"; }
    { attr = "libxml2"; label = "libxml2"; }
    { attr = "pcre2"; label = "pcre2"; }
    { attr = "oniguruma"; label = "oniguruma"; }
  ];

  # Codecs — bit-twiddling, deflate, fixed-point DSP, SIMD intrinsics.
  # `march` and `lto` produce the most visible asm differences here.
  codecs = [
    { attr = "libpng"; label = "libpng"; }
    { attr = "libjpeg_turbo"; label = "libjpeg-turbo"; }
    { attr = "libwebp"; label = "libwebp"; }
    { attr = "giflib"; label = "giflib"; }
    { attr = "libogg"; label = "libogg"; }
    { attr = "libvorbis"; label = "libvorbis"; }
    { attr = "flac"; label = "flac"; }
    { attr = "kissfft"; label = "kissfft"; }
    { attr = "mpg123"; label = "mpg123"; }
  ];

  # Cryptography — AES/SHA inner loops, constant-time bit twiddling,
  # arithmetic on fixed-width words. Sanitizers are informative.
  # OpenSSL is the elephant: large but its closure deps already live
  # in this list (zlib), so we get the full library + the binary
  # tarballs of everything it pulls in essentially for free.
  crypto = [
    { attr = "libsodium"; label = "libsodium"; }
    { attr = "mbedtls"; label = "mbedtls"; }
    { attr = "monocypher"; label = "monocypher"; }
    { attr = "nettle"; label = "nettle"; }
    { attr = "libb2"; label = "libb2"; }
    { attr = "xxHash"; label = "xxhash"; }
    { attr = "libargon2"; label = "argon2"; }
    { attr = "openssl"; label = "openssl"; }
    { attr = "gnutls"; label = "gnutls"; }
    { attr = "libgcrypt"; label = "libgcrypt"; }
    { attr = "libssh2"; label = "libssh2"; }
    { attr = "c-ares"; label = "c-ares"; }
  ];

  # Numerics — heavy integer/FP arithmetic, sometimes inline asm.
  # gmp / mpfr exercise word-level optimisation; fftw is the canonical
  # SIMD-rewrite-after-LTO benchmark.
  math = [
    { attr = "gmp"; label = "gmp"; }
    { attr = "mpfr"; label = "mpfr"; }
    { attr = "fftw"; label = "fftw"; }
  ];

  # Small CLI tools — varied: shells, calculators, text tools, daemons.
  # Pulls in different syscall patterns vs. the GNU utils above.
  cliTools = [
    { attr = "dash"; label = "dash"; }
    { attr = "m4"; label = "m4"; }
    { attr = "bc"; label = "bc"; }
    { attr = "ed"; label = "ed"; }
    { attr = "less"; label = "less"; }
    { attr = "nano"; label = "nano"; }
    { attr = "mawk"; label = "mawk"; }
    { attr = "redis"; label = "redis"; }
    { attr = "tcc"; label = "tcc"; }
  ];

in
{
  # Per-category lists are informational; the matrix only uses ``all``.
  tier1 = smoke;
  tier2 = smallLibs;
  tier3 = gnuUtils;
  inherit parsers codecs crypto math cliTools;

  all = smoke ++ smallLibs ++ gnuUtils ++ parsers ++ codecs ++ crypto ++ math ++ cliTools;
}
