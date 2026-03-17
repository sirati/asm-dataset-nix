# Import and configure old nixpkgs sets for legacy compiler discovery.
#
# Returns a list of old-compiler spec groups ready for lib/old-compilers.nix.
#
# Arguments:
#   system             — e.g. "x86_64-linux"
#   nixpkgsInputs      — attrset of flake inputs: { nixpkgs-15_09, nixpkgs-18_03, ... }
#   mipsClangOverlay   — the MIPS compiler-rt fix overlay (or null to skip)
{
  system,
  nixpkgsInputs,
  mipsClangOverlay ? null,
}:
let
  overlays = if mipsClangOverlay != null then [ mipsClangOverlay ] else [ ];

  # Pre-flake nixpkgs (flake = false) don't need the MIPS overlay
  # because their clang versions are too old to have the compiler-rt
  # issues (they use GCC-based cross stdenvs anyway).
  oldPkgs_15_09 = import nixpkgsInputs.nixpkgs-15_09 {
    inherit system;
    config.allowUnfree = true;
  };
  oldPkgs_18_03 = import nixpkgsInputs.nixpkgs-18_03 {
    inherit system;
    config.allowUnfree = true;
  };

  # Flake-era nixpkgs get the MIPS overlay so their cross compiler-rt
  # builds succeed for MIPS targets.
  oldPkgs_22_11 = import nixpkgsInputs.nixpkgs-22_11 {
    inherit system overlays;
    config.allowUnfree = true;
  };
  oldPkgs_23_11 = import nixpkgsInputs.nixpkgs-23_11 {
    inherit system overlays;
    config.allowUnfree = true;
  };
  oldPkgs_24_05 = import nixpkgsInputs.nixpkgs-24_05 {
    inherit system overlays;
    config.allowUnfree = true;
  };
in
[
  # ── nixpkgs 15.09: GCC 4.4, 4.6, Clang 3.6 ──
  {
    oldPkgs = oldPkgs_15_09;
    nixpkgsSrc = nixpkgsInputs.nixpkgs-15_09;
    inherit system;
    gccSpecs = [
      { attr = "gcc44"; label = "4_4"; }
      { attr = "gcc46"; label = "4_6"; }
    ];
    clangSpecs = [
      { attr = "llvmPackages_36"; label = "3_6"; }
    ];
  }

  # ── nixpkgs 18.03: GCC 4.5, 5; Clang 3.4-3.9, 4 ──
  {
    oldPkgs = oldPkgs_18_03;
    nixpkgsSrc = nixpkgsInputs.nixpkgs-18_03;
    inherit system;
    gccSpecs = [
      { attr = "gcc45"; label = "4_5"; }
      { attr = "gcc5"; label = "5"; }
    ];
    clangSpecs = [
      { attr = "llvmPackages_34"; label = "3_4"; }
      { attr = "llvmPackages_35"; label = "3_5"; }
      { attr = "llvmPackages_37"; label = "3_7"; }
      { attr = "llvmPackages_38"; label = "3_8"; }
      { attr = "llvmPackages_39"; label = "3_9"; }
      { attr = "llvmPackages_4"; label = "4"; }
    ];
  }

  # ── nixpkgs 22.11: GCC 4.8-12, Clang 5-14 ──
  {
    oldPkgs = oldPkgs_22_11;
    gccSpecs = [
      { attr = "gcc48"; label = "4_8"; }
      { attr = "gcc49"; label = "4_9"; }
      { attr = "gcc6"; label = "6"; }
      { attr = "gcc7"; label = "7"; }
      { attr = "gcc8"; label = "8"; }
      { attr = "gcc9"; label = "9"; }
      { attr = "gcc10"; label = "10"; }
      { attr = "gcc11"; label = "11"; }
      { attr = "gcc12"; label = "12"; }
    ];
    clangSpecs = [
      { attr = "llvmPackages_5"; label = "5"; }
      { attr = "llvmPackages_6"; label = "6"; }
      { attr = "llvmPackages_7"; label = "7"; }
      { attr = "llvmPackages_8"; label = "8"; }
      { attr = "llvmPackages_9"; label = "9"; }
      { attr = "llvmPackages_10"; label = "10"; }
      { attr = "llvmPackages_11"; label = "11"; }
      { attr = "llvmPackages_12"; label = "12"; }
      { attr = "llvmPackages_13"; label = "13"; }
      { attr = "llvmPackages_14"; label = "14"; }
    ];
  }

  # ── nixpkgs 23.11: Clang 15-16 ──
  {
    oldPkgs = oldPkgs_23_11;
    clangSpecs = [
      { attr = "llvmPackages_15"; label = "15"; }
      { attr = "llvmPackages_16"; label = "16"; }
    ];
    gccSpecs = [ ];
  }

  # ── nixpkgs 24.05: Clang 17 ──
  {
    oldPkgs = oldPkgs_24_05;
    clangSpecs = [
      { attr = "llvmPackages_17"; label = "17"; }
    ];
    gccSpecs = [ ];
  }
]
