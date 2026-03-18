# Cross-compilation target definitions.
# All targets are verified to work with both GCC and Clang on nixpkgs-unstable.
#
# Each target has minGccVersion/minClangVersion ({ major, minor }) from support_matrix.md.
# Compiler/target combos below the minimum are excluded from the matrix.
# { major = 0; minor = 0; } means no restriction (all compilers supported).
#
# Returns: { targets = { ... }; getPkgsForTarget = pkgs -> target -> pkgs; }

{ }:

let
  noMin = {
    major = 0;
    minor = 0;
  };
in
{
  targets = {
    x86_64 = {
      label = "x86_64";
      crossAttr = null; # native — no cross-compilation needed
      system = "x86_64-linux";
      minGccVersion = noMin;
      minClangVersion = noMin;
    };
    i686 = {
      label = "i686";
      crossAttr = "gnu32";
      crossConfig = "i686-unknown-linux-gnu";
      system = "i686-linux";
      minGccVersion = noMin;
      minClangVersion = noMin;
    };
    aarch64 = {
      label = "aarch64";
      crossAttr = "aarch64-multiplatform";
      crossConfig = "aarch64-unknown-linux-gnu";
      system = "aarch64-linux";
      minGccVersion = {
        major = 4;
        minor = 8;
      }; # ARM64 added in GCC 4.8
      minClangVersion = noMin; # clang3_4 passes (lower than matrix's 3.5)
    };
    armv7l = {
      label = "armv7l";
      crossAttr = "armv7l-hf-multiplatform";
      crossConfig = "armv7l-unknown-linux-gnueabihf";
      system = "armv7l-linux";
      minGccVersion = {
        major = 4;
        minor = 7;
      }; # gnueabihf target triple added in GCC 4.7
      minClangVersion = noMin;
    };
    mipsel = {
      label = "mipsel";
      crossAttr = "mipsel-linux-gnu";
      crossConfig = "mipsel-unknown-linux-gnu";
      system = "mipsel-linux";
      minGccVersion = noMin;
      minClangVersion = noMin;
    };
    mips64el = {
      label = "mips64el";
      crossAttr = "mips64el-linux-gnuabin32";
      crossConfig = "mips64el-unknown-linux-gnuabin32";
      system = "mips64el-linux";
      minGccVersion = noMin;
      minClangVersion = noMin;
    };
    ppc32 = {
      label = "ppc32";
      crossAttr = "ppc32";
      crossConfig = "powerpc-unknown-linux-gnu";
      system = "powerpc-linux";
      minGccVersion = noMin;
      minClangVersion = noMin;
    };
    ppc64 = {
      label = "ppc64";
      crossAttr = "ppc64";
      crossConfig = "powerpc64-unknown-linux-gnuabielfv2";
      system = "powerpc64-linux";
      minGccVersion = {
        major = 4;
        minor = 9;
      }; # ELFv2 ABI (gnuabielfv2) added in GCC 4.9
      minClangVersion = noMin;
    };
    riscv64 = {
      label = "riscv64";
      crossAttr = "riscv64";
      crossConfig = "riscv64-unknown-linux-gnu";
      system = "riscv64-linux";
      minGccVersion = {
        major = 7;
        minor = 0;
      }; # RISC-V added in GCC 7
      minClangVersion = {
        major = 9;
        minor = 0;
      }; # RISC-V stabilized in Clang 9
    };
  };

  # Helper: given native pkgs and a target, return the appropriate pkgs set.
  # For native (x86_64), returns pkgs unchanged.
  # For cross targets, returns pkgs.pkgsCross.<crossAttr>.
  getPkgsForTarget =
    pkgs: target:
    if target.crossAttr == null then
      pkgs
    else if !(pkgs ? pkgsCross) || !(pkgs.pkgsCross ? ${target.crossAttr}) then
      null
    else
      pkgs.pkgsCross.${target.crossAttr};
}
