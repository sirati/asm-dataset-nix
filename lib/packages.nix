# Curated package list, tiered by build complexity.
# All packages are verified to support cross-compilation and stdenv override.

{ }:

{
  # Tier 1: Minimal, fast-building — used for smoke tests
  tier1 = [
    {
      attr = "hello";
      label = "hello";
    }
    {
      attr = "busybox";
      label = "busybox";
    }
  ];

  # Tier 2: Small libraries and utilities
  tier2 = [
    {
      attr = "zlib";
      label = "zlib";
    }
    {
      attr = "bzip2";
      label = "bzip2";
    }
    {
      attr = "lz4";
      label = "lz4";
    }
    {
      attr = "xz";
      label = "xz";
    }
    {
      attr = "sqlite";
      label = "sqlite";
    }
    {
      attr = "lua5_4";
      label = "lua";
    }
  ];

  # Tier 3: Larger GNU utilities
  tier3 = [
    {
      attr = "coreutils";
      label = "coreutils";
    }
    {
      attr = "findutils";
      label = "findutils";
    }
    {
      attr = "gawk";
      label = "gawk";
    }
    {
      attr = "gnused";
      label = "gnused";
    }
    {
      attr = "gnugrep";
      label = "gnugrep";
    }
    {
      attr = "diffutils";
      label = "diffutils";
    }
    {
      attr = "patch";
      label = "patch";
    }
    {
      attr = "which";
      label = "which";
    }
    {
      attr = "file";
      label = "file";
    }
  ];

  all = [
    {
      attr = "hello";
      label = "hello";
    }
    {
      attr = "busybox";
      label = "busybox";
    }
    {
      attr = "zlib";
      label = "zlib";
    }
    {
      attr = "bzip2";
      label = "bzip2";
    }
    {
      attr = "lz4";
      label = "lz4";
    }
    {
      attr = "xz";
      label = "xz";
    }
    {
      attr = "sqlite";
      label = "sqlite";
    }
    {
      attr = "lua5_4";
      label = "lua";
    }
    {
      attr = "coreutils";
      label = "coreutils";
    }
    {
      attr = "findutils";
      label = "findutils";
    }
    {
      attr = "gawk";
      label = "gawk";
    }
    {
      attr = "gnused";
      label = "gnused";
    }
    {
      attr = "gnugrep";
      label = "gnugrep";
    }
    {
      attr = "diffutils";
      label = "diffutils";
    }
    {
      attr = "patch";
      label = "patch";
    }
    {
      attr = "which";
      label = "which";
    }
    {
      attr = "file";
      label = "file";
    }
  ];
}
