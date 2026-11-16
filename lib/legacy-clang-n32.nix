# Clang 3.4 changes N32 widths without changing the corresponding C types.
# Recompile its target definitions and driver entries only. All other frontend
# and backend code comes from the original cached 3.4 libraries.
{ oldPkgs, clang, llvm }:
let
  version = "3.4.2";
  source = oldPkgs.fetchurl {
    url = "http://llvm.org/releases/${version}/cfe-${version}.src.tar.gz";
    sha256 = "045wjnp5j8xd2zjhvldcllnwlnrwz3dafmlk412z804d5xvzb9jv";
  };
  repaired = oldPkgs.stdenv.mkDerivation {
    name = "clang-${version}-n32-abi";
    src = source;
    unpackPhase = ''
      tar -xf "$src" --strip-components=1 \
        cfe-${version}.src/lib/Basic/Targets.cpp \
        cfe-${version}.src/tools/driver/driver.cpp \
        cfe-${version}.src/tools/driver/cc1_main.cpp \
        cfe-${version}.src/tools/driver/cc1as_main.cpp
      sourceRoot=.
    '';
    patchPhase = ''
      sed -i '/if (Name == "n32") {/,/ABI = Name;/s/ABI = Name;/SizeType = UnsignedInt; PtrDiffType = SignedInt; ABI = Name;/' lib/Basic/Targets.cpp
      grep -A8 'if (Name == "n32")' lib/Basic/Targets.cpp | grep 'SizeType = UnsignedInt;'
    '';
    dontConfigure = true;
    hardeningDisable = [ "all" ];
    buildPhase = ''
      flags=(-std=c++11 -O2 -g0 -fno-rtti -fno-exceptions
        -D__STDC_LIMIT_MACROS -D__STDC_CONSTANT_MACROS -D_GNU_SOURCE
        -I${clang}/include -I${llvm}/include)
      for source in lib/Basic/Targets.cpp tools/driver/{driver,cc1_main,cc1as_main}.cpp; do
        "$CXX" "''${flags[@]}" -c "$source" -o "$(basename "$source" .cpp).o"
      done
      "$CXX" -o clang-3.4 driver.o cc1_main.o cc1as_main.o Targets.o \
        -Wl,--start-group ${clang}/lib/libclang*.a -Wl,--end-group \
        -L${llvm}/lib -Wl,-rpath,${llvm}/lib \
        $(${llvm}/bin/llvm-config --libs all) -L${oldPkgs.zlib}/lib -lz -lpthread -ldl -rdynamic
    '';
    doCheck = true;
    checkPhase = ''
      cat > n32.c <<'C'
      typedef char size_type[__builtin_types_compatible_p(__typeof__(sizeof(0)), unsigned int) ? 1 : -1];
      typedef char difference_type[__builtin_types_compatible_p(__typeof__((char*)0 - (char*)0), int) ? 1 : -1];
      typedef char intptr_width[sizeof(__INTPTR_TYPE__) == sizeof(void*) ? 1 : -1];
      C
      ./clang-3.4 -target mips64el-unknown-linux-gnuabin32 -mabi=n32 -fsyntax-only n32.c
    '';
    installPhase = ''
      mkdir -p "$out/bin"
      for path in ${clang}/*; do
        test "$(basename "$path")" = bin || ln -s "$path" "$out/"
      done
      for path in ${clang}/bin/*; do
        case "$(basename "$path")" in clang|clang++|clang-cl|clang-3.4) ;; *) ln -s "$path" "$out/bin/";; esac
      done
      cp clang-3.4 "$out/bin/"
      for name in clang clang++ clang-cl; do ln -s clang-3.4 "$out/bin/$name"; done
    '';
    meta = clang.meta;
  };
in clang // repaired
