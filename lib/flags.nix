# Optimization levels, code-changing flag sets, and hardening modes.
#
# Flag sets with null cflags use compiler-specific alternatives
# (gccFlag / clangFlag), resolved at build time by mkVariant.nix.
#
# extraHardeningDisable: additional hardening flags to disable for this flag set
# ldflags: extra linker flags

{ }:

let
  optimizationLevels = [
    {
      flag = "-O0";
      label = "O0";
      clangOnly = false;
    }
    {
      flag = "-O1";
      label = "O1";
      clangOnly = false;
    }
    {
      flag = "-O2";
      label = "O2";
      clangOnly = false;
    }
    {
      flag = "-O3";
      label = "O3";
      clangOnly = false;
    }
    {
      flag = "-Os";
      label = "Os";
      clangOnly = false;
    }
    {
      flag = "-Oz";
      label = "Oz";
      clangOnly = true;
    }
    {
      flag = "-Ofast";
      label = "Ofast";
      clangOnly = false;
    }
  ];

  flagSets = [
    {
      label = "baseline";
      cflags = "";
      cxxflags = "";
    }
    {
      label = "noinline";
      cflags = "-fno-inline";
      cxxflags = "-fno-inline";
    }
    {
      label = "unroll";
      cflags = "-funroll-loops";
      cxxflags = "-funroll-loops";
    }
    {
      label = "sections";
      cflags = "-ffunction-sections -fdata-sections";
      cxxflags = "-ffunction-sections -fdata-sections";
    }
    {
      label = "frameptr";
      cflags = "-fno-omit-frame-pointer";
      cxxflags = "-fno-omit-frame-pointer";
    }
    {
      label = "nopic";
      cflags = "-fno-PIC";
      cxxflags = "-fno-PIC";
      ldflags = "-no-pie";
      # Must also disable PIE hardening, otherwise the linker re-adds -pie
      extraHardeningDisable = [ "pie" ];
    }
    {
      label = "novec";
      cflags = null;
      cxxflags = null;
      gccFlag = "-fno-tree-vectorize";
      clangFlag = "-fno-vectorize";
    }
  ];

  hardeningModes = [
    {
      label = "hardened";
      hardeningDisable = [ ];
    }
    {
      label = "unhardened";
      hardeningDisable = [ "all" ];
    }
  ];

in
{
  inherit optimizationLevels flagSets hardeningModes;
}
