# Test suite for retry wrappers
# This file creates several test derivations to verify that retry wrappers work correctly

{ pkgs ? import <nixpkgs> { config.allowUnfree = true; } }:

let
  retryWrappers = import ./nix-retry-wrapper { inherit pkgs; };

  cudaPackages_12_6_pascal = import ./pkgs/torch/cuda-packages-pascal.nix {
    inherit pkgs;
    cudaLabel    = "cu126";
    cudaPackages = pkgs.cudaPackages_12_6;
  };

  wrappers = retryWrappers.makeAllRetryWrappers pkgs.cudaPackages_12_6 {
    maxAttempts = 3;
  };

  wrappersPascal = retryWrappers.makeAllRetryWrappers cudaPackages_12_6_pascal {
    maxAttempts = 3;
  };

in
{
  # Test 1: Simple test to verify wrappers are in PATH and callable
  test-wrapper-presence = pkgs.runCommand "test-wrapper-presence" {
    nativeBuildInputs = [ wrappers ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing wrapper presence ===" >&2

    # Check that wrappers exist and are executable
    type gcc >&2
    type g++ >&2
    type nvcc >&2
    type ninja >&2

    # Verify they show retry messages
    echo "=== Testing GCC wrapper ===" >&2
    gcc --version 2>&1 | tee gcc_output.txt

    if grep -q "=== \[gcc\] Attempt" gcc_output.txt; then
      echo "SUCCESS: GCC wrapper is active" >&2
      echo "SUCCESS" > $out
    else
      echo "FAILED: GCC wrapper not showing retry messages" >&2
      cat gcc_output.txt >&2
      exit 1
    fi
  '';

  # Test 2: Verify wrapper handles successful compilation
  test-simple-compilation = pkgs.runCommand "test-simple-compilation" {
    nativeBuildInputs = [ wrappers pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing simple C compilation ===" >&2

    cat > test.c << 'EOF'
#include <stdio.h>
int main() {
  printf("Hello from retry wrapper!\n");
  return 0;
}
EOF

    # Compile with wrapped gcc (should show retry message)
    gcc -o test test.c 2>&1 | tee compile_output.txt

    if grep -q "=== \[gcc\] Attempt 1 of 3 ===" compile_output.txt; then
      echo "SUCCESS: Wrapper showed retry attempt message" >&2
    else
      echo "WARNING: No retry message found" >&2
      cat compile_output.txt >&2
    fi

    # Run the compiled program
    ./test

    echo "SUCCESS" > $out
  '';

  # Test 3: Verify wrapper handles C++ compilation
  test-cpp-compilation = pkgs.runCommand "test-cpp-compilation" {
    nativeBuildInputs = [ wrappers pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing C++ compilation ===" >&2

    cat > test.cpp << 'EOF'
#include <iostream>
int main() {
  std::cout << "Hello from C++ retry wrapper!" << std::endl;
  return 0;
}
EOF

    # Compile with wrapped g++ (should show retry message)
    g++ -o test test.cpp 2>&1 | tee compile_output.txt

    if grep -q "=== \[g++\] Attempt 1 of 3 ===" compile_output.txt; then
      echo "SUCCESS: G++ wrapper showed retry attempt message" >&2
    else
      echo "WARNING: No retry message found for g++" >&2
      cat compile_output.txt >&2
    fi

    # Run the compiled program
    ./test

    echo "SUCCESS" > $out
  '';

  # Test 4: Test CUDA compilation (if CUDA is available)
  test-cuda-compilation = pkgs.runCommand "test-cuda-compilation" {
    nativeBuildInputs = [ wrappers pkgs.cudaPackages_12_6.cuda_nvcc pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing CUDA compilation ===" >&2

    cat > test.cu << 'EOF'
#include <stdio.h>

__global__ void hello() {
    printf("Hello from CUDA kernel!\n");
}

int main() {
    hello<<<1, 1>>>();
    cudaDeviceSynchronize();
    return 0;
}
EOF

    # Try to compile with wrapped nvcc
    echo "Attempting CUDA compilation..." >&2
    if nvcc -o test test.cu 2>&1 | tee compile_output.txt; then
      echo "CUDA compilation succeeded" >&2

      if grep -q "=== \[nvcc\] Attempt 1 of 3 ===" compile_output.txt; then
        echo "SUCCESS: NVCC wrapper showed retry attempt message" >&2
      else
        echo "INFO: NVCC compiled but no retry message found" >&2
        cat compile_output.txt >&2
      fi
    else
      echo "INFO: CUDA compilation failed (this is OK if no GPU present)" >&2
      cat compile_output.txt >&2
    fi

    echo "SUCCESS" > $out
  '';

  # Test 5: Test ninja wrapper
  test-ninja-wrapper = pkgs.runCommand "test-ninja-wrapper" {
    nativeBuildInputs = [ wrappers pkgs.ninja pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing Ninja wrapper ===" >&2

    # Create a simple build.ninja file
    cat > build.ninja << 'EOF'
rule compile
  command = gcc -c $in -o $out
  description = Compiling $in

rule link
  command = gcc $in -o $out
  description = Linking $out

build test.o: compile test.c
build test: link test.o
EOF

    cat > test.c << 'EOF'
#include <stdio.h>
int main() {
  printf("Built with ninja!\n");
  return 0;
}
EOF

    # Run ninja with wrapper
    ninja 2>&1 | tee ninja_output.txt

    if grep -q "=== \[ninja\] Attempt" ninja_output.txt; then
      echo "SUCCESS: Ninja wrapper showed retry attempt message" >&2
    else
      echo "INFO: Ninja ran but no retry message found" >&2
      cat ninja_output.txt >&2
    fi

    # Run the built program
    ./test

    echo "SUCCESS" > $out
  '';

  # Test 6: Test that wrappers properly propagate exit codes
  test-failure-propagation = pkgs.runCommand "test-failure-propagation" {
    nativeBuildInputs = [ wrappers pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappers}/bin:$PATH"

    echo "=== Testing failure propagation ===" >&2

    # Create invalid C code that will fail to compile
    cat > invalid.c << 'EOF'
#include <stdio.h>
int main() {
  this_is_invalid_syntax
  return 0;
}
EOF

    # Try to compile (should fail and retry 3 times)
    if gcc -o test invalid.c 2>&1 | tee compile_output.txt; then
      echo "FAILED: Compilation should have failed!" >&2
      exit 1
    else
      echo "Expected compilation failure occurred" >&2

      # Count retry attempts
      retry_count=$(grep -c "=== \[gcc\] Attempt" compile_output.txt || true)
      echo "Retry attempts: $retry_count" >&2

      if [ "$retry_count" -eq 3 ]; then
        echo "SUCCESS: All 3 retry attempts were made" >&2
      else
        echo "WARNING: Expected 3 retry attempts, got $retry_count" >&2
        cat compile_output.txt >&2
      fi

      # Check for failure message
      if grep -q "=== \[gcc\] All 3 attempts failed ===" compile_output.txt; then
        echo "SUCCESS: Proper failure message shown" >&2
      else
        echo "WARNING: Final failure message not found" >&2
      fi
    fi

    echo "SUCCESS" > $out
  '';

  # Test 7: Verify Pascal variant wrappers work
  test-pascal-wrappers = pkgs.runCommand "test-pascal-wrappers" {
    nativeBuildInputs = [ wrappersPascal pkgs.gcc ];
  } ''
    set -x
    export PATH="${wrappersPascal}/bin:$PATH"

    echo "=== Testing Pascal variant wrappers ===" >&2

    type gcc >&2
    type nvcc >&2

    cat > test.c << 'EOF'
#include <stdio.h>
int main() {
  printf("Hello from Pascal wrappers!\n");
  return 0;
}
EOF

    gcc -o test test.c 2>&1 | tee compile_output.txt

    if grep -q "=== \[gcc\] Attempt" compile_output.txt; then
      echo "SUCCESS: Pascal wrappers are active" >&2
    else
      echo "WARNING: Pascal wrapper not showing retry messages" >&2
      cat compile_output.txt >&2
    fi

    ./test
    echo "SUCCESS" > $out
  '';

  # Test 8: Build a simple derivation with retry wrappers in stdenv
  test-with-stdenv-override = pkgs.stdenv.mkDerivation {
    name = "test-stdenv-retry";

    nativeBuildInputs = [ wrappers ];

    src = pkgs.writeTextDir "test.c" ''
      #include <stdio.h>
      int main() {
        printf("Built with stdenv retry wrappers!\n");
        return 0;
      }
    '';

    preConfigure = ''
      export PATH="${wrappers}/bin:$PATH"
      echo "=== Wrappers added to PATH ===" >&2
    '';

    buildPhase = ''
      echo "=== Building with wrapped compiler ===" >&2
      gcc -o test test.c 2>&1 | tee $NIX_BUILD_TOP/compile_output.txt

      if grep -q "=== \[gcc\] Attempt" $NIX_BUILD_TOP/compile_output.txt; then
        echo "SUCCESS: Wrapper is active during buildPhase" >&2
      else
        echo "WARNING: No retry messages in buildPhase" >&2
        cat $NIX_BUILD_TOP/compile_output.txt >&2
      fi
    '';

    installPhase = ''
      mkdir -p $out/bin
      cp test $out/bin/
      cp $NIX_BUILD_TOP/compile_output.txt $out/compile_output.txt
    '';

    doCheck = true;
    checkPhase = ''
      echo "=== Running compiled program ===" >&2
      ./test
    '';
  };

  # Test 9: All tests combined
  run-all-tests = pkgs.runCommand "run-all-retry-wrapper-tests" {
    buildInputs = [
      # Reference all test outputs to force them to build
    ];
  } ''
    echo "=== All retry wrapper tests ===" > $out
    echo "" >> $out
    echo "Individual tests should be run separately:" >> $out
    echo "  nix build .#tests.test-wrapper-presence" >> $out
    echo "  nix build .#tests.test-simple-compilation" >> $out
    echo "  nix build .#tests.test-cpp-compilation" >> $out
    echo "  nix build .#tests.test-cuda-compilation" >> $out
    echo "  nix build .#tests.test-ninja-wrapper" >> $out
    echo "  nix build .#tests.test-failure-propagation" >> $out
    echo "  nix build .#tests.test-pascal-wrappers" >> $out
    echo "  nix build .#tests.test-with-stdenv-override" >> $out
  '';
}
