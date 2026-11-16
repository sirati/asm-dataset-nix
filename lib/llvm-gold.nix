# Build just the linker plugin against the selected LLVM release's shared
# backend. This does not build LLVM or substitute another version's optimizer.
{ pkgs, llvm, clang }:
let
  lib = pkgs.lib;
  plugin = pkgs.buildPackages.stdenv.mkDerivation {
    pname = "llvm-gold";
    version = llvm.version;
    dontUnpack = true;
    dontConfigure = true;
    buildPhase = ''
      mkdir -p include/llvm/Config
      # The installed public LLVM headers omit the private config.h. The gold
      # source uses it only for plugin-api.h's fixed-width integer types.
      printf '#define HAVE_STDINT_H 1\n' > include/llvm/Config/config.h
      "$CXX" -std=c++17 -O2 -g0 -fPIC -fno-rtti -shared \
        -Iinclude -I${lib.getDev llvm}/include \
        -I${lib.getDev pkgs.buildPackages.binutils-unwrapped}/include \
        ${llvm.src}/llvm/tools/gold/gold-plugin.cpp \
        -L${lib.getLib llvm}/lib -Wl,-rpath,${lib.getLib llvm}/lib \
        -lLLVM-${lib.versions.major llvm.version} -o LLVMgold.so
    '';
    installPhase = ''mkdir -p "$out/lib"; cp LLVMgold.so "$out/lib/"'';
  };
  adapted = pkgs.buildPackages.symlinkJoin {
    name = "capture-clang-${llvm.version}-with-llvmgold";
    paths = [ clang (lib.getLib clang) ];
    postBuild = ''
      if test -L "$out/lib/LLVMgold.so"; then rm "$out/lib/LLVMgold.so"; fi
      test ! -e "$out/lib/LLVMgold.so"
      ln -s ${plugin}/lib/LLVMgold.so "$out/lib/LLVMgold.so"
    '';
  };
in clang // adapted // { datasetLlvmGold = plugin; }
