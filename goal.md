the goal of this project is to provide a nix flake with the following capabilities:
- discover all nix packaged compiler versions of gcc, clang and maybe others
- compile a list of packages with the superset of all available compilers, all their versions, all optimization levels, set of flags that massively change the code output, disabling and enabling of nixes protection measures
- cross compile to x86, x64, arm, arm64, mips, mips64 and other architectures fully supported by angr decompiler
- the flake should be able to generate a file containing all to build derivations, and their dependencies, so that a manager could assign a subset to a SLURM job scheduler
- the output for each program should not be the regular output, but only ALL binaries created by the individual nix derivation as a tarball with all files at root level. all other files are not included, the tarball is not supposed to be a working configuration of the program.

when impl this always launch test jobs in the background with jobs 1 and cores 1 , if you can in a systemd --user  cgroup to restrict ram usage to 1GB, 4GB with swap -> 3GB swap allowed , only test 
at most have 10 tests running at the same time.


currently the project contains some nix files as a reference but they have no other significance.
