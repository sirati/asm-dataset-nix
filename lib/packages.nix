# Curated package list, grouped by build-complexity / asm-pattern category
# and priority within each category.
#
# Priority rubric:
#   high — small, broadly compatible, very high asm-pattern ROI;
#          the "must-have" core for any meaningful run.
#   med  — useful and well-shaped, but bigger / modern-C++ / specialized.
#   low  — large, experimental, heavy deps, or risky cross-compile.
#          Iterate to drop entries that can't be made to build.
#
# The matrix only consumes ``all`` (flat list of all priorities, all
# categories). Selecting a subset (e.g. only ``high``) is a future
# enhancement — for now the priority labelling is a curation guide,
# not a build-time gate.
#
# Modern-C++-only entries (simdjson, rapidjson, x265, capnproto, …)
# will surface as failures on legacy gcc/clang variants; that's
# expected — the matrix swallows them and moves on.

{ }:

let
  pkg = a: l: { attr = a; label = l; };

  # ---------- Tier 1: smoke ----------
  smoke = {
    high = [
      (pkg "hello" "hello")
      (pkg "busybox" "busybox")
    ];
    med = [ ];
    low = [ ];
  };

  # ---------- Tier 2: small libs (compression, storage, scripting) ----------
  smallLibs = {
    high = [
      (pkg "zlib" "zlib")
      (pkg "bzip2" "bzip2")
      (pkg "lz4" "lz4")
      (pkg "xz" "xz")
      (pkg "sqlite" "sqlite")
      (pkg "lua5_4" "lua")
    ];
    med = [
      (pkg "lzo" "lzo")
      (pkg "zstd" "zstd")
      (pkg "brotli" "brotli")
      (pkg "snappy" "snappy")
      (pkg "lzfse" "lzfse")
      (pkg "miniz" "miniz")
      (pkg "zopfli" "zopfli")
      (pkg "lmdb" "lmdb")
      (pkg "pigz" "pigz")
      (pkg "lzip" "lzip")
      (pkg "lzop" "lzop")
      (pkg "liblzf" "liblzf")
    ];
    low = [
      (pkg "lzham" "lzham")
      (pkg "bzip3" "bzip3")
      (pkg "zlib-ng" "zlib-ng")
      (pkg "zpaq" "zpaq")
    ];
  };

  # ---------- Tier 3: classic GNU utilities ----------
  gnuUtils = {
    high = [
      (pkg "coreutils" "coreutils")
      (pkg "gawk" "gawk")
      (pkg "gnused" "gnused")
      (pkg "gnugrep" "gnugrep")
      (pkg "findutils" "findutils")
    ];
    med = [
      (pkg "diffutils" "diffutils")
      (pkg "patch" "patch")
      (pkg "which" "which")
      (pkg "file" "file")
    ];
    low = [ ];
  };

  # ---------- Parsers / regex / string ----------
  parsers = {
    high = [
      (pkg "jq" "jq")
      (pkg "cjson" "cjson")
      (pkg "simdjson" "simdjson")
      (pkg "json_c" "json_c")
      (pkg "yyjson" "yyjson")
      (pkg "expat" "expat")
      (pkg "libxml2" "libxml2")
      (pkg "libyaml" "libyaml")
      (pkg "pcre2" "pcre2")
      (pkg "re2" "re2")
    ];
    med = [
      (pkg "rapidjson" "rapidjson")
      (pkg "pugixml" "pugixml")
      (pkg "tinyxml-2" "tinyxml-2")
      (pkg "gumbo" "gumbo")
      (pkg "lexbor" "lexbor")
      (pkg "tomlc99" "tomlc99")
      (pkg "cmark" "cmark")
      (pkg "md4c" "md4c")
      (pkg "uriparser" "uriparser")
      (pkg "oniguruma" "oniguruma")
      (pkg "hyperscan" "hyperscan")
      (pkg "vectorscan" "vectorscan")
      (pkg "utf8proc" "utf8proc")
    ];
    low = [
      (pkg "rapidxml" "rapidxml")
      (pkg "rapidyaml" "rapidyaml")
      (pkg "tomlplusplus" "tomlplusplus")
      (pkg "libconfig" "libconfig")
      (pkg "libconfuse" "libconfuse")
      (pkg "libcbor" "libcbor")
      (pkg "cmark-gfm" "cmark-gfm")
      (pkg "multimarkdown" "multimarkdown")
      (pkg "discount" "discount")
      (pkg "inih" "inih")
      (pkg "iniparser" "iniparser")
      (pkg "tre" "tre")
      (pkg "libstemmer" "libstemmer")
      (pkg "hunspell" "hunspell")
      (pkg "libdatrie" "libdatrie")
      (pkg "libidn" "libidn")
    ];
  };

  # ---------- Codecs / DSP ----------
  codecs = {
    high = [
      (pkg "libpng" "libpng")
      (pkg "libjpeg_turbo" "libjpeg-turbo")
      (pkg "flac" "flac")
      (pkg "libopus" "libopus")
      (pkg "kissfft" "kissfft")
      (pkg "dav1d" "dav1d")  # canonical SIMD-asm reference
    ];
    med = [
      (pkg "libwebp" "libwebp")
      (pkg "giflib" "giflib")
      (pkg "libtiff" "libtiff")
      (pkg "libogg" "libogg")
      (pkg "libvorbis" "libvorbis")
      (pkg "mpg123" "mpg123")
      (pkg "libsndfile" "libsndfile")
      (pkg "x264" "x264")
      (pkg "openh264" "openh264")
      (pkg "libsamplerate" "libsamplerate")
      (pkg "soxr" "soxr")
      (pkg "speex" "speex")
      (pkg "wavpack" "wavpack")
      (pkg "codec2" "codec2")
      (pkg "pffft" "pffft")
    ];
    low = [
      (pkg "x265" "x265")
      (pkg "libjxl" "libjxl")
      (pkg "openjpeg" "openjpeg")
      (pkg "lcms2" "lcms2")
      (pkg "libimagequant" "libimagequant")
      (pkg "imlib2" "imlib2")
      (pkg "dcraw" "dcraw")
      (pkg "pngquant" "pngquant")
      (pkg "optipng" "optipng")
      (pkg "mozjpeg" "mozjpeg")
      (pkg "guetzli" "guetzli")
      (pkg "jpegoptim" "jpegoptim")
      (pkg "netpbm" "netpbm")
      (pkg "vips" "vips")
      (pkg "rubberband" "rubberband")
      (pkg "soundtouch" "soundtouch")
      (pkg "aubio" "aubio")
      (pkg "ladspa-sdk" "ladspa-sdk")
      (pkg "lv2" "lv2")
      (pkg "lilv" "lilv")
      (pkg "serd" "serd")
      (pkg "sord" "sord")
      (pkg "flite" "flite")
      (pkg "sox" "sox")
    ];
  };

  # ---------- Cryptography ----------
  crypto = {
    high = [
      (pkg "openssl" "openssl")
      (pkg "libsodium" "libsodium")
      (pkg "mbedtls" "mbedtls")
      (pkg "xxHash" "xxhash")
      (pkg "libb2" "libb2")
      (pkg "secp256k1" "secp256k1")
    ];
    med = [
      (pkg "gnutls" "gnutls")
      (pkg "libgcrypt" "libgcrypt")
      (pkg "monocypher" "monocypher")
      (pkg "nettle" "nettle")
      (pkg "libtomcrypt" "libtomcrypt")
      (pkg "libtommath" "libtommath")
      (pkg "libargon2" "argon2")
      (pkg "libssh2" "libssh2")
      (pkg "c-ares" "c-ares")
      (pkg "wolfssl" "wolfssl")
      (pkg "bearssl" "bearssl")
      (pkg "libblake3" "libblake3")
      (pkg "rhash" "rhash")
      (pkg "libseccomp" "libseccomp")
      (pkg "blst" "blst")
      (pkg "botan3" "botan3")
    ];
    low = [
      (pkg "liboqs" "liboqs")
      (pkg "pbc" "pbc")
      (pkg "s2n-tls" "s2n-tls")
    ];
  };

  # ---------- Serialization / RPC ----------
  serialization = {
    high = [
      (pkg "msgpack-c" "msgpack-c")
      (pkg "nanopb" "nanopb")
    ];
    med = [
      (pkg "flatbuffers" "flatbuffers")
      (pkg "flatcc" "flatcc")
      (pkg "capnproto" "capnproto")
      (pkg "protobufc" "protobufc")
      (pkg "msgpack-cxx" "msgpack-cxx")
    ];
    low = [
      (pkg "avro-c" "avro-c")
      (pkg "avro-cpp" "avro-cpp")
      (pkg "libmpack" "libmpack")
    ];
  };

  # ---------- Numerics / algorithms / geometry ----------
  math = {
    high = [
      (pkg "gmp" "gmp")
      (pkg "mpfr" "mpfr")
      (pkg "fftw" "fftw")
      (pkg "gsl" "gsl")
    ];
    med = [
      (pkg "armadillo" "armadillo")
      (pkg "igraph" "igraph")
      (pkg "glpk" "glpk")
      (pkg "nlopt" "nlopt")
      (pkg "cln" "cln")
      (pkg "ipopt" "ipopt")
      (pkg "lapack-reference" "lapack-reference")
      (pkg "nauty" "nauty")
      (pkg "bliss" "bliss")
      (pkg "mpir" "mpir")
    ];
    low = [
      (pkg "ntl" "ntl")
      (pkg "flint" "flint")
      (pkg "gf2x" "gf2x")
      (pkg "ginac" "ginac")
      (pkg "suitesparse-graphblas" "suitesparse-graphblas")
      (pkg "cgal" "cgal")
      (pkg "geogram" "geogram")
      (pkg "draco" "draco")
      (pkg "meshoptimizer" "meshoptimizer")
      (pkg "nanoflann" "nanoflann")
      (pkg "geos" "geos")
      (pkg "proj" "proj")
      (pkg "s2geometry" "s2geometry")
      (pkg "tippecanoe" "tippecanoe")
      (pkg "shapelib" "shapelib")
    ];
  };

  # ---------- Networking / protocols ----------
  networking = {
    high = [
      (pkg "hiredis" "hiredis")
      (pkg "nghttp2" "nghttp2")
    ];
    med = [
      (pkg "zeromq" "zeromq")
      (pkg "mosquitto" "mosquitto")
      (pkg "nanomsg" "nanomsg")
      (pkg "nng" "nng")
      (pkg "libwebsockets" "libwebsockets")
      (pkg "libdnet" "libdnet")
      (pkg "ldns" "ldns")
      (pkg "libcoap" "libcoap")
    ];
    low = [
      (pkg "nghttp3" "nghttp3")
      (pkg "h2o" "h2o")
      (pkg "czmq" "czmq")
      (pkg "libmodbus" "libmodbus")
      (pkg "knot-dns" "knot-dns")
      (pkg "libtins" "libtins")
      (pkg "libnids" "libnids")
      (pkg "neon" "neon")
    ];
  };

  # ---------- Embedded VMs / interpreters ----------
  interpreters = {
    high = [
      (pkg "tcc" "tcc")
      (pkg "duktape" "duktape")
      (pkg "quickjs" "quickjs")
      (pkg "mujs" "mujs")
      (pkg "wasm3" "wasm3")
    ];
    med = [
      (pkg "jerryscript" "jerryscript")
      (pkg "mruby" "mruby")
      (pkg "janet" "janet")
      (pkg "tinyscheme" "tinyscheme")
      (pkg "picoc" "picoc")
    ];
    low = [
      (pkg "angelscript" "angelscript")
      (pkg "chibi-scheme" "chibi-scheme")
      (pkg "quickjs-ng" "quickjs-ng")
    ];
  };

  # ---------- Memory / system primitives ----------
  system = {
    high = [
      (pkg "jemalloc" "jemalloc")
      (pkg "mimalloc" "mimalloc")
    ];
    med = [
      (pkg "gperftools" "gperftools")
      (pkg "hwloc" "hwloc")
      (pkg "liburing" "liburing")
      (pkg "libaio" "libaio")
      (pkg "libatomic_ops" "libatomic_ops")
    ];
    low = [
      (pkg "liburcu" "liburcu")
      (pkg "libcork" "libcork")
      (pkg "talloc" "talloc")
    ];
  };

  # ---------- Binary analysis / disassembly ----------
  binaryAnalysis = {
    high = [
      (pkg "capstone" "capstone")
      (pkg "libelf" "libelf")
    ];
    med = [
      (pkg "zydis" "zydis")
      (pkg "unicorn" "unicorn")
      (pkg "elfutils" "elfutils")
    ];
    low = [
      (pkg "keystone" "keystone")
      (pkg "xed" "xed")
    ];
  };

  # ---------- Small CLI tools ----------
  cliTools = {
    high = [
      (pkg "dash" "dash")
      (pkg "m4" "m4")
      (pkg "bc" "bc")
      (pkg "less" "less")
    ];
    med = [
      (pkg "ed" "ed")
      (pkg "nano" "nano")
      (pkg "mawk" "mawk")
      (pkg "redis" "redis")
    ];
    low = [ ];
  };

  # ---------- Misc utilities ----------
  misc = {
    high = [ ];
    med = [
      (pkg "libcpuid" "libcpuid")
      (pkg "qrencode" "qrencode")
      (pkg "libb64" "libb64")
    ];
    low = [
      (pkg "libdmtx" "libdmtx")
      (pkg "cmph" "cmph")
      (pkg "gperf" "gperf")
      (pkg "libcorrect" "libcorrect")
      (pkg "libmaxminddb" "libmaxminddb")
    ];
  };

  # — Helpers —
  flatten = c: c.high ++ c.med ++ c.low;

  categories = [
    smoke smallLibs gnuUtils parsers codecs crypto serialization
    math networking interpreters system binaryAnalysis cliTools misc
  ];

in
{
  # Per-category structures (each has .high / .med / .low sub-lists).
  inherit
    smoke
    smallLibs
    gnuUtils
    parsers
    codecs
    crypto
    serialization
    math
    networking
    interpreters
    system
    binaryAnalysis
    cliTools
    misc
    ;

  # Back-compat aliases (flattened, all priorities).
  tier1 = flatten smoke;
  tier2 = flatten smallLibs;
  tier3 = flatten gnuUtils;

  # Priority cross-cuts: every "high" / "med" / "low" pick from every
  # category, useful when an operator wants to limit the matrix to
  # high-confidence picks first.
  highOnly = builtins.concatMap (c: c.high) categories;
  medOnly = builtins.concatMap (c: c.med) categories;
  lowOnly = builtins.concatMap (c: c.low) categories;

  all = builtins.concatMap flatten categories;
}
