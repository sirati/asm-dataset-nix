# Cross-compilation target definitions.
# All targets are verified to work with both GCC and Clang on nixpkgs-unstable.
#
# Returns: { targets = { ... }; getPkgsForTarget = pkgs -> target -> pkgs; }

{ }:

{
  targets = {
    x86_64 = {
      label = "x86_64";
      crossAttr = null; # native — no cross-compilation needed
      system = "x86_64-linux";
    };
    i686 = {
      label = "i686";
      crossAttr = "gnu32";
      crossConfig = "i686-unknown-linux-gnu";
      system = "i686-linux";
    };
    aarch64 = {
      label = "aarch64";
      crossAttr = "aarch64-multiplatform";
      crossConfig = "aarch64-unknown-linux-gnu";
      system = "aarch64-linux";
    };
    armv7l = {
      label = "armv7l";
      crossAttr = "armv7l-hf-multiplatform";
      crossConfig = "armv7l-unknown-linux-gnueabihf";
      system = "armv7l-linux";
    };
    mipsel = {
      label = "mipsel";
      crossAttr = "mipsel-linux-gnu";
      crossConfig = "mipsel-unknown-linux-gnu";
      system = "mipsel-linux";
    };
    mips64el = {
      label = "mips64el";
      crossAttr = "mips64el-linux-gnuabin32";
      crossConfig = "mips64el-unknown-linux-gnuabin32";
      system = "mips64el-linux";
    };
  };

  # Helper: given native pkgs and a target, return the appropriate pkgs set.
  # For native (x86_64), returns pkgs unchanged.
  # For cross targets, returns pkgs.pkgsCross.<crossAttr>.
  getPkgsForTarget =
    pkgs: target: if target.crossAttr == null then pkgs else pkgs.pkgsCross.${target.crossAttr};
}
